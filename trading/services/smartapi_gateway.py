from __future__ import annotations

import binascii
import logging
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

import pandas as pd
import pyotp
from django.conf import settings

log = logging.getLogger(__name__)

SMARTAPI_IMPORT_ERROR = None
try:
    from SmartApi import SmartConnect
except Exception as primary_exc:  # pragma: no cover - dependency may not be installed in all environments
    try:
        from smartapi import SmartConnect  # type: ignore[no-redef]
    except Exception as fallback_exc:  # pragma: no cover - dependency may not be installed in all environments
        SmartConnect = None  # type: ignore[assignment]
        SMARTAPI_IMPORT_ERROR = fallback_exc or primary_exc


@dataclass
class SmartCredentials:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str

    def is_valid(self) -> bool:
        return all([self.api_key, self.client_code, self.pin, self.totp_secret])


class SmartAPIClient:
    _HISTORICAL_CACHE: dict[tuple[str, str, str, str, int, str], tuple[float, pd.DataFrame]] = {}
    _HISTORICAL_CACHE_LOCK = Lock()
    _HISTORICAL_CACHE_MAX_ITEMS = 600
    _LOGIN_COOLDOWN_UNTIL: dict[str, float] = {}
    _LOGIN_COOLDOWN_LOCK = Lock()

    def __init__(
        self,
        credentials: SmartCredentials,
        timeout_seconds: int = 15,
        login_retries: int = 2,
        root_url: str = "",
    ):
        self.credentials = credentials
        self.timeout_seconds = max(3, int(timeout_seconds or 15))
        self.login_retries = max(1, int(login_retries or 1))
        self.root_url = (root_url or "").strip()
        self.obj = None
        self.auth_token = ""
        self.refresh_token = ""
        self.feed_token = ""

    @classmethod
    def from_user_profile(cls, profile):
        defaults = settings.SMART_API_DEFAULTS
        credentials = SmartCredentials(
            api_key=profile.smart_api_key or defaults.get("api_key", ""),
            client_code=profile.smart_client_code or defaults.get("client_code", ""),
            pin=profile.smart_pin or defaults.get("pin", ""),
            totp_secret=profile.smart_totp_secret or defaults.get("totp_secret", ""),
        )
        return cls(
            credentials=credentials,
            timeout_seconds=getattr(settings, "SMART_API_TIMEOUT_SECONDS", 15),
            login_retries=getattr(settings, "SMART_API_LOGIN_RETRIES", 2),
            root_url=getattr(settings, "SMART_API_ROOT_URL", ""),
        )

    def generate_totp(self) -> str:
        if not self.credentials.totp_secret:
            raise ValueError("Missing SmartAPI TOTP secret")

        try:
            return pyotp.TOTP(self.credentials.totp_secret).now()
        except (binascii.Error, TypeError, ValueError) as exc:
            raise ValueError("Invalid SmartAPI TOTP secret. Use the Base32 secret, not a 6-digit OTP.") from exc

    def validate_login(self) -> Tuple[bool, str]:
        if not self.credentials.is_valid():
            return False, "Missing SmartAPI credentials. Update your profile first."

        if SmartConnect is None:
            if SMARTAPI_IMPORT_ERROR:
                return False, f"smartapi-python import failed: {SMARTAPI_IMPORT_ERROR}"
            return False, "smartapi-python package is not installed."

        host = self._smartapi_host()
        if host and not self._is_host_reachable(host):
            return False, f"Cannot reach {host}:443. Check internet/firewall/VPN/proxy settings."

        cooldown_remaining = self._login_cooldown_remaining_seconds()
        if cooldown_remaining > 0:
            return False, f"SmartAPI access rate limit is active. Retry in about {cooldown_remaining}s."

        last_error = ""
        for attempt in range(1, self.login_retries + 1):
            try:
                connect_kwargs = {
                    "api_key": self.credentials.api_key,
                    "timeout": self.timeout_seconds,
                }
                if self.root_url:
                    connect_kwargs["root"] = self.root_url

                self.obj = SmartConnect(**connect_kwargs)
                totp = self.generate_totp()
                data = self.obj.generateSession(
                    self.credentials.client_code,
                    self.credentials.pin,
                    totp,
                )
                if not isinstance(data, dict):
                    last_error = str(data)
                    if self._is_rate_limited_error(last_error):
                        self._set_login_cooldown()
                        if attempt < self.login_retries:
                            time.sleep(min(2.0 * attempt, 4.0))
                            continue
                    break

                if data.get("status"):
                    self._clear_login_cooldown()
                    self.auth_token = data["data"].get("jwtToken", "")
                    self.refresh_token = data["data"].get("refreshToken", "")
                    self.feed_token = self.obj.getfeedToken()
                    return True, f"Connected as {data['data'].get('name', self.credentials.client_code)}"

                message = str(data.get("message", "SmartAPI login failed"))
                if self._is_rate_limited_error(message):
                    last_error = message
                    self._set_login_cooldown()
                    if attempt < self.login_retries:
                        time.sleep(min(2.0 * attempt, 4.0))
                        continue
                    break
                return False, message
            except Exception as exc:  # pragma: no cover - external API behaviour
                last_error = str(exc)
                if self._is_rate_limited_error(last_error):
                    self._set_login_cooldown()
                    if attempt < self.login_retries:
                        time.sleep(min(2.0 * attempt, 4.0))
                        continue
                    break
                if self._is_timeout_error(last_error) and attempt < self.login_retries:
                    time.sleep(min(1.5 * attempt, 3))
                    continue
                break

        if self._is_rate_limited_error(last_error):
            retry_in = self._login_cooldown_remaining_seconds() or int(self._login_cooldown_seconds())
            return (
                False,
                f"SmartAPI access rate limit reached. Please retry in about {retry_in}s.",
            )
        if self._is_timeout_error(last_error):
            return (
                False,
                f"SmartAPI network timeout after {self.login_retries} attempt(s). "
                f"Check access to {host}:443 and retry.",
            )
        return False, f"Login error: {last_error or 'unknown error'}"

    def ensure_login(self) -> bool:
        if self.obj and self.auth_token:
            return True
        success, _ = self.validate_login()
        return success

    def fetch_historical_data(
        self,
        token: str,
        interval: str = "FIVE_MINUTE",
        days: int = 5,
        exchange: str = "NSE",
    ) -> Optional[pd.DataFrame]:
        try:
            day_count = max(1, int(days or 1))
        except (TypeError, ValueError):
            day_count = 1

        cache_key = self._historical_cache_key(
            token=token,
            interval=interval,
            day_count=day_count,
            exchange=exchange,
        )
        cached = self._get_cached_historical_data(cache_key)
        if cached is not None:
            return cached

        if not self.ensure_login():
            return None

        end_date = datetime.now()
        start_date = end_date - timedelta(days=day_count)
        full_payload = self._build_candle_payload(
            exchange=exchange,
            token=token,
            interval=interval,
            from_dt=start_date,
            to_dt=end_date,
        )

        response = self._get_candle_data_with_retry(payload=full_payload, max_attempts=2)
        if isinstance(response, dict) and response.get("data"):
            full_frame = self._rows_to_candle_frame(response["data"])
            if full_frame is not None:
                self._set_cached_historical_data(cache_key, full_frame)
            return full_frame

        if day_count <= 1:
            return None

        all_rows: list[Any] = []
        current_start = start_date
        while current_start < end_date:
            current_end = min(current_start + timedelta(days=1), end_date)
            chunk_payload = self._build_candle_payload(
                exchange=exchange,
                token=token,
                interval=interval,
                from_dt=current_start,
                to_dt=current_end,
            )
            chunk_response = self._get_candle_data_with_retry(payload=chunk_payload, max_attempts=2)
            if isinstance(chunk_response, dict) and chunk_response.get("data"):
                all_rows.extend(chunk_response["data"])
            current_start = current_end

        chunk_frame = self._rows_to_candle_frame(all_rows)
        if chunk_frame is not None:
            self._set_cached_historical_data(cache_key, chunk_frame)
        return chunk_frame

    @staticmethod
    def _historical_cache_ttl_seconds() -> float:
        try:
            return max(0.0, float(getattr(settings, "SMART_API_HISTORICAL_CACHE_SECONDS", 8)))
        except (TypeError, ValueError):
            return 8.0

    def _historical_cache_key(
        self,
        token: str,
        interval: str,
        day_count: int,
        exchange: str,
    ) -> tuple[str, str, str, str, int, str]:
        return (
            str(self.credentials.client_code or "").strip().upper(),
            str(exchange or "NSE").strip().upper(),
            str(token or "").strip(),
            str(interval or "FIVE_MINUTE").strip().upper(),
            int(day_count or 1),
            str(self.root_url or "").strip().lower(),
        )

    def _get_cached_historical_data(
        self,
        cache_key: tuple[str, str, str, str, int, str],
    ) -> Optional[pd.DataFrame]:
        ttl = self._historical_cache_ttl_seconds()
        if ttl <= 0:
            return None

        now = time.monotonic()
        with self._HISTORICAL_CACHE_LOCK:
            cached = self._HISTORICAL_CACHE.get(cache_key)
            if not cached:
                return None
            ts, frame = cached
            if now - ts > ttl:
                self._HISTORICAL_CACHE.pop(cache_key, None)
                return None
            return frame.copy(deep=True)

    def _set_cached_historical_data(
        self,
        cache_key: tuple[str, str, str, str, int, str],
        frame: pd.DataFrame,
    ):
        if frame is None or frame.empty:
            return

        ttl = self._historical_cache_ttl_seconds()
        if ttl <= 0:
            return

        now = time.monotonic()
        with self._HISTORICAL_CACHE_LOCK:
            self._HISTORICAL_CACHE[cache_key] = (now, frame.copy(deep=True))

            stale_keys = [
                key
                for key, (ts, _) in self._HISTORICAL_CACHE.items()
                if (now - ts) > ttl
            ]
            for key in stale_keys:
                self._HISTORICAL_CACHE.pop(key, None)

            overflow = len(self._HISTORICAL_CACHE) - self._HISTORICAL_CACHE_MAX_ITEMS
            if overflow > 0:
                oldest = sorted(
                    self._HISTORICAL_CACHE.items(),
                    key=lambda item: item[1][0],
                )[:overflow]
                for key, _ in oldest:
                    self._HISTORICAL_CACHE.pop(key, None)

    @staticmethod
    def _build_candle_payload(
        exchange: str,
        token: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> dict[str, Any]:
        return {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }

    @staticmethod
    def _rows_to_candle_frame(rows: list[Any]) -> Optional[pd.DataFrame]:
        if not rows:
            return None

        columns = ["datetime", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame(rows, columns=columns)
        df["datetime"] = pd.to_datetime(df["datetime"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna().sort_values("datetime")
        df = df.drop_duplicates(subset=["datetime"], keep="last")
        return df if not df.empty else None

    def _call_candle_data(self, payload: dict[str, Any]):
        try:
            return self.obj.getCandleData(**payload)
        except TypeError:
            return self.obj.getCandleData(payload)

    def _get_candle_data_with_retry(self, payload: dict[str, Any], max_attempts: int = 3):
        attempts = max(1, int(max_attempts or 1))
        for attempt in range(1, attempts + 1):
            try:
                response = self._call_candle_data(payload)
            except Exception as exc:
                message = str(exc)
                if attempt >= attempts or not self._is_retryable_historical_error(message):
                    return None
                time.sleep(min(0.6 * attempt, 1.5))
                continue

            if not isinstance(response, dict):
                return None

            if response.get("status") is False:
                message = str(response.get("message", ""))
                if attempt >= attempts or not self._is_retryable_historical_error(message):
                    return response
                time.sleep(min(0.6 * attempt, 1.5))
                continue

            return response

        return None

    @staticmethod
    def _as_float(value) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        value = str(symbol or "").strip().upper()
        if value.endswith("-EQ"):
            value = value[:-3]
        return value

    @staticmethod
    def _normalize_order_status(status: Any) -> str:
        return str(status or "").strip().upper().replace(" ", "_")

    @staticmethod
    def _extract_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("fetched", "orders", "positions", "data", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            return [payload]
        return []

    @staticmethod
    def _is_pending_status(status: str) -> bool:
        pending = {
            "OPEN",
            "PENDING",
            "TRIGGER_PENDING",
            "VALIDATION_PENDING",
            "PUT_ORDER_REQ_RECEIVED",
            "MODIFY_PENDING",
            "AMO_RECEIVED",
        }
        return status in pending

    @staticmethod
    def is_terminal_rejection_status(status: str | None) -> bool:
        terminal = {
            "REJECTED",
            "CANCELLED",
            "CANCELED",
            "FAILED",
            "EXPIRED",
        }
        return SmartAPIClient._normalize_order_status(status) in terminal

    @staticmethod
    def is_filled_status(status: str | None) -> bool:
        filled = {"COMPLETE", "COMPLETED", "TRADED"}
        return SmartAPIClient._normalize_order_status(status) in filled

    @staticmethod
    def _sum_utilised_fields(payload: dict[str, Any]) -> float:
        total = 0.0
        for key, value in payload.items():
            if str(key).lower().startswith("utilised"):
                num = SmartAPIClient._as_float(value)
                if num is not None:
                    total += max(0.0, num)
        return total

    def fetch_market_quotes(
        self,
        instruments: list[dict[str, str]],
        allow_ltp_fallback: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """
        Return quote snapshot keyed by symbol token:
        {
            "2885": {"ltp": 1234.5, "bid": 1234.0, "ask": 1235.0, "volume": 120034}
        }
        """
        if not instruments or not self.ensure_login():
            return {}

        quotes: dict[str, dict[str, Any]] = {}
        exchange_tokens: dict[str, list[str]] = {}
        for item in instruments:
            token = str(item.get("token", "")).strip()
            if not token:
                continue
            exchange = str(item.get("exchange", "NSE") or "NSE").upper()
            exchange_tokens.setdefault(exchange, []).append(token)

        # Prefer batch market data call for efficiency and bid/ask depth.
        try:
            response = self.obj.getMarketData("FULL", exchange_tokens)
            data = response.get("data", {}) if isinstance(response, dict) else {}
            fetched = data.get("fetched") if isinstance(data, dict) else None
            if fetched is None and isinstance(data, list):
                fetched = data
            fetched = fetched or []

            for row in fetched:
                token = str(
                    row.get("symbolToken")
                    or row.get("symboltoken")
                    or row.get("token")
                    or ""
                ).strip()
                if not token:
                    continue

                quotes[token] = {
                    "ltp": self._as_float(
                        row.get("ltp")
                        or row.get("lastTradedPrice")
                        or row.get("close")
                    ),
                    "bid": self._as_float(
                        row.get("bestBidPrice")
                        or row.get("bestBuyPrice")
                        or row.get("best_bid_price")
                    ),
                    "ask": self._as_float(
                        row.get("bestAskPrice")
                        or row.get("bestSellPrice")
                        or row.get("best_ask_price")
                    ),
                    "volume": self._as_int(
                        row.get("tradeVolume")
                        or row.get("volume")
                        or row.get("totalTradedVolume")
                    ),
                }
        except Exception:
            # Fall back to per-symbol LTP call below.
            pass

        if not allow_ltp_fallback:
            return quotes

        # Fill remaining symbols via ltpData if FULL quote failed/partial.
        for item in instruments:
            token = str(item.get("token", "")).strip()
            if not token or token in quotes:
                continue

            exchange = str(item.get("exchange", "NSE") or "NSE").upper()
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue

            try:
                response = self.obj.ltpData(exchange, symbol, token)
                payload = response.get("data", {}) if isinstance(response, dict) else {}
                quotes[token] = {
                    "ltp": self._as_float(payload.get("ltp") or payload.get("close")),
                    "bid": None,
                    "ask": None,
                    "volume": self._as_int(payload.get("volume")),
                }
            except Exception:
                continue

        return quotes

    def fetch_fund_summary(self) -> Optional[dict[str, Any]]:
        if not self.ensure_login():
            return None

        try:
            response = self.obj.rmsLimit()
        except Exception:
            return None

        payload = response.get("data", {}) if isinstance(response, dict) else {}
        if not isinstance(payload, dict):
            return None

        total_funds = self._as_float(
            payload.get("net")
            or payload.get("availablebalance")
            or payload.get("totalnet")
            or payload.get("total")
        )
        available_funds = self._as_float(
            payload.get("availablecash")
            or payload.get("availableCash")
            or payload.get("availablemargin")
            or payload.get("availableMargin")
        )
        utilized_margin_from_fields = self._sum_utilised_fields(payload)
        if total_funds is not None and available_funds is not None:
            utilized_margin = max(0.0, total_funds - available_funds)
        else:
            utilized_margin = utilized_margin_from_fields

        margin_utilized_percent = None
        if total_funds not in (None, 0):
            margin_utilized_percent = (utilized_margin / total_funds) * 100

        return {
            "total_funds": round(total_funds, 2) if total_funds is not None else None,
            "available_funds": round(available_funds, 2) if available_funds is not None else None,
            "utilized_margin": round(utilized_margin, 2),
            "margin_utilized_percent": round(margin_utilized_percent, 2) if margin_utilized_percent is not None else None,
        }

    def get_available_margin(self) -> float:
        summary = self.fetch_fund_summary()
        if not summary:
            return 0.0

        available = summary.get("available_funds")
        if available in (None, ""):
            available = summary.get("total_funds")

        try:
            return max(0.0, float(available or 0))
        except (TypeError, ValueError):
            return 0.0

    def get_max_quantity_for_margin(
        self,
        price: float,
        available_margin: float,
        margin_usage_percent: float = 90.0,
        estimated_margin_factor: float = 0.25,
    ) -> int:
        try:
            margin_to_use = float(available_margin) * (float(margin_usage_percent) / 100.0)
            estimated_margin_per_share = float(price) * float(estimated_margin_factor)
        except (TypeError, ValueError):
            return 0

        if margin_to_use <= 0 or estimated_margin_per_share <= 0:
            return 0

        qty = int(margin_to_use / estimated_margin_per_share)
        return max(1, qty)

    def resolve_nse_symbol_token(self, symbol: str) -> Optional[dict[str, str]]:
        """
        Resolve an NSE cash symbol to SmartAPI token metadata.
        Returns:
            {"symbol": "RELIANCE", "token": "2885", "exchange": "NSE"}
        """
        query = (symbol or "").strip().upper()
        if not query or not self.ensure_login():
            return None

        queries = [query]
        if not query.endswith("-EQ"):
            queries.append(f"{query}-EQ")

        for term in queries:
            response = None
            try:
                response = self.obj.searchScrip("NSE", term)
            except TypeError:
                try:
                    response = self.obj.searchScrip({"exchange": "NSE", "searchscrip": term})
                except Exception:
                    response = None
            except Exception:
                response = None

            payload = response.get("data", {}) if isinstance(response, dict) else {}
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("fetched") or payload.get("results") or []
            else:
                rows = []

            if not isinstance(rows, list):
                continue

            preferred: Optional[dict[str, str]] = None
            fallback: Optional[dict[str, str]] = None

            for row in rows:
                if not isinstance(row, dict):
                    continue

                token = str(
                    row.get("symboltoken")
                    or row.get("symbolToken")
                    or row.get("token")
                    or ""
                ).strip()
                if not token:
                    continue

                exchange = str(
                    row.get("exchange")
                    or row.get("exchangeSegment")
                    or row.get("exch_seg")
                    or "NSE"
                ).upper()
                if exchange != "NSE":
                    continue

                trading_symbol = str(
                    row.get("tradingsymbol")
                    or row.get("symbol")
                    or row.get("name")
                    or query
                ).upper()

                normalized = trading_symbol.replace("-EQ", "").strip()
                if not normalized:
                    continue

                entry = {
                    "symbol": normalized,
                    "token": token,
                    "exchange": "NSE",
                }

                if normalized == query or trading_symbol == f"{query}-EQ":
                    preferred = entry
                    break
                if fallback is None:
                    fallback = entry

            if preferred is not None:
                return preferred
            if fallback is not None:
                return fallback

        return None

    def _smartapi_host(self) -> str:
        root = self.root_url or "https://apiconnect.angelone.in"
        parsed = urlparse(root)
        return parsed.hostname or "apiconnect.angelone.in"

    def _login_cache_key(self) -> str:
        return "|".join(
            [
                str(self.credentials.api_key or "").strip(),
                str(self.credentials.client_code or "").strip().upper(),
                str(self.root_url or "").strip().lower(),
            ]
        )

    @classmethod
    def _login_cooldown_seconds(cls) -> float:
        try:
            return max(15.0, float(getattr(settings, "SMART_API_RATE_LIMIT_COOLDOWN_SECONDS", 75)))
        except (TypeError, ValueError):
            return 75.0

    def _login_cooldown_remaining_seconds(self) -> int:
        key = self._login_cache_key()
        now = time.monotonic()
        with self._LOGIN_COOLDOWN_LOCK:
            expires_at = self._LOGIN_COOLDOWN_UNTIL.get(key, 0.0)
            if expires_at <= now:
                self._LOGIN_COOLDOWN_UNTIL.pop(key, None)
                return 0
            return int(max(1, (expires_at - now) + 0.999))

    def _set_login_cooldown(self, seconds: float | None = None):
        cooldown = self._login_cooldown_seconds() if seconds is None else seconds
        try:
            cooldown = max(1.0, float(cooldown))
        except (TypeError, ValueError):
            cooldown = self._login_cooldown_seconds()

        key = self._login_cache_key()
        expires_at = time.monotonic() + cooldown
        with self._LOGIN_COOLDOWN_LOCK:
            self._LOGIN_COOLDOWN_UNTIL[key] = max(self._LOGIN_COOLDOWN_UNTIL.get(key, 0.0), expires_at)

    def _clear_login_cooldown(self):
        key = self._login_cache_key()
        with self._LOGIN_COOLDOWN_LOCK:
            self._LOGIN_COOLDOWN_UNTIL.pop(key, None)

    @staticmethod
    def _is_host_reachable(host: str) -> bool:
        try:
            with socket.create_connection((host, 443), timeout=4):
                return True
        except OSError:
            return False

    @staticmethod
    def _is_timeout_error(message: str) -> bool:
        msg = (message or "").lower()
        timeout_markers = ["timed out", "connecttimeout", "read timeout", "max retries exceeded"]
        return any(marker in msg for marker in timeout_markers)

    @staticmethod
    def _is_rate_limited_error(message: str) -> bool:
        msg = (message or "").lower()
        markers = [
            "exceeding access rate",
            "access rate",
            "rate limit",
            "too many requests",
            "status code 429",
            "http 429",
        ]
        return any(marker in msg for marker in markers)

    @staticmethod
    def _is_retryable_historical_error(message: str) -> bool:
        msg = (message or "").lower()
        retry_markers = [
            "timed out",
            "connecttimeout",
            "read timeout",
            "max retries exceeded",
            "name resolution",
            "failed to resolve",
            "getaddrinfo failed",
            "connection aborted",
            "connection reset",
            "forcibly closed",
            "failed to establish a new connection",
            "temporarily unavailable",
        ]
        return any(marker in msg for marker in retry_markers)

    @staticmethod
    def extract_order_id(response: Any) -> str:
        if isinstance(response, str):
            return response.strip()
        if not isinstance(response, dict):
            return ""
        order_id = response.get("orderid")
        if order_id:
            return str(order_id).strip()
        data = response.get("data")
        if isinstance(data, dict):
            order_id = data.get("orderid") or data.get("orderId")
            if order_id:
                return str(order_id).strip()
        return ""

    def fetch_order_book(self) -> list[dict[str, Any]]:
        if not self.ensure_login():
            return []

        calls = [
            lambda: self.obj.orderBook(),
            lambda: self.obj.orderBook({}),
            lambda: self.obj.getOrderBook(),
        ]
        response = None
        for call in calls:
            try:
                response = call()
                break
            except Exception:
                continue

        if response is None:
            return []

        if isinstance(response, dict):
            payload = response.get("data", response)
        else:
            payload = response
        return self._extract_rows(payload)

    def get_order_status(self, order_id: str) -> str | None:
        order_id = str(order_id or "").strip()
        if not order_id:
            return None

        rows = self.fetch_order_book()
        for row in rows:
            row_id = str(
                row.get("orderid")
                or row.get("orderId")
                or row.get("id")
                or ""
            ).strip()
            if row_id != order_id:
                continue
            raw_status = row.get("status") or row.get("orderstatus") or row.get("orderStatus")
            normalized = self._normalize_order_status(raw_status)
            return normalized or None
        return None

    def fetch_positions(self) -> list[dict[str, Any]]:
        if not self.ensure_login():
            return []

        calls = [
            lambda: self.obj.position(),
            lambda: self.obj.position({}),
            lambda: self.obj.getPosition(),
        ]
        response = None
        for call in calls:
            try:
                response = call()
                break
            except Exception:
                continue

        if response is None:
            return []
        if isinstance(response, dict):
            payload = response.get("data", response)
        else:
            payload = response
        return self._extract_rows(payload)

    def get_net_position_quantity(self, symbol: str, token: str) -> int | None:
        target_symbol = self._normalize_symbol(symbol)
        target_token = str(token or "").strip()
        rows = self.fetch_positions()

        for row in rows:
            row_symbol = self._normalize_symbol(
                row.get("tradingsymbol")
                or row.get("symbol")
                or row.get("tradingSymbol")
                or ""
            )
            row_token = str(
                row.get("symboltoken")
                or row.get("symbolToken")
                or row.get("token")
                or ""
            ).strip()
            if target_token and row_token and row_token != target_token:
                continue
            if row_symbol and target_symbol and row_symbol != target_symbol:
                continue
            qty = (
                row.get("netqty")
                or row.get("netQty")
                or row.get("net_quantity")
                or row.get("netquantity")
            )
            parsed = self._as_int(qty)
            if parsed is not None:
                return parsed
        return None

    def place_intraday_market_order(
        self,
        symbol: str,
        token: str,
        transaction_type: str,
        quantity: int,
        exchange: str = "NSE",
    ) -> tuple[Optional[dict[str, Any]], str]:
        if not self.ensure_login():
            return None, "Not connected to SmartAPI"
        if int(quantity or 0) <= 0:
            return None, "Invalid quantity for market order exit"

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": str(transaction_type or "").upper(),
            "exchange": exchange,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": int(quantity),
        }
        try:
            response = self.obj.placeOrderFullResponse(order_params)
            if isinstance(response, dict):
                return response, "Market order placed"
            return {"data": response}, "Market order placed"
        except Exception:
            try:
                order_id = self.obj.placeOrder(order_params)
                if order_id:
                    return {"orderid": str(order_id)}, "Market order placed"
            except Exception as exc:  # pragma: no cover - external API behaviour
                return None, f"Market order error: {exc}"
        return None, "Market order failed"

    def place_limit_order(
        self,
        symbol: str,
        token: str,
        transaction_type: str,
        price: float,
        quantity: int,
        exchange: str = "NSE",
    ) -> Optional[str]:
        if not self.ensure_login():
            return None
        if int(quantity or 0) <= 0:
            return None

        self._rate_limit()
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": str(transaction_type or "").upper(),
            "exchange": exchange,
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": int(quantity),
            "price": round(float(price), 2),
        }
        try:
            response = self.obj.placeOrderFullResponse(order_params)
        except Exception:
            try:
                response = self.obj.placeOrder(order_params)
            except Exception as exc:  # pragma: no cover - external API behaviour
                log.error("Limit order failed: %s", exc)
                return None

        order_id = self.extract_order_id(response)
        if order_id:
            log.info("LIMIT %s %s qty=%d price=%.2f id=%s", transaction_type, symbol, quantity, price, order_id)
            return order_id
        log.error("Limit order response missing id: %s", response)
        return None

    def place_entry_order(
        self,
        symbol: str,
        token: str,
        direction: str,
        price: float,
        quantity: int,
        exchange: str = "NSE",
    ) -> Optional[str]:
        tx = "BUY" if str(direction or "").upper() == "LONG" else "SELL"
        return self.place_limit_order(symbol, token, tx, price, quantity, exchange=exchange)

    def place_target_order(
        self,
        symbol: str,
        token: str,
        direction: str,
        price: float,
        quantity: int,
        exchange: str = "NSE",
    ) -> Optional[str]:
        tx = "SELL" if str(direction or "").upper() == "LONG" else "BUY"
        return self.place_limit_order(symbol, token, tx, price, quantity, exchange=exchange)

    def place_stoploss_market_order(
        self,
        symbol: str,
        token: str,
        direction: str,
        trigger_price: float,
        quantity: int,
        exchange: str = "NSE",
    ) -> Optional[str]:
        if not self.ensure_login():
            return None
        if int(quantity or 0) <= 0:
            return None

        self._rate_limit()
        tx = "SELL" if str(direction or "").upper() == "LONG" else "BUY"
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": tx,
            "exchange": exchange,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": int(quantity),
            "triggerprice": round(float(trigger_price), 2),
        }
        try:
            response = self.obj.placeOrder(order_params)
        except Exception as exc:  # pragma: no cover - external API behaviour
            log.error("SL order failed: %s", exc)
            return None

        order_id = self.extract_order_id(response)
        if order_id:
            log.info("SL %s %s qty=%d trigger=%.2f id=%s", tx, symbol, quantity, trigger_price, order_id)
            return order_id
        log.error("SL order response missing id: %s", response)
        return None

    def wait_for_fill(self, order_id: str, timeout_sec: int = 30, poll_interval: int = 2) -> bool:
        order_id = str(order_id or "").strip()
        if not order_id:
            return False

        started = time.time()
        while time.time() - started < max(1, int(timeout_sec or 1)):
            status = self.get_order_status(order_id)
            if status and self.is_filled_status(status):
                return True
            if status and self.is_terminal_rejection_status(status):
                return False
            time.sleep(max(1, int(poll_interval or 1)))
        log.warning("Order %s fill timeout", order_id)
        return False

    def is_order_triggered(self, order_id: str) -> bool:
        status = self.get_order_status(order_id)
        return bool(status and self.is_filled_status(status))

    def exit_position_market(
        self,
        symbol: str,
        token: str,
        direction: str,
        quantity: int,
        exchange: str = "NSE",
    ) -> tuple[bool, str, Optional[dict[str, Any]]]:
        if not self.ensure_login():
            return False, "Not connected to SmartAPI", None

        net_qty = self.get_net_position_quantity(symbol=symbol, token=token)
        fallback_qty = int(quantity or 0)
        if net_qty == 0:
            return True, f"No open broker position for {symbol}; local trade close only.", None
        if net_qty is None and fallback_qty <= 0:
            return False, f"Unable to determine broker quantity for {symbol}.", None

        exit_qty = abs(net_qty) if net_qty not in (None, 0) else fallback_qty
        if exit_qty <= 0:
            return False, f"Computed exit quantity is invalid for {symbol}.", None

        txn = "SELL" if str(direction or "").upper() == "LONG" else "BUY"
        response, message = self.place_intraday_market_order(
            symbol=symbol,
            token=token,
            transaction_type=txn,
            quantity=exit_qty,
            exchange=exchange,
        )
        if response is None:
            return False, message, None
        order_id = self.extract_order_id(response)
        if order_id:
            return True, f"Exit market order placed ({order_id}) for {symbol} qty {exit_qty}.", response
        return True, f"Exit market order placed for {symbol} qty {exit_qty}.", response

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> tuple[bool, str]:
        if not self.ensure_login():
            return False, "Not connected to SmartAPI"
        order_id = str(order_id or "").strip()
        if not order_id:
            return False, "Missing order id"

        normalized_variety = str(variety or "NORMAL").upper()
        try:
            self.obj.cancelOrder(order_id, normalized_variety)
            return True, f"Cancelled order {order_id}"
        except Exception:
            try:
                self.obj.cancelOrder({"variety": normalized_variety, "orderid": order_id})
                return True, f"Cancelled order {order_id}"
            except Exception as exc:  # pragma: no cover - external API behaviour
                return False, f"Cancel failed for {order_id}: {exc}"

    def cancel_pending_orders_for_symbol(
        self,
        symbol: str,
        token: str,
        exclude_order_ids: set[str] | None = None,
    ) -> tuple[int, str]:
        if not self.ensure_login():
            return 0, "Not connected to SmartAPI"

        excluded = {str(value).strip() for value in (exclude_order_ids or set()) if str(value).strip()}
        target_symbol = self._normalize_symbol(symbol)
        target_token = str(token or "").strip()
        rows = self.fetch_order_book()
        cancelled = 0
        failures: list[str] = []

        for row in rows:
            order_id = str(
                row.get("orderid")
                or row.get("orderId")
                or row.get("id")
                or ""
            ).strip()
            if not order_id or order_id in excluded:
                continue

            row_symbol = self._normalize_symbol(
                row.get("tradingsymbol")
                or row.get("symbol")
                or row.get("tradingSymbol")
                or ""
            )
            row_token = str(
                row.get("symboltoken")
                or row.get("symbolToken")
                or row.get("token")
                or ""
            ).strip()
            if target_token and row_token and row_token != target_token:
                continue
            if target_symbol and row_symbol and row_symbol != target_symbol:
                continue

            status = self._normalize_order_status(
                row.get("status") or row.get("orderstatus") or row.get("orderStatus")
            )
            if not self._is_pending_status(status):
                continue

            variety = str(row.get("variety") or row.get("producttype") or "NORMAL").upper()
            ok, message = self.cancel_order(order_id=order_id, variety=variety)
            if ok:
                cancelled += 1
            else:
                failures.append(message)

        if failures:
            return cancelled, "; ".join(failures)
        return cancelled, f"Cancelled {cancelled} pending order(s) for {symbol}."

    def place_bracket_order(
        self,
        symbol: str,
        token: str,
        direction: str,
        entry_price: float,
        stop_loss_price: float,
        target_price: float,
        quantity: int,
    ):
        if not self.ensure_login():
            return None, "Not connected to SmartAPI"

        transaction_type = "BUY" if direction == "LONG" else "SELL"

        stop_loss_distance = abs(entry_price - stop_loss_price)
        target_distance = abs(target_price - entry_price)

        order_params = {
            "variety": "ROBO",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": transaction_type,
            "exchange": "NSE",
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": int(quantity),
            "price": round(entry_price, 2),
            "squareoff": round(target_distance, 2),
            "stoploss": round(stop_loss_distance, 2),
        }

        try:
            response = self.obj.placeOrderFullResponse(order_params)
            return response, "Order placed"
        except Exception as exc:  # pragma: no cover - external API behaviour
            return None, f"Order error: {exc}"

    def logout(self):
        if not self.obj:
            self._clear_session()
            return

        client_code = (self.credentials.client_code or "").strip()
        try:
            if client_code:
                self.obj.terminateSession(client_code)
        except Exception:
            pass
        finally:
            self._clear_session()

    def _clear_session(self):
        self.obj = None
        self.auth_token = ""
        self.refresh_token = ""
        self.feed_token = ""
