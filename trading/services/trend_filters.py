from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class OptimalTrendFilter:
    ema_period: int = 50
    buffer_percent: float = 1.0
    min_candles: int = 30
    slope_period: int = 5
    strong_threshold: float = 0.5
    weak_threshold: float = 0.2
    slope_threshold: float = 0.1
    fallback_enabled: bool = True

    @staticmethod
    def _as_float(value) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _status_label(allowed: bool, strength: float) -> str:
        if not allowed:
            return "FAIL"
        if strength >= 70:
            return "PASS"
        return "CAUTION"

    def _unknown_status(self, message: str, should_trade: bool = False) -> dict[str, Any]:
        return {
            "trend": "UNKNOWN",
            "strength": 0.0,
            "slope": 0.0,
            "distance_percent": 0.0,
            "current_price": None,
            "current_ema": None,
            "should_trade": bool(should_trade),
            "allowed_direction": None,
            "trend_status": "FAIL" if not should_trade else "CAUTION",
            "message": message,
        }

    def fetch_15min_data(
        self,
        client,
        symbol: str = "NIFTY",
        token: str = "13",
        days: int = 5,
    ) -> pd.DataFrame | None:
        if client is None:
            return None

        try:
            df = client.fetch_historical_data(
                token=str(token),
                interval="FIFTEEN_MINUTE",
                days=int(days),
                exchange="NSE",
            )
            if df is not None and not df.empty:
                return df.sort_values("datetime").reset_index(drop=True)
        except Exception:
            pass

        return None

    def calculate_indicators(self, df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or len(df) < int(self.min_candles):
            return None

        frame = df.copy()
        if "datetime" in frame.columns:
            frame = frame.sort_values("datetime")

        if "close" not in frame.columns:
            return None

        if "high" not in frame.columns:
            frame["high"] = frame["close"]
        if "low" not in frame.columns:
            frame["low"] = frame["close"]

        for col in ["open", "high", "low", "close", "volume"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["high", "low", "close"])
        if len(frame) < int(self.min_candles):
            return None

        frame["ema_50"] = frame["close"].ewm(span=int(self.ema_period), adjust=False).mean()
        if len(frame) >= int(self.slope_period):
            frame["ema_slope"] = (
                frame["ema_50"]
                .pct_change(periods=int(self.slope_period))
                .mul(100)
            )
        else:
            frame["ema_slope"] = 0.0
        frame["ema_slope"] = frame["ema_slope"].fillna(0.0)
        frame["distance_percent"] = (
            (frame["close"] - frame["ema_50"]) / frame["ema_50"]
        ).mul(100)

        high_low = frame["high"] - frame["low"]
        high_close = (frame["high"] - frame["close"].shift()).abs()
        low_close = (frame["low"] - frame["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        frame["atr"] = tr.rolling(window=14).mean()
        frame["atr_percent"] = (frame["atr"] / frame["close"]).mul(100)
        return frame

    def get_trend_message(self, trend: str, distance: float, slope: float) -> str:
        if trend == "STRONG_BULLISH":
            return (
                f"Strong uptrend: {distance:.2f}% above 50 EMA "
                f"with {slope:.2f}% slope."
            )
        if trend == "WEAK_BULLISH":
            return (
                f"Weak uptrend: {distance:.2f}% above 50 EMA "
                f"with flattening slope {slope:.2f}%."
            )
        if trend == "STRONG_BEARISH":
            return (
                f"Strong downtrend: {abs(distance):.2f}% below 50 EMA "
                f"with {slope:.2f}% slope."
            )
        if trend == "WEAK_BEARISH":
            return (
                f"Weak downtrend: {abs(distance):.2f}% below 50 EMA "
                f"with flattening slope {slope:.2f}%."
            )
        if trend == "SIDEWAYS":
            return f"Sideways market: {abs(distance):.2f}% from 50 EMA."
        return f"Trend unclear: {distance:.2f}% from 50 EMA."

    def get_trend_status(self, df: pd.DataFrame | None) -> dict[str, Any]:
        indicators = self.calculate_indicators(df)
        if indicators is None or len(indicators) < int(self.min_candles):
            return self._unknown_status("Insufficient data for 15-minute trend filter.")

        latest = indicators.iloc[-1]
        current_price = self._as_float(latest.get("close"))
        current_ema = self._as_float(latest.get("ema_50"))
        current_slope = self._as_float(latest.get("ema_slope")) or 0.0
        distance = self._as_float(latest.get("distance_percent")) or 0.0

        if current_price is None or current_ema in (None, 0):
            return self._unknown_status("Invalid 15-minute trend values.")

        trend = "SIDEWAYS"
        strength = 0.0
        allowed_direction: str | None = None
        should_trade = False

        if abs(distance) <= float(self.buffer_percent):
            trend = "SIDEWAYS"
            strength = max(0.0, 20.0 - (abs(distance) * 10.0))
            should_trade = False
            allowed_direction = None
        elif distance > 0:
            allowed_direction = "LONG"
            should_trade = True
            if current_slope > float(self.slope_threshold) and distance >= float(self.strong_threshold):
                trend = "STRONG_BULLISH"
                strength = min(100.0, 70.0 + (abs(distance) * 8.0))
            elif current_slope >= -float(self.slope_threshold):
                trend = "WEAK_BULLISH"
                weak_base = 40.0 if abs(distance) <= float(self.weak_threshold) else 45.0
                strength = min(100.0, weak_base + (abs(distance) * 6.0))
            else:
                trend = "BULLISH_DIVERGENCE"
                strength = 30.0
        else:
            allowed_direction = "SHORT"
            should_trade = True
            if current_slope < -float(self.slope_threshold) and abs(distance) >= float(self.strong_threshold):
                trend = "STRONG_BEARISH"
                strength = min(100.0, 70.0 + (abs(distance) * 8.0))
            elif current_slope <= float(self.slope_threshold):
                trend = "WEAK_BEARISH"
                weak_base = 40.0 if abs(distance) <= float(self.weak_threshold) else 45.0
                strength = min(100.0, weak_base + (abs(distance) * 6.0))
            else:
                trend = "BEARISH_DIVERGENCE"
                strength = 30.0

        message = self.get_trend_message(trend, distance, current_slope)
        return {
            "trend": trend,
            "strength": round(float(strength), 2),
            "slope": round(float(current_slope), 4),
            "distance_percent": round(float(distance), 2),
            "current_price": round(float(current_price), 2),
            "current_ema": round(float(current_ema), 2),
            "should_trade": bool(should_trade),
            "allowed_direction": allowed_direction,
            "trend_status": self._status_label(bool(should_trade), float(strength)),
            "message": message,
        }

    def check_trend_for_direction(self, df: pd.DataFrame | None, direction: str) -> tuple[bool, dict[str, Any]]:
        status = self.get_trend_status(df)
        normalized_direction = str(direction or "").upper()

        if not status.get("should_trade"):
            status["trend_status"] = "FAIL"
            return False, status

        allowed = status.get("allowed_direction") == normalized_direction
        status["trend_status"] = self._status_label(bool(allowed), float(status.get("strength", 0.0)))
        return bool(allowed), status

    def get_trend_strength_score(self, df: pd.DataFrame | None) -> float:
        status = self.get_trend_status(df)
        return float(status.get("strength", 0.0))

    def get_position_size_multiplier(self, df: pd.DataFrame | None) -> float:
        strength = self.get_trend_strength_score(df)
        if strength >= 70:
            return 1.0
        if strength >= 40:
            return 0.75
        if strength >= 20:
            return 0.5
        return 0.25

    def check_5min_fallback(
        self,
        client,
        direction: str,
        data_5min: pd.DataFrame | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        if client is None:
            status = self._unknown_status("5-minute fallback unavailable (missing client).", should_trade=True)
            status.update(
                {
                    "trend": "UNKNOWN (fallback)",
                    "strength": 25.0,
                    "allowed_direction": str(direction or "").upper() or None,
                    "trend_status": "CAUTION",
                    "message": "No trend data available. Trading with caution.",
                }
            )
            return True, status

        if data_5min is None:
            try:
                data_5min = client.fetch_historical_data(
                    token="13",
                    interval="FIVE_MINUTE",
                    days=2,
                    exchange="NSE",
                )
            except Exception:
                data_5min = None

        if data_5min is not None and len(data_5min) >= 50:
            ema_50 = data_5min["close"].ewm(span=50, adjust=False).mean()
            current_price = self._as_float(data_5min["close"].iloc[-1])
            current_ema = self._as_float(ema_50.iloc[-1])
            if current_price not in (None, 0) and current_ema not in (None, 0):
                normalized_direction = str(direction or "").upper()
                if normalized_direction == "LONG":
                    allowed = current_price > current_ema
                    trend = "BULLISH (5m fallback)" if allowed else "BEARISH (5m fallback)"
                else:
                    allowed = current_price < current_ema
                    trend = "BEARISH (5m fallback)" if allowed else "BULLISH (5m fallback)"

                strength = 50.0 if allowed else 0.0
                distance = ((current_price - current_ema) / current_ema) * 100
                status = {
                    "trend": trend,
                    "strength": strength,
                    "slope": 0.0,
                    "distance_percent": round(distance, 2),
                    "current_price": round(current_price, 2),
                    "current_ema": round(current_ema, 2),
                    "should_trade": bool(allowed),
                    "allowed_direction": "LONG" if current_price > current_ema else "SHORT",
                    "trend_status": self._status_label(bool(allowed), strength),
                    "message": (
                        f"5m fallback: price {current_price:.2f} vs EMA50 {current_ema:.2f}"
                    ),
                }
                return bool(allowed), status

        status = self._unknown_status("No trend data available. Trading with caution.", should_trade=True)
        status.update(
            {
                "trend": "UNKNOWN (fallback)",
                "strength": 25.0,
                "allowed_direction": str(direction or "").upper() or None,
                "trend_status": "CAUTION",
                "message": "No trend data available. Trading with caution.",
            }
        )
        return True, status

    def check_15min_trend_with_fallback(self, client, direction: str) -> tuple[bool, dict[str, Any]]:
        data_15min = self.fetch_15min_data(client=client)
        if data_15min is not None and len(data_15min) >= int(self.min_candles):
            return self.check_trend_for_direction(data_15min, direction)

        if not bool(self.fallback_enabled):
            return False, self._unknown_status("Insufficient 15m data and fallback is disabled.")

        allowed, status = self.check_5min_fallback(client, direction)
        status["message"] = f"15m data insufficient. {status.get('message', '')}".strip()
        return allowed, status
