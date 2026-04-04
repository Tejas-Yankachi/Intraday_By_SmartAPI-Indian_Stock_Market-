from datetime import date, time
from io import StringIO
from decimal import Decimal
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import Mock, patch

import pandas as pd

from trading.models import OpeningRange, StrategyLog, Trade, TradingSession, WatchlistSymbol
from trading.services.gap_filters import GapFilters
from trading.services.orb_strategy import ORBStrategy, StrategyConfig
from trading.services.pdh_pdl_strategy import PDHBreakoutStrategy
from trading.services.smartapi_gateway import SmartAPIClient, SmartCredentials
from trading.services.trend_filters import OptimalTrendFilter


class DashboardSmartApiIndicatorTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="tejas", password="test-pass-123")
        self.client.force_login(self.user)

    def test_dashboard_contains_smartapi_indicator(self):
        response = self.client.get(reverse("trading:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Advanced PDH/PDL Dashboard")
        self.assertContains(response, "Signal & PDH/PDL Scanner")
        self.assertContains(response, "Intraday Smart PDH/PDL")
        self.assertContains(response, "Backend Execution Monitor")
        self.assertContains(response, 'id="backendExecutionOutput"')
        self.assertContains(response, 'id="smartApiStatusPill"')
        self.assertContains(response, 'id="smartApiStatusLabel"')
        self.assertContains(response, "NSE Watchlist")
        self.assertContains(response, reverse("trading:watchlist_add"))
        self.assertContains(response, reverse("accounts:smartapi_test"))
        self.assertContains(response, "Account Holder")

    def test_watchlist_add_with_manual_token(self):
        response = self.client.post(
            reverse("trading:watchlist_add"),
            data={"symbol": "sbin", "token": "3045"},
        )

        self.assertEqual(response.status_code, 302)
        row = WatchlistSymbol.objects.get(symbol="SBIN")
        self.assertEqual(row.token, "3045")
        self.assertEqual(row.exchange, "NSE")
        self.assertTrue(row.enabled)

    def test_watchlist_symbol_click_opens_chart_page(self):
        row = WatchlistSymbol.objects.create(symbol="SBIN", token="3045", exchange="NSE", enabled=True, priority=1)

        dashboard = self.client.get(reverse("trading:dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, reverse("trading:watchlist_chart", args=[row.id]))

        chart = self.client.get(reverse("trading:watchlist_chart", args=[row.id]))
        self.assertEqual(chart.status_code, 200)
        self.assertContains(chart, "SBIN TradingView Chart")
        self.assertContains(chart, "NSE:SBIN")

    @patch("trading.views.SmartAPIClient.from_user_profile")
    def test_watchlist_add_uses_smartapi_lookup_when_token_missing(self, mock_factory):
        fake_client = Mock()
        fake_client.resolve_nse_symbol_token.return_value = {
            "symbol": "TATAMOTORS",
            "token": "3456",
            "exchange": "NSE",
        }
        mock_factory.return_value = fake_client

        response = self.client.post(
            reverse("trading:watchlist_add"),
            data={"symbol": "tatamotors", "token": ""},
        )

        self.assertEqual(response.status_code, 302)
        row = WatchlistSymbol.objects.get(symbol="TATAMOTORS")
        self.assertEqual(row.token, "3456")
        fake_client.resolve_nse_symbol_token.assert_called_once_with("TATAMOTORS")

    @patch("trading.views.get_live_dashboard_snapshot")
    def test_dashboard_api_includes_live_scanner_payload(self, mock_snapshot):
        mock_snapshot.return_value = {
            "api_connection": {"active": False, "status": "INACTIVE", "message": "demo"},
            "market_context": {"market_bias": "NEUTRAL", "window_timer": "Closed"},
            "funds": {"total_funds": 100000.0, "available_funds": 76000.0, "utilized_margin": 24000.0, "margin_utilized_percent": 24.0},
            "signal_table": [{"symbol": "RELIANCE", "signal": "NO SIGNAL", "pdh": 2500.0, "pdl": 2450.0}],
            "opening_ranges": [{"symbol": "RELIANCE", "pdh": 2500.0, "pdl": 2450.0}],
            "price_map": {"RELIANCE": 2501.0},
        }

        response = self.client.get(reverse("trading:dashboard_api"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("api_connection", payload)
        self.assertIn("market_context", payload)
        self.assertIn("funds", payload)
        self.assertIn("risk", payload)
        self.assertIn("signal_table", payload)
        self.assertIn("opening_ranges", payload)
        self.assertIn("backend_execution", payload)


class TerminalDashboardCommandTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="terminal", password="test-pass-123")

    def test_terminal_dashboard_renders_backend_state(self):
        session = TradingSession.objects.create(
            user=self.user,
            status=TradingSession.STATUS_RUNNING,
            market_bias="BULLISH",
            notes="Monitoring backend execution from the terminal.",
        )
        Trade.objects.create(
            session=session,
            user=self.user,
            symbol="RELIANCE",
            token="2885",
            direction=Trade.DIRECTION_LONG,
            entry_price=Decimal("2500.00"),
            stop_loss=Decimal("2480.00"),
            target_price=Decimal("2540.00"),
            quantity=5,
            status=Trade.STATUS_OPEN,
            order_id="OID123",
        )
        OpeningRange.objects.create(
            session=session,
            symbol="RELIANCE",
            token="2885",
            high=Decimal("2510.00"),
            low=Decimal("2490.00"),
        )
        StrategyLog.objects.create(
            session=session,
            symbol="RELIANCE",
            level=StrategyLog.LEVEL_INFO,
            message="Breakout confirmed",
        )

        out = StringIO()
        call_command("terminal_dashboard", once=True, session_id=session.id, stdout=out)
        rendered = out.getvalue()

        self.assertIn("BACKEND EXECUTION DASHBOARD", rendered)
        self.assertIn(f"Session #{session.id}", rendered)
        self.assertIn("Open Trades", rendered)
        self.assertIn("PDH/PDL Levels", rendered)
        self.assertIn("RELIANCE", rendered)
        self.assertIn("Breakout confirmed", rendered)


class BracketCandleExecutionTests(TestCase):
    def _build_strategy(self) -> ORBStrategy:
        return ORBStrategy(
            StrategyConfig(
                capital=Decimal("100000"),
                risk_per_trade_percent=Decimal("1.0"),
                min_risk_amount=Decimal("500"),
                brokerage_per_trade=Decimal("20"),
                trade_start_time=time(9, 20),
                trade_end_time=time(15, 0),
                exit_time=time(15, 15),
            )
        )

    @staticmethod
    def _build_symbol_candles(last_close: float, last_high: float, opening_high: float) -> pd.DataFrame:
        ts = pd.date_range("2026-01-01 09:15:00", periods=60, freq="5min")
        closes = [90 + (i * 0.2) for i in range(59)] + [last_close]
        highs = [c + 0.8 for c in closes]
        lows = [c - 0.8 for c in closes]
        volumes = [60000 for _ in closes]

        highs[-1] = last_high
        lows[-1] = min(lows[-1], opening_high - 2)

        return pd.DataFrame(
            {
                "datetime": ts,
                "open": closes,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        )

    @staticmethod
    def _build_nifty() -> pd.DataFrame:
        ts = pd.date_range("2026-01-01 09:15:00", periods=60, freq="15min")
        closes = [200 + (i * 0.7) for i in range(60)]
        return pd.DataFrame({"datetime": ts, "close": closes})

    def test_identify_opening_range_uses_latest_trading_day(self):
        strategy = self._build_strategy()
        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(
                    [
                        "2026-01-01 09:15:00",
                        "2026-01-01 09:20:00",
                        "2026-01-02 09:15:00",
                        "2026-01-02 09:20:00",
                    ]
                ),
                "open": [100, 101, 200, 201],
                "high": [110, 111, 210, 211],
                "low": [90, 91, 190, 191],
                "close": [105, 106, 205, 206],
                "volume": [1000, 1000, 1000, 1000],
            }
        )

        opening_high, opening_low = strategy.identify_opening_range(frame)

        self.assertEqual(opening_high, 210.0)
        self.assertEqual(opening_low, 190.0)

    def test_no_entry_when_only_wick_breaks_orh(self):
        strategy = self._build_strategy()
        opening_high = 101.5
        opening_low = 98.0
        data_5min = self._build_symbol_candles(
            last_close=101.2,   # close below ORH
            last_high=102.4,    # wick above ORH
            opening_high=opening_high,
        )
        nifty_15min = self._build_nifty()

        direction, entry, reason = strategy.evaluate_entry(
            data_5min=data_5min,
            nifty_15min=nifty_15min,
            opening_high=opening_high,
            opening_low=opening_low,
            current_time=time(10, 30),
        )

        self.assertIsNone(direction)
        self.assertIsNone(entry)
        self.assertNotIn("bracket candle close above ORH", reason)

    def test_long_entry_when_bracket_candle_closes_above_orh(self):
        strategy = self._build_strategy()
        opening_high = 101.5
        opening_low = 98.0
        data_5min = self._build_symbol_candles(
            last_close=102.2,   # close above ORH
            last_high=102.8,
            opening_high=opening_high,
        )
        nifty_15min = self._build_nifty()

        direction, entry, reason = strategy.evaluate_entry(
            data_5min=data_5min,
            nifty_15min=nifty_15min,
            opening_high=opening_high,
            opening_low=opening_low,
            current_time=time(10, 30),
        )

        self.assertEqual(direction, "LONG")
        self.assertAlmostEqual(entry, 102.2, places=2)
        self.assertIn("bracket candle close above ORH", reason)

    def test_trade_window_follows_profile_start_time(self):
        strategy = self._build_strategy()

        self.assertFalse(strategy.check_time_window(time(9, 19)))
        self.assertTrue(strategy.check_time_window(time(9, 20)))


class PDHBreakoutStrategyTests(TestCase):
    def _build_strategy(self) -> PDHBreakoutStrategy:
        return PDHBreakoutStrategy(
            StrategyConfig(
                capital=Decimal("100000"),
                risk_per_trade_percent=Decimal("1.0"),
                min_risk_amount=Decimal("500"),
                brokerage_per_trade=Decimal("20"),
                trade_start_time=time(9, 20),
                trade_end_time=time(15, 0),
                exit_time=time(15, 15),
            )
        )

    @staticmethod
    def _two_day_frame(prev_high: float = 111.0, prev_low: float = 90.0, final_close: float = 112.0, final_open: float = 111.6) -> pd.DataFrame:
        day1 = pd.date_range("2026-01-01 09:15:00", periods=50, freq="5min")
        day2 = pd.date_range("2026-01-02 09:15:00", periods=5, freq="5min")
        ts = list(day1) + list(day2)

        closes = [100.0 + (idx * 0.2) for idx in range(len(ts))]
        opens = [close - 0.1 for close in closes]
        highs = [close + 0.8 for close in closes]
        lows = [close - 0.8 for close in closes]
        volumes = [60000 for _ in ts]

        highs[12] = prev_high
        lows[15] = prev_low

        opens[-1] = final_open
        closes[-1] = final_close
        highs[-1] = final_close + 0.8
        lows[-1] = final_open - 0.4

        return pd.DataFrame(
            {
                "datetime": ts,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        )

    def test_identify_opening_range_uses_previous_day_levels(self):
        strategy = self._build_strategy()
        frame = self._two_day_frame(prev_high=125.0, prev_low=95.0)

        opening_high, opening_low = strategy.identify_opening_range(frame)

        self.assertEqual(opening_high, 125.0)
        self.assertEqual(opening_low, 95.0)

    def test_identify_opening_range_uses_reference_date_when_provided(self):
        strategy = self._build_strategy()
        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(
                    [
                        "2026-01-01 15:25:00",
                        "2026-01-02 15:25:00",
                        "2026-01-05 15:25:00",
                    ]
                ),
                "open": [100.0, 110.0, 120.0],
                "high": [105.0, 115.0, 126.0],
                "low": [95.0, 108.0, 118.0],
                "close": [104.0, 114.0, 125.0],
                "volume": [1000, 1000, 1000],
            }
        )

        opening_high, opening_low = strategy.identify_opening_range(frame, reference_date=date(2026, 1, 5))

        self.assertEqual(opening_high, 115.0)
        self.assertEqual(opening_low, 108.0)

    def test_market_bias_uses_previous_day_levels(self):
        strategy = self._build_strategy()
        bullish_frame = self._two_day_frame(prev_high=111.0, prev_low=90.0, final_close=113.0)
        bearish_frame = self._two_day_frame(prev_high=111.0, prev_low=90.0, final_close=88.0, final_open=88.4)

        self.assertEqual(strategy.get_market_bias(bullish_frame), "BULLISH")
        self.assertEqual(strategy.get_market_bias(bearish_frame), "BEARISH")

    def test_calculate_target_price_uses_full_target_r(self):
        strategy = self._build_strategy()

        target = strategy.calculate_target_price(100.0, 96.0, 10)

        self.assertAlmostEqual(target, 110.0, places=2)

    def test_evaluate_entry_returns_long_on_pdh_breakout_when_filters_pass(self):
        strategy = self._build_strategy()
        frame = self._two_day_frame(prev_high=111.0, prev_low=90.0, final_close=112.0, final_open=111.6)

        with (
            patch.object(strategy, "_candle_body_ok", return_value=True),
            patch.object(strategy, "check_ema_alignment", return_value=True),
            patch.object(strategy, "_volume_ok", return_value=True),
            patch.object(strategy, "_atr_ok", return_value=True),
            patch.object(strategy, "_retest_confirmed", return_value=True),
            patch.object(strategy, "get_market_bias", return_value="BULLISH"),
        ):
            direction, entry, reason = strategy.evaluate_entry(
                data_5min=frame,
                nifty_15min=frame,
                opening_high=111.0,
                opening_low=90.0,
                current_time=time(10, 30),
            )

        self.assertEqual(direction, "LONG")
        self.assertAlmostEqual(entry, 112.0, places=2)
        self.assertIn("PDH", reason)

    def test_get_trailing_stop_uses_previous_candle_high_low(self):
        strategy = self._build_strategy()
        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(
                    [
                        "2026-01-01 09:15:00",
                        "2026-01-01 09:20:00",
                        "2026-01-01 09:25:00",
                    ]
                ),
                "open": [100, 102, 104],
                "high": [103, 104, 105],
                "low": [99, 101, 103],
                "close": [102, 103, 104],
                "volume": [1000, 1000, 1000],
            }
        )

        long_stop = strategy.get_trailing_stop("LONG", 100.0, frame)
        short_stop = strategy.get_trailing_stop("SHORT", 106.0, frame)

        self.assertEqual(long_stop, 101.0)
        self.assertEqual(short_stop, 104.0)


