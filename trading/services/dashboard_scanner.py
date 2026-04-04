from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from threading import Lock
from typing import Any

import pandas as pd
from django.utils import timezone

from trading.models import WatchlistSymbol

from .orb_strategy import StrategyConfig
from .pdh_pdl_strategy import PDHBreakoutStrategy
from .smartapi_gateway import SmartAPIClient

_SCAN_CACHE: dict[int, dict[str, Any]] = {}
_SCAN_CACHE_LOCK = Lock()
_CACHE_TTL_SUCCESS_SECONDS = 15
_CACHE_TTL_FAILURE_SECONDS = 25
HISTORICAL_LOOKBACK_DAYS = 7

INDEX_INSTRUMENTS = [
    {"label": "NIFTY 50", "symbol": "Nifty 50", "token": "99926000", "exchange": "NSE"},
    {"label": "BANK NIFTY", "symbol": "Nifty Bank", "token": "99926009", "exchange": "NSE"},
    {"label": "FIN NIFTY", "symbol": "Nifty Fin Service", "token": "99926037", "exchange": "NSE"},
    {"label": "MIDCAP SELECT", "symbol": "NIFTY MID SELECT", "token": "99926074", "exchange": "NSE"},
]


def _build_strategy(profile) -> PDHBreakoutStrategy:
    return PDHBreakoutStrategy(
        config=StrategyConfig(
            capital=Decimal(profile.capital),
            risk_per_trade_percent=Decimal(profile.risk_per_trade_percent),
            min_risk_amount=Decimal(profile.min_risk_amount),
            brokerage_per_trade=Decimal(profile.brokerage_per_trade),
            trade_start_time=profile.trade_start_time,
            trade_end_time=profile.trade_end_time,
            exit_time=profile.exit_time,
        )
    )


def _format_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _time_to_seconds(now_time: time, target_time: time) -> int:
    now_dt = datetime.combine(datetime.today(), now_time)
    target_dt = datetime.combine(datetime.today(), target_time)
    return int((target_dt - now_dt).total_seconds())


def _trade_window_state(now_time: time, start_time: time, end_time: time) -> tuple[str, bool]:
    if now_time < start_time:
        return f"Starts in {_format_hms(_time_to_seconds(now_time, start_time))}", False
    if now_time <= end_time:
        return f"{_format_hms(_time_to_seconds(now_time, end_time))} left", True
    return "Closed", False


