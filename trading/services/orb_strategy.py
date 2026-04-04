from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Tuple

import pandas as pd

from .gap_filters import GapFilters
from .trend_filters import OptimalTrendFilter

# ========== UNIVERSAL OPTIMIZED THRESHOLDS ==========
VOLUME_THRESHOLD = 0.7
VOLUME_ABSOLUTE_MIN = 25000
ATR_THRESHOLD_PERCENT = 0.4
EMA_REQUIRE_FULL_STACK = False
TREND_EMA_PERIOD = 50
TREND_BUFFER_PERCENT = 1.0
TREND_MIN_CANDLES = 30
TREND_STRONG_THRESHOLD = 0.5
TREND_WEAK_THRESHOLD = 0.2
TREND_SLOPE_THRESHOLD = 0.1
TREND_FALLBACK_ENABLED = True
MARKET_BIAS_BUFFER = 1.0
STRONG_BREAKOUT_THRESHOLD = 0.5
GAP_MAX_PERCENT = 1.5
GAP_MODERATE_THRESHOLD = 0.5
GAP_FILL_CHECK = True


@dataclass
class StrategyConfig:
    capital: Decimal
    risk_per_trade_percent: Decimal
    min_risk_amount: Decimal
    brokerage_per_trade: Decimal
    trade_start_time: object
    trade_end_time: object
    exit_time: object
    volume_threshold: float = VOLUME_THRESHOLD
    volume_absolute_min: int = VOLUME_ABSOLUTE_MIN
    atr_threshold_percent: float = ATR_THRESHOLD_PERCENT
    ema_require_full_stack: bool = EMA_REQUIRE_FULL_STACK
    trend_ema_period: int = TREND_EMA_PERIOD
    trend_buffer_percent: float = TREND_BUFFER_PERCENT
    trend_min_candles: int = TREND_MIN_CANDLES
    trend_strong_threshold: float = TREND_STRONG_THRESHOLD
    trend_weak_threshold: float = TREND_WEAK_THRESHOLD
    trend_slope_threshold: float = TREND_SLOPE_THRESHOLD
    trend_fallback_enabled: bool = TREND_FALLBACK_ENABLED
    market_bias_buffer_percent: float = MARKET_BIAS_BUFFER
    strong_breakout_threshold_percent: float = STRONG_BREAKOUT_THRESHOLD
    max_gap_percent: float = GAP_MAX_PERCENT
    moderate_gap_threshold_percent: float = GAP_MODERATE_THRESHOLD
    gap_fill_check_required: bool = GAP_FILL_CHECK
    entry_buffer_percent: float = 0.05
    gap_skip_percent: float = 1.0
    candle_body_min: float = 0.30
    retest_mode: bool = True
    retest_window: int = 3
    retest_tolerance: float = 0.15
    atr_stop_multiplier: float = 1.0
    full_target_r: float = 2.5
    partial_exit_r: float = 1.0
    partial_exit_pct: float = 0.50
    breakeven_r: float = 1.0
    margin_usage_percent: float = 90.0
    estimated_margin_factor: float = 0.25
    single_trade_mode: bool = True


