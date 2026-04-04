from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GapType = Literal["GAP_UP", "GAP_DOWN", "NO_GAP", "UNKNOWN"]


@dataclass
class GapFilters:
    max_gap_percent: float = 1.5
    moderate_gap_threshold: float = 0.5
    gap_fill_check: bool = True

    @staticmethod
    def _as_float(value) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def detect_gap(
        self,
        current_open: float | None,
        current_high: float | None,
        current_low: float | None,
        previous_close: float | None,
    ) -> tuple[GapType, float, bool]:
        prev = self._as_float(previous_close)
        high = self._as_float(current_high)
        low = self._as_float(current_low)
        if prev in (None, 0):
            return "UNKNOWN", 0.0, False
        if high is None or low is None:
            return "UNKNOWN", 0.0, False

        if low > prev:
            gap_percent = ((low - prev) / prev) * 100
            return "GAP_UP", round(gap_percent, 2), True
        if high < prev:
            gap_percent = ((prev - high) / prev) * 100
            return "GAP_DOWN", round(gap_percent, 2), True
        return "NO_GAP", 0.0, False

    def detect_opening_range_gap(
        self,
        opening_high: float | None,
        opening_low: float | None,
        previous_close: float | None,
    ) -> tuple[GapType, float, bool]:
        # Opening range gap logic compares OR envelope vs previous close.
        return self.detect_gap(
            current_open=opening_low,
            current_high=opening_high,
            current_low=opening_low,
            previous_close=previous_close,
        )

    def should_skip_trade(self, gap_type: GapType, gap_percent: float) -> bool:
        if gap_type in {"NO_GAP", "UNKNOWN"}:
            return False
        return float(gap_percent or 0) > float(self.max_gap_percent)

    def get_position_size_multiplier(self, gap_type: GapType, gap_percent: float) -> float:
        if gap_type in {"NO_GAP", "UNKNOWN"}:
            return 1.0

        gap_value = float(gap_percent or 0)
        if gap_value < float(self.moderate_gap_threshold):
            return 1.0
        if gap_value < float(self.max_gap_percent):
            return 0.5
        return 0.0

    def gap_status(self, gap_type: GapType, gap_percent: float) -> str:
        if gap_type in {"NO_GAP", "UNKNOWN"}:
            return "OK"
        gap_value = float(gap_percent or 0)
        if gap_value < float(self.moderate_gap_threshold):
            return "OK"
        if gap_value < float(self.max_gap_percent):
            return "MODERATE"
        return "LARGE"

    def gap_action(self, gap_type: GapType, gap_percent: float) -> str:
        multiplier = self.get_position_size_multiplier(gap_type, gap_percent)
        if multiplier <= 0:
            return "Skip"
        if multiplier < 1:
            return "Reduce Size"
        return "Full Size"

    def check_gap_breakout_quality(
        self,
        opening_high: float | None,
        opening_low: float | None,
        previous_close: float | None,
        current_price: float | None,
    ) -> tuple[float, bool]:
        gap_type, gap_percent, has_gap = self.detect_opening_range_gap(
            opening_high=opening_high,
            opening_low=opening_low,
            previous_close=previous_close,
        )
        if not has_gap:
            return 100.0, True

        or_high = self._as_float(opening_high)
        or_low = self._as_float(opening_low)
        price = self._as_float(current_price)
        if or_high is None or or_low is None or price is None:
            return 0.0, False

        or_width = max(or_high - or_low, 0.000001)
        if gap_type == "GAP_UP":
            gap_filled_percent = ((price - or_low) / or_width) * 100
        else:
            gap_filled_percent = ((or_high - price) / or_width) * 100

        base_score = 100 - min((float(gap_percent) * 30), 70)
        gap_fill_penalty = 30 if float(gap_filled_percent) < 20 else 0
        quality_score = max(0.0, min(100.0, base_score - gap_fill_penalty))
        should_trade = quality_score > 50 and float(gap_percent) < float(self.max_gap_percent)
        return round(quality_score, 2), bool(should_trade)
