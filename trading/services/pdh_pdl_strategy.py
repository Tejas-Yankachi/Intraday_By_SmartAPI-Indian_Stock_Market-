from __future__ import annotations

from datetime import date
from typing import Optional, Tuple

import pandas as pd

from .orb_strategy import ORBStrategy, StrategyConfig


class PDHBreakoutStrategy(ORBStrategy):
    """
    PDH/PDL breakout variant of the shared breakout engine.

    The class keeps the existing ORBStrategy interface so the dashboard and
    session runner can swap implementations without changing their call sites.
    """

    @staticmethod
    def _normalized_frame(data_5min: pd.DataFrame | None) -> pd.DataFrame:
        if data_5min is None or data_5min.empty or "datetime" not in data_5min.columns:
            return pd.DataFrame()

        frame = data_5min.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame = frame.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")

        needed = [col for col in ["open", "high", "low", "close"] if col in frame.columns]
        if needed:
            frame = frame.dropna(subset=needed)
        return frame

    @staticmethod
    def _previous_day_frame(frame: pd.DataFrame, reference_date: date | None = None) -> pd.DataFrame:
        if frame is None or frame.empty or "datetime" not in frame.columns:
            return pd.DataFrame()

        if reference_date is None:
            latest_day = frame["datetime"].dt.date.iloc[-1]
            previous_days = frame[frame["datetime"].dt.date < latest_day]
        else:
            previous_days = frame[frame["datetime"].dt.date < reference_date]
        if previous_days.empty:
            return pd.DataFrame()

        previous_day = previous_days["datetime"].dt.date.max()
        return previous_days[previous_days["datetime"].dt.date == previous_day].copy()

    def _pdh_pdl(
        self,
        data_5min: pd.DataFrame | None,
        reference_date: date | None = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        frame = self._normalized_frame(data_5min)
        prev_day = self._previous_day_frame(frame, reference_date=reference_date)
        if prev_day.empty:
            return None, None

        return float(prev_day["high"].max()), float(prev_day["low"].min())

    def identify_opening_range(
        self,
        data_5min: pd.DataFrame,
        reference_date: date | None = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        # Compatibility name: in the PDH/PDL engine this returns the prior day's levels.
        return self._pdh_pdl(data_5min, reference_date=reference_date)

    def get_market_bias(self, nifty_data: pd.DataFrame, reference_date: date | None = None) -> str:
        frame = self._normalized_frame(nifty_data)
        if frame.empty or len(frame) < 2:
            return "NEUTRAL"

        pdh, pdl = self._pdh_pdl(frame, reference_date=reference_date)
        if pdh is None or pdl is None:
            return "NEUTRAL"

        current_price = float(frame["close"].iloc[-1])
        if current_price > pdh:
            return "BULLISH"
        if current_price < pdl:
            return "BEARISH"
        return "NEUTRAL"

    def _candle_body_ok(self, candle: pd.Series, direction: str) -> bool:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        candle_range = high - low
        if candle_range <= 0:
            return False

        body_floor = 1.0 - float(self.config.candle_body_min)
        if direction == "LONG":
            return ((close - low) / candle_range) >= body_floor
        return ((high - close) / candle_range) >= body_floor

    def _volume_ok(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 20 or "volume" not in frame.columns:
            return False

        volume_ma = frame["volume"].rolling(window=20).mean().iloc[-1]
        current_volume = float(frame["volume"].iloc[-1])
        if pd.isna(volume_ma) or volume_ma <= 0:
            return False

        relative_ok = current_volume / float(volume_ma) >= float(self.config.volume_threshold)
        absolute_ok = current_volume >= float(self.config.volume_absolute_min)
        return bool(relative_ok or absolute_ok)

    def _atr_ok(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 14:
            return False

        high_low = frame["high"] - frame["low"]
        high_close = (frame["high"] - frame["close"].shift()).abs()
        low_close = (frame["low"] - frame["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=14).mean().iloc[-1]
        current_price = float(frame["close"].iloc[-1])
        if pd.isna(atr) or current_price <= 0:
            return False

        atr_percent = (float(atr) / current_price) * 100
        return atr_percent >= float(self.config.atr_threshold_percent)

    def _retest_confirmed(self, frame: pd.DataFrame, level: float, direction: str) -> bool:
        if not bool(self.config.retest_mode):
            return True
        if len(frame) < 3 or level <= 0:
            return False

        end_idx = len(frame) - 1
        start_idx = max(0, end_idx - int(self.config.retest_window))
        window = frame.iloc[start_idx:end_idx]
        if window.empty:
            return False

        tolerance = float(self.config.retest_tolerance)
        for _, candle in window.iterrows():
            if direction == "LONG":
                low = float(candle["low"])
                if abs(low - level) / level * 100 <= tolerance:
                    return True
            else:
                high = float(candle["high"])
                if abs(high - level) / level * 100 <= tolerance:
                    return True
        return False

    def evaluate_entry(
        self,
        data_5min: pd.DataFrame,
        nifty_15min: pd.DataFrame,
        opening_high: float,
        opening_low: float,
        current_time,
        reference_date: date | None = None,
        client=None,
    ) -> Tuple[Optional[str], Optional[float], str]:
        self.last_entry_meta = {}

        if not self.check_time_window(current_time):
            return None, None, "Outside trade window"

        completed_data = self._completed_candles(data_5min)
        if completed_data is None or completed_data.empty or len(completed_data) < 50:
            return None, None, "No completed 5-minute bracket candle yet"

        frame = self._normalized_frame(completed_data)
        if frame.empty or len(frame) < 2:
            return None, None, "Insufficient price data"

        bracket_candle = frame.iloc[-1]
        current_open = float(bracket_candle["open"])
        previous_close = float(frame.iloc[-2]["close"])
        current_close = float(bracket_candle["close"])

        gap_percent = 0.0
        if previous_close > 0:
            gap_percent = abs(current_open - previous_close) / previous_close * 100
        if current_open > previous_close:
            gap_type = "GAP_UP"
        elif current_open < previous_close:
            gap_type = "GAP_DOWN"
        else:
            gap_type = "NO_GAP"

        gap_skip = gap_percent > float(self.config.gap_skip_percent)
        gap_action = "Skip" if gap_skip else "Full Size"
        gap_status = "LARGE" if gap_skip else ("OK" if gap_type == "NO_GAP" else "MODERATE")
        gap_multiplier = 0.0 if gap_skip else 1.0

        market_bias = self.get_market_bias(nifty_15min, reference_date=reference_date)
        if market_bias == "NEUTRAL":
            self.last_entry_meta = {
                "gap_type": gap_type,
                "gap_percent": round(gap_percent, 2),
                "gap_status": gap_status,
                "gap_action": gap_action,
                "gap_multiplier": gap_multiplier,
                "position_size_multiplier": 0.0,
                "trend_15m": market_bias,
                "trend_strength": 0.0,
                "trend_distance_percent": 0.0,
                "trend_slope_percent": 0.0,
                "trend_status": "FAIL",
                "trend_message": "Neutral market bias",
            }
            return None, None, "Neutral market bias"

        direction: Optional[str] = None
        breakout_level: Optional[float] = None
        reason = ""
        breakout_percent = 0.0

        if market_bias == "BULLISH" and opening_high is not None and float(opening_high) > 0 and current_close > float(opening_high):
            direction = "LONG"
            breakout_level = float(opening_high)
            breakout_percent = ((current_close - breakout_level) / breakout_level) * 100
            reason = "Long breakout above PDH"
        elif market_bias == "BEARISH" and opening_low is not None and float(opening_low) > 0 and current_close < float(opening_low):
            direction = "SHORT"
            breakout_level = float(opening_low)
            breakout_percent = ((breakout_level - current_close) / breakout_level) * 100
            reason = "Short breakout below PDL"

        if direction is None or breakout_level is None:
            self.last_entry_meta = {
                "gap_type": gap_type,
                "gap_percent": round(gap_percent, 2),
                "gap_status": gap_status,
                "gap_action": gap_action,
                "gap_multiplier": gap_multiplier,
                "position_size_multiplier": 0.0,
                "trend_15m": market_bias,
                "trend_strength": 0.0,
                "trend_distance_percent": 0.0,
                "trend_slope_percent": 0.0,
                "trend_status": "FAIL",
                "trend_message": "No breakout beyond PDH/PDL",
            }
            return None, None, "No breakout beyond PDH/PDL"

        if not self._retest_confirmed(frame, breakout_level, direction):
            self.last_entry_meta = {
                "gap_type": gap_type,
                "gap_percent": round(gap_percent, 2),
                "gap_status": gap_status,
                "gap_action": gap_action,
                "gap_multiplier": gap_multiplier,
                "position_size_multiplier": 0.0,
                "trend_15m": market_bias,
                "trend_strength": 0.0,
                "trend_distance_percent": 0.0,
                "trend_slope_percent": 0.0,
                "trend_status": "FAIL",
                "trend_message": "No retest confirmation",
            }
            return None, None, "No retest confirmation"

        if not self._candle_body_ok(bracket_candle, direction):
            return None, None, "Candle body filter failed"
        if not self.check_ema_alignment(frame, direction):
            return None, None, "EMA trend filter failed"
        if not self._volume_ok(frame):
            return None, None, "Volume filter failed"
        if not self._atr_ok(frame):
            return None, None, "ATR filter failed"

        self.last_entry_meta = {
            "gap_type": gap_type,
            "gap_percent": round(gap_percent, 2),
            "gap_status": gap_status,
            "gap_action": gap_action,
            "gap_multiplier": gap_multiplier,
            "previous_close": round(previous_close, 2),
            "market_bias": market_bias,
            "trend_15m": market_bias,
            "trend_strength": 100.0,
            "trend_distance_percent": round(breakout_percent, 2),
            "trend_slope_percent": 0.0,
            "trend_status": "PASS",
            "trend_message": reason,
            "position_size_multiplier": gap_multiplier,
            "breakout_percent": round(breakout_percent, 2),
            "signal_entry_price": round(current_close, 2),
            "retest_confirmed": True,
        }
        return direction, current_close, reason

    def get_trailing_stop(self, direction: str, current_stop: float, current_data: pd.DataFrame) -> float:
        completed = self._completed_candles(current_data)
        if completed is None or completed.empty or len(completed) < 2:
            return float(current_stop)

        previous_candle = completed.iloc[-2]
        if direction == "LONG":
            candidate = float(previous_candle["low"])
            return max(float(current_stop), candidate)

        candidate = float(previous_candle["high"])
        return min(float(current_stop), candidate)

    def calculate_target_price(self, entry_price: float, stop_loss_price: float, position_size: int) -> float:
        risk_per_share = abs(entry_price - stop_loss_price)
        if position_size <= 0 or risk_per_share <= 0:
            return entry_price

        if entry_price > stop_loss_price:
            return entry_price + (risk_per_share * float(self.config.full_target_r))
        return entry_price - (risk_per_share * float(self.config.full_target_r))