class GapFiltersTests(TestCase):
    def test_detect_gap_up_and_skip_large_gap(self):
        filters = GapFilters(max_gap_percent=1.5, moderate_gap_threshold=0.5, gap_fill_check=True)
        gap_type, gap_percent, has_gap = filters.detect_gap(
            current_open=1364.5,
            current_high=1384.4,
            current_low=1364.5,
            previous_close=1350.0,
        )

        self.assertEqual(gap_type, "GAP_UP")
        self.assertTrue(has_gap)
        self.assertGreater(gap_percent, 1.0)
        self.assertFalse(filters.should_skip_trade(gap_type, gap_percent))
        self.assertEqual(filters.get_position_size_multiplier(gap_type, gap_percent), 0.5)

    def test_large_gap_returns_skip_action(self):
        filters = GapFilters(max_gap_percent=1.5, moderate_gap_threshold=0.5, gap_fill_check=True)
        gap_type, gap_percent, _ = filters.detect_gap(
            current_open=210.0,
            current_high=212.0,
            current_low=210.0,
            previous_close=200.0,
        )

        self.assertEqual(gap_type, "GAP_UP")
        self.assertGreaterEqual(gap_percent, 5.0)
        self.assertEqual(filters.get_position_size_multiplier(gap_type, gap_percent), 0.0)
        self.assertEqual(filters.gap_status(gap_type, gap_percent), "LARGE")
        self.assertEqual(filters.gap_action(gap_type, gap_percent), "Skip")