def _safe_number(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _atr_metrics(data_5min: pd.DataFrame, atr_threshold_percent: float) -> tuple[float | None, float | None, bool]:
    if data_5min is None or len(data_5min) < 14:
        return None, None, False

    high_low = data_5min["high"] - data_5min["low"]
    high_close = (data_5min["high"] - data_5min["close"].shift()).abs()
    low_close = (data_5min["low"] - data_5min["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr_series = tr.rolling(window=14).mean()
    atr_value = _safe_number(atr_series.iloc[-1])
    close_price = _safe_number(data_5min["close"].iloc[-1])
    if atr_value is None or close_price in (None, 0):
        return None, None, False
    atr_percent = (atr_value / close_price) * 100
    return atr_value, atr_percent, atr_percent >= float(atr_threshold_percent)


def _opening_range(data_5min: pd.DataFrame, reference_date=None) -> tuple[float | None, float | None]:
    if data_5min is None or data_5min.empty:
        return None, None

    frame = data_5min.sort_values("datetime").copy()
    if reference_date is None:
        latest_day = frame["datetime"].dt.date.iloc[-1]
        previous_days = frame[frame["datetime"].dt.date < latest_day]
    else:
        previous_days = frame[frame["datetime"].dt.date < reference_date]
    if previous_days.empty:
        return None, None

    previous_day = previous_days["datetime"].dt.date.max()
    day_data = previous_days[previous_days["datetime"].dt.date == previous_day]
    if day_data.empty:
        return None, None

    return _safe_number(day_data["high"].max()), _safe_number(day_data["low"].min())


def _completed_5min_frame(data_5min: pd.DataFrame, now_local) -> pd.DataFrame:
    if data_5min is None or data_5min.empty or "datetime" not in data_5min.columns:
        return pd.DataFrame()

    frame = data_5min.sort_values("datetime").copy()
    series = frame["datetime"]
    series_tz = getattr(series.dt, "tz", None)
    now_ts = pd.Timestamp(now_local)
    if series_tz is None and now_ts.tzinfo is not None:
        now_ts = now_ts.tz_localize(None)
    elif series_tz is not None and now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize(series_tz)
    elif series_tz is not None and now_ts.tzinfo is not None:
        now_ts = now_ts.tz_convert(series_tz)

    cutoff = now_ts - pd.Timedelta(minutes=5)
    return frame[frame["datetime"] <= cutoff]


def _previous_day_close(data_5min: pd.DataFrame, reference_date=None) -> float | None:
    if data_5min is None or data_5min.empty or "datetime" not in data_5min.columns:
        return None

    frame = data_5min.sort_values("datetime").copy()
    if frame.empty:
        return None

    if reference_date is None:
        latest_day = frame["datetime"].dt.date.iloc[-1]
        previous_day_rows = frame[frame["datetime"].dt.date < latest_day]
    else:
        previous_day_rows = frame[frame["datetime"].dt.date < reference_date]
    if previous_day_rows.empty:
        return None

    return _safe_number(previous_day_rows["close"].iloc[-1])


def _ema_stack_metrics(data_5min: pd.DataFrame, require_full_stack: bool) -> dict[str, Any]:
    if data_5min is None or len(data_5min) < 50:
        return {
            "ema_9": None,
            "ema_21": None,
            "ema_50": None,
            "long_ok": False,
            "short_ok": False,
        }

    ema_9 = data_5min["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema_21 = data_5min["close"].ewm(span=21, adjust=False).mean().iloc[-1]
    ema_50 = data_5min["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    price = data_5min["close"].iloc[-1]
    if require_full_stack:
        long_ok = bool(price > ema_9 > ema_21 > ema_50)
        short_ok = bool(price < ema_9 < ema_21 < ema_50)
    else:
        long_ok = bool(price > ema_50)
        short_ok = bool(price < ema_50)
    return {
        "ema_9": round(float(ema_9), 2),
        "ema_21": round(float(ema_21), 2),
        "ema_50": round(float(ema_50), 2),
        "long_ok": long_ok,
        "short_ok": short_ok,
    }


def _volume_metrics(
    data_5min: pd.DataFrame,
    volume_threshold: float,
    volume_absolute_min: float,
) -> tuple[float | None, bool]:
    if data_5min is None or len(data_5min) < 20:
        return None, False

    volume_ma20 = data_5min["volume"].rolling(window=20).mean().iloc[-1]
    current_volume = _safe_number(data_5min["volume"].iloc[-1])
    base_volume = _safe_number(volume_ma20)
    if current_volume is None or base_volume in (None, 0):
        return None, False

    ratio = current_volume / base_volume
    min_required = max(float(volume_threshold) * base_volume, float(volume_absolute_min))
    return ratio, current_volume > min_required


def _market_context(
    strategy: PDHBreakoutStrategy,
    nifty_15min: pd.DataFrame | None,
    reference_date=None,
) -> dict[str, Any]:
    if nifty_15min is None or nifty_15min.empty:
        return {
            "nifty_ltp": None,
            "market_bias": "NEUTRAL",
            "ema_50_slope": None,
            "updated_at": timezone.localtime().strftime("%H:%M:%S"),
        }

    bias = strategy.get_market_bias(nifty_15min, reference_date=reference_date)
    nifty_ltp = _safe_number(nifty_15min["close"].iloc[-1])
    ema_50 = nifty_15min["close"].ewm(span=50, adjust=False).mean()
    slope = None
    if len(ema_50) >= 6:
        slope = _safe_number(ema_50.iloc[-1] - ema_50.iloc[-6])
    return {
        "nifty_ltp": round(nifty_ltp, 2) if nifty_ltp is not None else None,
        "market_bias": bias,
        "ema_50_slope": round(slope, 4) if slope is not None else None,
        "updated_at": timezone.localtime().strftime("%H:%M:%S"),
    }


def _indices_snapshot(client: SmartAPIClient, fallback_nifty_ltp: float | None) -> list[dict[str, Any]]:
    quotes = client.fetch_market_quotes(INDEX_INSTRUMENTS, allow_ltp_fallback=True)
    rows: list[dict[str, Any]] = []
    for item in INDEX_INSTRUMENTS:
        quote = quotes.get(item["token"], {})
        ltp = _safe_number(quote.get("ltp"))
        if item["token"] == "99926000" and ltp is None:
            ltp = fallback_nifty_ltp
        rows.append(
            {
                "label": item["label"],
                "ltp": round(ltp, 2) if ltp is not None else None,
                "updated_at": timezone.localtime().strftime("%H:%M:%S"),
            }
        )
    return rows


def _empty_snapshot(message: str) -> dict[str, Any]:
    now = timezone.localtime()
    return {
        "api_connection": {
            "active": False,
            "status": "INACTIVE",
            "message": message,
            "checked_at": now.strftime("%H:%M:%S"),
        },
        "market_context": {
            "nifty_ltp": None,
            "market_bias": "NEUTRAL",
            "ema_50_slope": None,
            "updated_at": now.strftime("%H:%M:%S"),
            "window_timer": "Closed",
        },
        "indices": [],
        "funds": {
            "total_funds": None,
            "available_funds": None,
            "utilized_margin": None,
            "margin_utilized_percent": None,
        },
        "signal_table": [],
        "opening_ranges": [],
        "price_map": {},
    }


def _compute_live_snapshot(user) -> tuple[dict[str, Any], int]:
    watchlist = list(WatchlistSymbol.objects.filter(enabled=True).order_by("priority", "symbol"))
    if not watchlist:
        return _empty_snapshot("No watchlist symbols are enabled."), _CACHE_TTL_FAILURE_SECONDS

    profile = user.trading_profile
    strategy = _build_strategy(profile)
    now_local = timezone.localtime()
    effective_start_time = strategy.effective_trade_start_time()
    window_timer, window_open = _trade_window_state(
        now_local.time(),
        effective_start_time,
        profile.trade_end_time,
    )

    client = SmartAPIClient.from_user_profile(profile)
    if not client.credentials.is_valid():
        base_message = "SmartAPI credentials are incomplete in profile."
        if not profile.is_live_trading_enabled:
            base_message = "Paper mode active. Add SmartAPI credentials to enable live market feeds."
        payload = _empty_snapshot(base_message)
        payload["market_context"]["window_timer"] = window_timer
        return payload, _CACHE_TTL_FAILURE_SECONDS

    connected, message = client.validate_login()
    if not connected:
        if not profile.is_live_trading_enabled:
            message = f"Paper mode active. {message}"
        payload = _empty_snapshot(message)
        payload["market_context"]["window_timer"] = window_timer
        return payload, _CACHE_TTL_FAILURE_SECONDS

    instruments = [
        {"symbol": row.symbol, "token": row.token, "exchange": row.exchange}
        for row in watchlist
    ]
    quote_map = client.fetch_market_quotes(instruments, allow_ltp_fallback=False)

    nifty_15min = client.fetch_historical_data(token="13", interval="FIFTEEN_MINUTE", days=7)
    market_context = _market_context(strategy, nifty_15min, now_local.date())
    market_context["window_timer"] = window_timer
    market_bias = market_context["market_bias"]
    trend_filter = strategy.trend_filter
    fallback_5min = None
    if nifty_15min is not None and len(nifty_15min) >= int(strategy.config.trend_min_candles):
        trend_long_ok, trend_long_status = trend_filter.check_trend_for_direction(nifty_15min, "LONG")
        trend_short_ok, trend_short_status = trend_filter.check_trend_for_direction(nifty_15min, "SHORT")
    elif bool(strategy.config.trend_fallback_enabled):
        fallback_5min = client.fetch_historical_data(token="13", interval="FIVE_MINUTE", days=HISTORICAL_LOOKBACK_DAYS)
        trend_long_ok, trend_long_status = trend_filter.check_5min_fallback(
            client,
            "LONG",
            data_5min=fallback_5min,
        )
        trend_short_ok, trend_short_status = trend_filter.check_5min_fallback(
            client,
            "SHORT",
            data_5min=fallback_5min,
        )
    else:
        trend_long_ok, trend_short_ok = False, False
        trend_long_status = trend_filter.get_trend_status(nifty_15min)
        trend_short_status = dict(trend_long_status)
        trend_short_status["trend_status"] = "FAIL"
    indices = _indices_snapshot(client, market_context.get("nifty_ltp"))
    funds = client.fetch_fund_summary() or {
        "total_funds": None,
        "available_funds": None,
        "utilized_margin": None,
        "margin_utilized_percent": None,
    }

    signal_table = []
    opening_ranges = []
    price_map: dict[str, float] = {}

    for row in watchlist:
        data_5min = client.fetch_historical_data(
            token=row.token,
            interval="FIVE_MINUTE",
            days=HISTORICAL_LOOKBACK_DAYS,
            exchange=row.exchange,
        )

        if data_5min is None or data_5min.empty:
            signal_table.append(
                {
                    "symbol": row.symbol,
                    "signal": "NO SIGNAL",
                    "signal_color": "NEUTRAL",
                    "ltp": None,
                    "bid": None,
                    "ask": None,
                    "volume": None,
                    "breakout_status": "NO DATA",
                    "time_remaining": window_timer,
                    "filters": {
                        "ema_stack": False,
                        "volume": False,
                        "atr": False,
                        "trend_15m": False,
                        "market_bias": False,
                        "gap": False,
                        "time_window": window_open,
                    },
                    "pdh": None,
                    "pdl": None,
                    "or_high": None,
                    "or_low": None,
                    "previous_close": None,
                    "gap_type": "UNKNOWN",
                    "gap_percent": None,
                    "gap_status": "UNKNOWN",
                    "gap_action": "Skip",
                    "indicators": {
                        "ema_9": None,
                        "ema_21": None,
                        "ema_50": None,
                        "volume_ratio": None,
                        "atr": None,
                        "atr_percent": None,
                        "trend_15m": "UNKNOWN",
                        "trend_strength": None,
                        "trend_distance_percent": None,
                        "trend_slope_percent": None,
                        "trend_status": "FAIL",
                    },
                }
            )
            opening_ranges.append(
                {
                    "symbol": row.symbol,
                    "pdh": None,
                    "pdl": None,
                    "opening_high": None,
                    "opening_low": None,
                    "range_points": None,
                    "range_percent": None,
                    "breakout_status": "NO DATA",
                }
            )
            continue

        quote = quote_map.get(str(row.token), {})
        ltp = _safe_number(quote.get("ltp"))
        if ltp is None:
            ltp = _safe_number(data_5min["close"].iloc[-1])
        bid = _safe_number(quote.get("bid"))
        ask = _safe_number(quote.get("ask"))
        volume = quote.get("volume")
        if volume is None:
            volume = _safe_number(data_5min["volume"].iloc[-1])
        if ltp is not None:
            price_map[row.symbol] = round(float(ltp), 2)

        completed_5min = _completed_5min_frame(data_5min, now_local)
        frame_for_filters = completed_5min if not completed_5min.empty else data_5min
        opening_high, opening_low = _opening_range(frame_for_filters, reference_date=now_local.date())
        previous_close = _previous_day_close(frame_for_filters, reference_date=now_local.date())

        bracket_candle = completed_5min.iloc[-1] if not completed_5min.empty else None
        bracket_close = _safe_number(bracket_candle["close"]) if bracket_candle is not None else None
        bracket_time = bracket_candle["datetime"] if bracket_candle is not None else None
        bracket_after_or = bool(bracket_time and bracket_time.time() > time(9, 20))

        breakout_status = "WAITING"
        if bracket_after_or and bracket_close is not None and opening_high is not None and bracket_close > opening_high:
            breakout_status = "ABOVE_PDH"
        elif bracket_after_or and bracket_close is not None and opening_low is not None and bracket_close < opening_low:
            breakout_status = "BELOW_PDL"

        range_points = None
        range_percent = None
        if opening_high is not None and opening_low is not None:
            range_points = opening_high - opening_low
            range_base = bracket_close if bracket_close not in (None, 0) else ltp
            if range_base not in (None, 0):
                range_percent = (range_points / range_base) * 100

        ema = _ema_stack_metrics(
            frame_for_filters,
            require_full_stack=bool(strategy.config.ema_require_full_stack),
        )
        volume_ratio, volume_ok = _volume_metrics(
            frame_for_filters,
            volume_threshold=float(strategy.config.volume_threshold),
            volume_absolute_min=float(strategy.config.volume_absolute_min),
        )
        atr_value, atr_percent, atr_ok = _atr_metrics(
            frame_for_filters,
            atr_threshold_percent=float(strategy.config.atr_threshold_percent),
        )

        breakout_above = breakout_status == "ABOVE_PDH"
        breakout_below = breakout_status == "BELOW_PDL"
        if breakout_above:
            trend_for_row_ok = bool(trend_long_ok)
            trend_for_row_status = trend_long_status
        elif breakout_below:
            trend_for_row_ok = bool(trend_short_ok)
            trend_for_row_status = trend_short_status
        elif market_bias == "BULLISH":
            trend_for_row_ok = bool(trend_long_ok)
            trend_for_row_status = trend_long_status
        elif market_bias == "BEARISH":
            trend_for_row_ok = bool(trend_short_ok)
            trend_for_row_status = trend_short_status
        else:
            long_strength = float(trend_long_status.get("strength", 0.0) or 0.0)
            short_strength = float(trend_short_status.get("strength", 0.0) or 0.0)
            trend_for_row_status = trend_long_status if long_strength >= short_strength else trend_short_status
            trend_for_row_ok = bool(trend_for_row_status.get("should_trade"))

        trend_label = str(trend_for_row_status.get("trend", "UNKNOWN"))
        trend_strength = float(trend_for_row_status.get("strength", 0.0) or 0.0)
        trend_distance = _safe_number(trend_for_row_status.get("distance_percent"))
        trend_slope = _safe_number(trend_for_row_status.get("slope"))
        trend_filter_status = str(
            trend_for_row_status.get("trend_status", "PASS" if trend_for_row_ok else "FAIL")
        )
        trend_message = str(trend_for_row_status.get("message", "") or "")
        trend_multiplier = max(0.5, trend_strength / 100.0) if trend_for_row_ok else 0.0

        entry_direction, signal_entry_price, entry_reason = strategy.evaluate_entry(
            data_5min=frame_for_filters,
            nifty_15min=nifty_15min,
            opening_high=float(opening_high or 0.0),
            opening_low=float(opening_low or 0.0),
            current_time=now_local.time(),
            reference_date=now_local.date(),
            client=client,
        )

        gap_type, gap_percent, _ = strategy.gap_filters.detect_opening_range_gap(
            opening_high=opening_high,
            opening_low=opening_low,
            previous_close=previous_close,
        )
        gap_multiplier = strategy.gap_filters.get_position_size_multiplier(gap_type, gap_percent)
        gap_status = strategy.gap_filters.gap_status(gap_type, gap_percent)
        gap_action = strategy.gap_filters.gap_action(gap_type, gap_percent)
        gap_quality_score, gap_quality_trade_ok = strategy.gap_filters.check_gap_breakout_quality(
            opening_high=opening_high,
            opening_low=opening_low,
            previous_close=previous_close,
            current_price=bracket_close,
        )
        gap_skip = (
            strategy.gap_filters.should_skip_trade(gap_type, gap_percent)
            or gap_multiplier <= 0
            or (bool(strategy.config.gap_fill_check_required) and not gap_quality_trade_ok)
        )
        total_multiplier = 0.0
        if float(gap_multiplier) > 0 and float(trend_multiplier) > 0:
            total_multiplier = max(0.25, float(gap_multiplier) * float(trend_multiplier))

        strong_breakout_threshold = float(strategy.config.strong_breakout_threshold_percent)
        long_breakout_percent = (
            ((bracket_close - opening_high) / opening_high) * 100
            if breakout_above and bracket_close is not None and opening_high not in (None, 0)
            else 0.0
        )
        short_breakout_percent = (
            ((opening_low - bracket_close) / opening_low) * 100
            if breakout_below and bracket_close is not None and opening_low not in (None, 0)
            else 0.0
        )
        strong_breakout_long = long_breakout_percent > strong_breakout_threshold
        strong_breakout_short = short_breakout_percent > strong_breakout_threshold
        volume_pass_long = volume_ok or strong_breakout_long
        volume_pass_short = volume_ok or strong_breakout_short

        long_ready = all(
            [
                market_bias == "BULLISH",
                ema["long_ok"],
                volume_pass_long,
                atr_ok,
                trend_long_ok,
                not gap_skip,
                breakout_above,
                window_open,
            ]
        )
        short_ready = all(
            [
                market_bias == "BEARISH",
                ema["short_ok"],
                volume_pass_short,
                atr_ok,
                trend_short_ok,
                not gap_skip,
                breakout_below,
                window_open,
            ]
        )

        if long_ready:
            signal = "LONG"
            signal_color = "LONG"
            filters = {
                "ema_stack": ema["long_ok"],
                "volume": volume_pass_long,
                "atr": atr_ok,
                "trend_15m": trend_long_ok,
                "market_bias": market_bias == "BULLISH",
                "gap": not gap_skip,
                "time_window": window_open,
            }
        elif short_ready:
            signal = "SHORT"
            signal_color = "SHORT"
            filters = {
                "ema_stack": ema["short_ok"],
                "volume": volume_pass_short,
                "atr": atr_ok,
                "trend_15m": trend_short_ok,
                "market_bias": market_bias == "BEARISH",
                "gap": not gap_skip,
                "time_window": window_open,
            }
        else:
            signal = "NO SIGNAL"
            signal_color = "NEUTRAL"
            if market_bias == "BULLISH":
                direction_bias = True
                volume_filter = volume_pass_long
            elif market_bias == "BEARISH":
                direction_bias = False
                volume_filter = volume_pass_short
            else:
                direction_bias = True
                volume_filter = volume_ok or strong_breakout_long or strong_breakout_short
            filters = {
                "ema_stack": ema["long_ok"] if direction_bias else ema["short_ok"],
                "volume": volume_filter,
                "atr": atr_ok,
                "trend_15m": trend_long_ok if direction_bias else trend_short_ok,
                "market_bias": market_bias in {"BULLISH", "BEARISH"},
                "gap": not gap_skip,
                "time_window": window_open,
            }

        if entry_direction == "LONG":
            signal = "LONG"
            signal_color = "LONG"
        elif entry_direction == "SHORT":
            signal = "SHORT"
            signal_color = "SHORT"
        else:
            signal = "NO SIGNAL"
            signal_color = "NEUTRAL"

        signal_table.append(
            {
                "symbol": row.symbol,
                "signal": signal,
                "signal_color": signal_color,
                "ltp": round(ltp, 2) if ltp is not None else None,
                "bid": round(bid, 2) if bid is not None else None,
                "ask": round(ask, 2) if ask is not None else None,
                "volume": int(volume) if volume is not None else None,
                "breakout_status": breakout_status,
                "time_remaining": window_timer,
                "filters": filters,
                "pdh": round(opening_high, 2) if opening_high is not None else None,
                "pdl": round(opening_low, 2) if opening_low is not None else None,
                "or_high": round(opening_high, 2) if opening_high is not None else None,
                "or_low": round(opening_low, 2) if opening_low is not None else None,
                "previous_close": round(previous_close, 2) if previous_close is not None else None,
                "gap_type": gap_type,
                "gap_percent": round(gap_percent, 2) if gap_percent is not None else None,
                "gap_status": gap_status,
                "gap_action": gap_action,
                "indicators": {
                    "ema_9": ema["ema_9"],
                    "ema_21": ema["ema_21"],
                    "ema_50": ema["ema_50"],
                    "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
                    "atr": round(atr_value, 2) if atr_value is not None else None,
                    "atr_percent": round(atr_percent, 2) if atr_percent is not None else None,
                    "trend_15m": trend_label,
                    "trend_strength": round(trend_strength, 2),
                    "trend_distance_percent": round(trend_distance, 2) if trend_distance is not None else None,
                    "trend_slope_percent": round(trend_slope, 4) if trend_slope is not None else None,
                    "trend_status": trend_filter_status,
                    "trend_message": trend_message,
                    "strong_breakout": strong_breakout_long or strong_breakout_short,
                    "breakout_percent": round(
                        max(long_breakout_percent, short_breakout_percent),
                        2,
                    ),
                    "bracket_close": round(bracket_close, 2) if bracket_close is not None else None,
                    "bracket_time": bracket_time.strftime("%H:%M") if bracket_time is not None else None,
                    "gap_quality_score": gap_quality_score,
                    "gap_multiplier": round(float(gap_multiplier), 4),
                    "trend_multiplier": round(float(trend_multiplier), 4),
                    "position_size_multiplier": round(float(total_multiplier), 4),
                },
            }
        )

        opening_ranges.append(
            {
                "symbol": row.symbol,
                "pdh": round(opening_high, 2) if opening_high is not None else None,
                "pdl": round(opening_low, 2) if opening_low is not None else None,
                "opening_high": round(opening_high, 2) if opening_high is not None else None,
                "opening_low": round(opening_low, 2) if opening_low is not None else None,
                "range_points": round(range_points, 2) if range_points is not None else None,
                "range_percent": round(range_percent, 2) if range_percent is not None else None,
                "breakout_status": breakout_status,
            }
        )

    payload = {
        "api_connection": {
            "active": True,
            "status": "ACTIVE",
            "message": message,
            "checked_at": timezone.localtime().strftime("%H:%M:%S"),
        },
        "market_context": market_context,
        "indices": indices,
        "funds": funds,
        "signal_table": signal_table,
        "opening_ranges": opening_ranges,
        "price_map": price_map,
    }
    return payload, _CACHE_TTL_SUCCESS_SECONDS


def get_live_dashboard_snapshot(user, force_refresh: bool = False) -> dict[str, Any]:
    user_id = int(user.id)
    now = timezone.now()

    if not force_refresh:
        with _SCAN_CACHE_LOCK:
            cached = _SCAN_CACHE.get(user_id)
            if cached and cached["expires_at"] > now:
                return cached["payload"]

    try:
        payload, ttl_seconds = _compute_live_snapshot(user)
    except Exception as exc:
        payload = _empty_snapshot(f"Live scan failed: {exc}")
        ttl_seconds = _CACHE_TTL_FAILURE_SECONDS
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE[user_id] = {
            "expires_at": now + timedelta(seconds=ttl_seconds),
            "payload": payload,
        }
    return payload