class ORBStrategy:
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.gap_filters = GapFilters(
            max_gap_percent=float(config.max_gap_percent),
            moderate_gap_threshold=float(config.moderate_gap_threshold_percent),
            gap_fill_check=bool(config.gap_fill_check_required),
        )
        self.trend_filter = OptimalTrendFilter(
            ema_period=int(config.trend_ema_period),
            buffer_percent=float(config.trend_buffer_percent),
            min_candles=int(config.trend_min_candles),
            strong_threshold=float(config.trend_strong_threshold),
            weak_threshold=float(config.trend_weak_threshold),
            slope_threshold=float(config.trend_slope_threshold),
            fallback_enabled=bool(config.trend_fallback_enabled),
        )
        self.last_entry_meta: dict = {}
        self.current_gap_multiplier: float = 1.0
        self.current_trend_multiplier: float = 1.0

    @staticmethod
    def _completed_candles(data_5min: pd.DataFrame) -> pd.DataFrame:
        if data_5min is None or data_5min.empty or "datetime" not in data_5min.columns:
            return pd.DataFrame()

        frame = data_5min.sort_values("datetime").copy()
        series = frame["datetime"]
        series_tz = getattr(series.dt, "tz", None)
        now_ts = pd.Timestamp.now(tz=series_tz) if series_tz is not None else pd.Timestamp.now()
        cutoff = now_ts - pd.Timedelta(minutes=5)
        return frame[frame["datetime"] <= cutoff]

    @staticmethod
    def _previous_day_close(data_5min: pd.DataFrame) -> float | None:
        if data_5min is None or data_5min.empty or "datetime" not in data_5min.columns:
            return None

        frame = data_5min.sort_values("datetime").copy()
        if frame.empty:
            return None

        latest_day = frame["datetime"].dt.date.iloc[-1]
        previous_day_rows = frame[frame["datetime"].dt.date < latest_day]
        if previous_day_rows.empty:
            return None
        try:
            return float(previous_day_rows["close"].iloc[-1])
        except (TypeError, ValueError):
            return None

    def check_time_window(self, current_time) -> bool:
        start_time = self.effective_trade_start_time()
        return start_time <= current_time <= self.config.trade_end_time

    def is_exit_time(self, current_time) -> bool:
        return current_time >= self.config.exit_time

    def effective_trade_start_time(self):
        return self.config.trade_start_time

    def identify_opening_range(self, data_5min: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
        if data_5min is None or data_5min.empty or "datetime" not in data_5min.columns:
            return None, None

        frame = data_5min.sort_values("datetime").copy()
        latest_day = frame["datetime"].dt.date.iloc[-1]
        day_frame = frame[frame["datetime"].dt.date == latest_day]
        opening_data = day_frame[
            (day_frame["datetime"].dt.time >= pd.to_datetime("09:15").time())
            & (day_frame["datetime"].dt.time <= pd.to_datetime("09:20").time())
        ]

        if opening_data.empty:
            return None, None

        first_candle = opening_data.iloc[0]
        return float(first_candle["high"]), float(first_candle["low"])

    def get_market_bias(self, nifty_data: pd.DataFrame) -> str:
        if nifty_data is None or len(nifty_data) < 50:
            return "NEUTRAL"

        ema_50 = nifty_data["close"].ewm(span=50, adjust=False).mean()
        current_price = float(nifty_data["close"].iloc[-1])
        current_ema = float(ema_50.iloc[-1])

        slope_period = 10
        slope = float(ema_50.iloc[-1] - ema_50.iloc[-(slope_period + 1)]) if len(ema_50) > slope_period else 0.0

        bias_buffer = max(0.0, float(self.config.market_bias_buffer_percent)) / 100.0
        bearish_ceiling = current_ema * (1 + bias_buffer)
        bullish_floor = current_ema * (1 - bias_buffer)

        if slope > 0 and current_price >= bullish_floor:
            return "BULLISH"
        if slope < 0 and current_price <= bearish_ceiling:
            return "BEARISH"
        return "NEUTRAL"

    def check_ema_alignment(self, data: pd.DataFrame, direction: str) -> bool:
        if len(data) < 50:
            return False

        ema_9 = data["close"].ewm(span=9, adjust=False).mean()
        ema_21 = data["close"].ewm(span=21, adjust=False).mean()
        ema_50 = data["close"].ewm(span=50, adjust=False).mean()
        current_price = float(data["close"].iloc[-1])
        require_full_stack = bool(self.config.ema_require_full_stack)

        if direction == "LONG":
            if require_full_stack:
                return current_price > ema_9.iloc[-1] > ema_21.iloc[-1] > ema_50.iloc[-1]
            return current_price > ema_50.iloc[-1]

        if require_full_stack:
            return current_price < ema_9.iloc[-1] < ema_21.iloc[-1] < ema_50.iloc[-1]
        return current_price < ema_50.iloc[-1]

    def check_15min_trend(self, data: pd.DataFrame, direction: str) -> bool:
        allowed, _ = self.trend_filter.check_trend_for_direction(data, direction)
        return bool(allowed)

    def check_volume_confirmation(self, data: pd.DataFrame, breakout_index: int = -1) -> bool:
        if len(data) < 20:
            return False

        volume_ma = data["volume"].rolling(window=20).mean()
        volume = float(data["volume"].iloc[breakout_index])
        relative_threshold = self.config.volume_threshold * float(volume_ma.iloc[breakout_index])
        threshold = max(relative_threshold, float(self.config.volume_absolute_min))

        return volume > threshold

    def check_atr_filter(self, data: pd.DataFrame) -> bool:
        if len(data) < 14:
            return False

        high_low = data["high"] - data["low"]
        high_close = (data["high"] - data["close"].shift()).abs()
        low_close = (data["low"] - data["close"].shift()).abs()

        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=14).mean().iloc[-1]

        current_price = float(data["close"].iloc[-1])
        atr_percent = (float(atr) / current_price) * 100
        return atr_percent > self.config.atr_threshold_percent

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_price: float,
        size_multiplier: float | None = None,
    ) -> int:
        risk_per_share = abs(entry_price - stop_loss_price)
        if risk_per_share == 0:
            return 0

        gap_multiplier = float(getattr(self, "current_gap_multiplier", 1.0) or 0.0)
        trend_multiplier = float(getattr(self, "current_trend_multiplier", 1.0) or 0.0)
        total_multiplier = (
            float(size_multiplier)
            if size_multiplier is not None
            else gap_multiplier * trend_multiplier
        )
        if total_multiplier <= 0:
            return 0
        total_multiplier = max(0.25, total_multiplier)

        base_risk = self.config.capital * (self.config.risk_per_trade_percent / Decimal("100"))
        risk_amount = base_risk * Decimal(str(total_multiplier))
        risk_amount = max(risk_amount, self.config.min_risk_amount)

        position_size = int(float(risk_amount) / risk_per_share)
        return max(1, position_size)

    def calculate_target_price(self, entry_price: float, stop_loss_price: float, position_size: int) -> float:
        risk_per_share = abs(entry_price - stop_loss_price)
        if position_size <= 0:
            return entry_price

        cost_per_share = float(self.config.brokerage_per_trade) / position_size

        if entry_price > stop_loss_price:
            return entry_price + (risk_per_share * 1.5) + cost_per_share
        return entry_price - (risk_per_share * 1.5) - cost_per_share

    def evaluate_entry(
        self,
        data_5min: pd.DataFrame,
        nifty_15min: pd.DataFrame,
        opening_high: float,
        opening_low: float,
        current_time,
        client: Any | None = None,
    ) -> Tuple[Optional[str], Optional[float], str]:
        self.last_entry_meta = {}
        self.current_gap_multiplier = 1.0
        self.current_trend_multiplier = 1.0

        if not self.check_time_window(current_time):
            return None, None, "Outside trade window"

        completed_data = self._completed_candles(data_5min)
        if completed_data is None or len(completed_data) < 50:
            return None, None, "No completed 5-minute bracket candle yet"

        bracket_candle = completed_data.iloc[-1]
        bracket_time = bracket_candle["datetime"]
        if bracket_time.time() <= pd.to_datetime("09:20").time():
            return None, None, "Waiting for post-opening bracket candle close"

        previous_close = self._previous_day_close(completed_data)
        gap_type, gap_percent, has_gap = self.gap_filters.detect_opening_range_gap(
            opening_high=opening_high,
            opening_low=opening_low,
            previous_close=previous_close,
        )
        gap_skip = self.gap_filters.should_skip_trade(gap_type, gap_percent)
        gap_multiplier = self.gap_filters.get_position_size_multiplier(gap_type, gap_percent)
        gap_status = self.gap_filters.gap_status(gap_type, gap_percent)
        gap_action = self.gap_filters.gap_action(gap_type, gap_percent)
        current_close = float(bracket_candle["close"])
        if not (current_close > float(opening_high) or current_close < float(opening_low)):
            return None, None, "No bracket breakout beyond OR levels"
        volume_ok = self.check_volume_confirmation(completed_data)

        quality_score, gap_quality_trade_ok = self.gap_filters.check_gap_breakout_quality(
            opening_high=opening_high,
            opening_low=opening_low,
            previous_close=previous_close,
            current_price=current_close,
        )

        market_bias = self.get_market_bias(nifty_15min)
        direction_for_bias = "LONG" if market_bias == "BULLISH" else "SHORT" if market_bias == "BEARISH" else None

        trend_allowed = False
        trend_status = self.trend_filter.get_trend_status(nifty_15min)
        if direction_for_bias is not None:
            if nifty_15min is not None and len(nifty_15min) >= int(self.config.trend_min_candles):
                trend_allowed, trend_status = self.trend_filter.check_trend_for_direction(
                    nifty_15min,
                    direction_for_bias,
                )
            elif bool(self.config.trend_fallback_enabled):
                trend_allowed, trend_status = self.trend_filter.check_15min_trend_with_fallback(
                    client,
                    direction_for_bias,
                )
            else:
                trend_allowed = False
                trend_status = self.trend_filter.get_trend_status(nifty_15min)
                trend_status["message"] = "Insufficient 15m data and fallback is disabled."
                trend_status["trend_status"] = "FAIL"

        trend_strength = float(trend_status.get("strength", 0.0) or 0.0)
        trend_multiplier = max(0.5, trend_strength / 100.0) if trend_allowed else 0.0
        self.current_gap_multiplier = float(gap_multiplier)
        self.current_trend_multiplier = float(trend_multiplier)

        def _meta(
            *,
            gap_action_override: str | None = None,
            position_size_multiplier: float | None = None,
        ) -> dict[str, Any]:
            combined_multiplier = position_size_multiplier
            if combined_multiplier is None:
                raw_total = float(gap_multiplier) * float(trend_multiplier)
                combined_multiplier = max(0.25, raw_total) if raw_total > 0 else 0.0
            return {
                "gap_type": gap_type,
                "gap_percent": gap_percent,
                "gap_status": gap_status,
                "gap_action": gap_action_override or gap_action,
                "has_gap": has_gap,
                "gap_quality_score": quality_score,
                "previous_close": previous_close,
                "gap_multiplier": round(float(gap_multiplier), 4),
                "trend_15m": trend_status.get("trend"),
                "trend_strength": round(trend_strength, 2),
                "trend_distance_percent": float(trend_status.get("distance_percent", 0.0) or 0.0),
                "trend_slope_percent": float(trend_status.get("slope", 0.0) or 0.0),
                "trend_status": trend_status.get("trend_status"),
                "trend_message": trend_status.get("message"),
                "trend_allowed_direction": trend_status.get("allowed_direction"),
                "trend_should_trade": bool(trend_status.get("should_trade")),
                "trend_multiplier": round(float(trend_multiplier), 4),
                "position_size_multiplier": round(float(combined_multiplier), 4),
            }

        if market_bias == "NEUTRAL":
            self.last_entry_meta = _meta(position_size_multiplier=0.0)
            return None, None, "Neutral market bias"

        if not trend_allowed:
            self.last_entry_meta = _meta(position_size_multiplier=0.0)
            trend_label = trend_status.get("trend", "UNKNOWN")
            trend_message = trend_status.get("message", "Trend filter rejected trade.")
            return (
                None,
                None,
                f"Trend filter failed: {trend_label} ({trend_strength:.0f}) - {trend_message}",
            )

        if gap_skip or gap_multiplier <= 0:
            self.last_entry_meta = _meta(gap_action_override="Skip", position_size_multiplier=0.0)
            return None, None, f"Skipped: {gap_type} gap {gap_percent:.2f}% exceeds threshold"

        if self.config.gap_fill_check_required and not gap_quality_trade_ok:
            self.last_entry_meta = _meta()
            return None, None, f"Gap quality too low ({quality_score:.2f})"

        long_breakout_percent = ((current_close - opening_high) / opening_high * 100) if opening_high else 0.0
        short_breakout_percent = ((opening_low - current_close) / opening_low * 100) if opening_low else 0.0
        strong_breakout_threshold = float(self.config.strong_breakout_threshold_percent)
        strong_breakout_long = long_breakout_percent > strong_breakout_threshold
        strong_breakout_short = short_breakout_percent > strong_breakout_threshold
        volume_or_bypass_long = volume_ok or strong_breakout_long
        volume_or_bypass_short = volume_ok or strong_breakout_short

        if market_bias == "BULLISH" and current_close > opening_high:
            if (
                self.check_ema_alignment(completed_data, "LONG")
                and volume_or_bypass_long
                and self.check_atr_filter(completed_data)
            ):
                reason = "Long bracket candle close above ORH"
                if strong_breakout_long and not volume_ok:
                    reason = "Long bracket close above ORH (strong breakout volume bypass)"
                self.last_entry_meta = _meta()
                return "LONG", current_close, reason

        if market_bias == "BEARISH" and current_close < opening_low:
            if (
                self.check_ema_alignment(completed_data, "SHORT")
                and volume_or_bypass_short
                and self.check_atr_filter(completed_data)
            ):
                reason = "Short bracket candle close below ORL"
                if strong_breakout_short and not volume_ok:
                    reason = "Short bracket close below ORL (strong breakout volume bypass)"
                self.last_entry_meta = _meta()
                return "SHORT", current_close, reason

        self.last_entry_meta = _meta()
        return None, None, "Filters not aligned"

    def get_trailing_stop(self, direction: str, current_stop: float, current_data: pd.DataFrame) -> float:
        ema_50 = current_data["close"].ewm(span=50, adjust=False).mean().iloc[-1]

        if direction == "LONG":
            return max(current_stop, float(ema_50))
        return min(current_stop, float(ema_50))

    @staticmethod
    def calculate_pnl(direction: str, entry_price: float, exit_price: float, quantity: int, brokerage: float) -> float:
        if direction == "LONG":
            gross = (exit_price - entry_price) * quantity
        else:
            gross = (entry_price - exit_price) * quantity

        return gross - brokerage