class OptimalTrendFilterTests(TestCase):
    @staticmethod
    def _trend_frame(base_price: float, step: float, periods: int = 40) -> pd.DataFrame:
        ts = pd.date_range("2026-01-01 09:15:00", periods=periods, freq="15min")
        closes = [base_price + (idx * step) for idx in range(periods)]
        return pd.DataFrame(
            {
                "datetime": ts,
                "open": closes,
                "high": [c + 0.6 for c in closes],
                "low": [c - 0.6 for c in closes],
                "close": closes,
                "volume": [100000 for _ in closes],
            }
        )

    def test_trend_filter_allows_long_in_bullish_trend(self):
        trend_filter = OptimalTrendFilter(min_candles=30)
        df = self._trend_frame(base_price=100.0, step=0.75, periods=45)

        allowed, status = trend_filter.check_trend_for_direction(df, "LONG")

        self.assertTrue(allowed)
        self.assertEqual(status["allowed_direction"], "LONG")
        self.assertIn(status["trend"], {"STRONG_BULLISH", "WEAK_BULLISH"})
        self.assertIn(status["trend_status"], {"PASS", "CAUTION"})
        self.assertGreater(status["strength"], 0)

    def test_trend_filter_rejects_sideways_market(self):
        trend_filter = OptimalTrendFilter(min_candles=30, buffer_percent=1.0)
        df = self._trend_frame(base_price=100.0, step=0.02, periods=45)

        allowed, status = trend_filter.check_trend_for_direction(df, "LONG")

        self.assertFalse(allowed)
        self.assertEqual(status["trend"], "SIDEWAYS")
        self.assertFalse(status["should_trade"])
        self.assertEqual(status["trend_status"], "FAIL")

    def test_trend_filter_uses_fallback_when_15m_data_missing(self):
        trend_filter = OptimalTrendFilter(min_candles=30, fallback_enabled=True)
        client = Mock()
        client.fetch_historical_data.return_value = None

        allowed, status = trend_filter.check_15min_trend_with_fallback(client, "LONG")

        self.assertTrue(allowed)
        self.assertEqual(status["trend"], "UNKNOWN (fallback)")
        self.assertEqual(status["trend_status"], "CAUTION")
        self.assertIn("insufficient", status["message"].lower())

    def test_fetch_15min_data_does_not_call_raw_candle_api_fallback(self):
        trend_filter = OptimalTrendFilter(min_candles=30, fallback_enabled=True)
        client = Mock()
        client.fetch_historical_data.return_value = None
        client.obj = Mock()

        data = trend_filter.fetch_15min_data(client=client, token="13", days=5)

        self.assertIsNone(data)
        client.obj.getCandleData.assert_not_called()


class SmartApiGatewayClientTests(TestCase):
    def setUp(self):
        with SmartAPIClient._HISTORICAL_CACHE_LOCK:
            SmartAPIClient._HISTORICAL_CACHE.clear()
        with SmartAPIClient._LOGIN_COOLDOWN_LOCK:
            SmartAPIClient._LOGIN_COOLDOWN_UNTIL.clear()

    @staticmethod
    def _credentials() -> SmartCredentials:
        return SmartCredentials(
            api_key="test-key",
            client_code="AACF355372",
            pin="1234",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

    @patch("trading.services.smartapi_gateway.time.sleep", return_value=None)
    def test_fetch_historical_data_retries_timeout_and_returns_rows(self, _mock_sleep):
        client = SmartAPIClient(credentials=self._credentials())
        client.ensure_login = Mock(return_value=True)
        client.obj = Mock()
        client.obj.getCandleData.side_effect = [
            Exception("Read timed out. (read timeout=20)"),
            {"status": True, "data": [["2026-04-02 10:00", "100", "101", "99", "100.5", "1200"]]},
        ]

        df = client.fetch_historical_data(token="2475", interval="FIVE_MINUTE", days=1)

        self.assertIsNotNone(df)
        self.assertEqual(len(df), 1)
        self.assertEqual(client.obj.getCandleData.call_count, 2)

    @patch("trading.services.smartapi_gateway.time.sleep", return_value=None)
    def test_fetch_historical_data_uses_fast_path_for_multi_day_range(self, _mock_sleep):
        client = SmartAPIClient(credentials=self._credentials())
        client.ensure_login = Mock(return_value=True)
        client.obj = Mock()
        client.obj.getCandleData.return_value = {
            "status": True,
            "data": [["2026-04-02 10:00", "100", "101", "99", "100.5", "1200"]],
        }

        df = client.fetch_historical_data(token="2475", interval="FIVE_MINUTE", days=2)

        self.assertIsNotNone(df)
        self.assertEqual(client.obj.getCandleData.call_count, 1)

    @patch("trading.services.smartapi_gateway.time.sleep", return_value=None)
    def test_fetch_historical_data_falls_back_to_chunked_calls_on_multi_day_failure(self, _mock_sleep):
        client = SmartAPIClient(credentials=self._credentials())
        client.ensure_login = Mock(return_value=True)
        client.obj = Mock()
        client.obj.getCandleData.side_effect = [
            Exception("Read timed out. (read timeout=20)"),
            Exception("Read timed out. (read timeout=20)"),
            {"status": True, "data": [["2026-04-01 10:00", "100", "101", "99", "100.5", "1200"]]},
            {"status": True, "data": [["2026-04-02 10:00", "101", "102", "100", "101.5", "1300"]]},
        ]

        df = client.fetch_historical_data(token="2475", interval="FIVE_MINUTE", days=2)

        self.assertIsNotNone(df)
        self.assertGreaterEqual(client.obj.getCandleData.call_count, 4)

    @patch.object(SmartAPIClient, "_historical_cache_ttl_seconds", return_value=10.0)
    @patch("trading.services.smartapi_gateway.time.sleep", return_value=None)
    def test_fetch_historical_data_uses_short_lived_cache(self, _mock_sleep, _mock_ttl):
        client = SmartAPIClient(credentials=self._credentials())
        client.ensure_login = Mock(return_value=True)
        client.obj = Mock()
        client.obj.getCandleData.return_value = {
            "status": True,
            "data": [["2026-04-02 10:00", "100", "101", "99", "100.5", "1200"]],
        }

        first = client.fetch_historical_data(token="2475", interval="FIVE_MINUTE", days=1)
        second = client.fetch_historical_data(token="2475", interval="FIVE_MINUTE", days=1)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(client.obj.getCandleData.call_count, 1)
        self.assertEqual(client.ensure_login.call_count, 1)

    def test_logout_uses_client_code_not_auth_token(self):
        client = SmartAPIClient(credentials=self._credentials())
        mock_obj = Mock()
        client.obj = mock_obj
        client.auth_token = "Bearer should-not-be-used"
        client.refresh_token = "refresh-token"
        client.feed_token = "feed-token"

        client.logout()

        # terminateSession must receive client code; passing JWT token causes "Invalid client code length".
        mock_obj.terminateSession.assert_called_once_with("AACF355372")
        self.assertIsNone(client.obj)
        self.assertEqual(client.auth_token, "")
        self.assertEqual(client.refresh_token, "")
        self.assertEqual(client.feed_token, "")

    @patch("trading.services.smartapi_gateway.time.sleep", return_value=None)
    @patch.object(SmartAPIClient, "_is_host_reachable", return_value=True)
    @patch("trading.services.smartapi_gateway.SmartConnect")
    def test_validate_login_returns_friendly_rate_limit_message(
        self,
        mock_smart_connect,
        _mock_host_reachable,
        _mock_sleep,
    ):
        mock_obj = Mock()
        mock_obj.generateSession.side_effect = Exception(
            "Couldn't parse the JSON response received from the server: "
            "b'Access denied because of exceeding access rate'"
        )
        mock_smart_connect.return_value = mock_obj

        client = SmartAPIClient(credentials=self._credentials(), login_retries=2)
        success, message = client.validate_login()

        self.assertFalse(success)
        self.assertIn("rate limit", message.lower())
        self.assertGreaterEqual(mock_obj.generateSession.call_count, 1)

    @patch.object(SmartAPIClient, "_login_cooldown_seconds", return_value=60.0)
    @patch("trading.services.smartapi_gateway.time.sleep", return_value=None)
    @patch.object(SmartAPIClient, "_is_host_reachable", return_value=True)
    @patch("trading.services.smartapi_gateway.SmartConnect")
    def test_validate_login_short_circuits_when_rate_limit_cooldown_active(
        self,
        mock_smart_connect,
        _mock_host_reachable,
        _mock_sleep,
        _mock_cooldown,
    ):
        mock_obj = Mock()
        mock_obj.generateSession.side_effect = Exception(
            "Access denied because of exceeding access rate"
        )
        mock_smart_connect.return_value = mock_obj

        client = SmartAPIClient(credentials=self._credentials(), login_retries=1)

        first_success, first_message = client.validate_login()
        second_success, second_message = client.validate_login()

        self.assertFalse(first_success)
        self.assertIn("rate limit", first_message.lower())
        self.assertFalse(second_success)
        self.assertIn("rate limit", second_message.lower())
        self.assertIn("retry in about", second_message.lower())
        self.assertEqual(mock_obj.generateSession.call_count, 1)
