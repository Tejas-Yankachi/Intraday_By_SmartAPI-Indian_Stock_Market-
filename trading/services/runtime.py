from __future__ import annotations

import threading
import time
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from trading.models import OpeningRange, StrategyLog, Trade, TradingSession, WatchlistSymbol

from .orb_strategy import StrategyConfig
from .pdh_pdl_strategy import PDHBreakoutStrategy
from .smartapi_gateway import SmartAPIClient

RUNNERS: dict[int, "SessionRunner"] = {}


class SessionRunner(threading.Thread):
    def __init__(self, session_id: int):
        super().__init__(daemon=True)
        self.session_id = session_id
        self._stop_event = threading.Event()
        self.client = None
        self._last_candle_time: dict[int, datetime] = {}

    def stop(self):
        self._stop_event.set()

    def log(self, message: str, level: str = StrategyLog.LEVEL_INFO, symbol: str = ""):
        session = TradingSession.objects.filter(id=self.session_id).first()
        if session:
            StrategyLog.objects.create(session=session, symbol=symbol, level=level, message=message)

    @staticmethod
    def _trade_meta(trade: Trade) -> dict:
        meta = trade.meta or {}
        return meta if isinstance(meta, dict) else {}

    def _close_trade(self, trade: Trade, exit_price: float, reason: str, broker_exit: bool = True):
        profile = trade.user.trading_profile
        brokerage = float(profile.brokerage_per_trade)
        if broker_exit and profile.is_live_trading_enabled and self.client:
            exit_ok, exit_message, exit_response = self.client.exit_position_market(
                symbol=trade.symbol,
                token=trade.token,
                direction=trade.direction,
                quantity=trade.quantity,
            )
            self.log(
                exit_message,
                level=StrategyLog.LEVEL_INFO if exit_ok else StrategyLog.LEVEL_ERROR,
                symbol=trade.symbol,
            )
            if not exit_ok:
                return

            exclude_order_ids = set()
            if exit_response:
                exit_order_id = self.client.extract_order_id(exit_response)
                if exit_order_id:
                    exclude_order_ids.add(exit_order_id)
        elif profile.is_live_trading_enabled and self.client:
            exclude_order_ids = set()

        if profile.is_live_trading_enabled and self.client:
            cancelled_count, cancel_message = self.client.cancel_pending_orders_for_symbol(
                symbol=trade.symbol,
                token=trade.token,
                exclude_order_ids=exclude_order_ids,
            )
            if cancel_message:
                level = StrategyLog.LEVEL_WARNING if "failed" in cancel_message.lower() else StrategyLog.LEVEL_INFO
                self.log(cancel_message, level=level, symbol=trade.symbol)
            if cancelled_count > 0:
                self.log(
                    f"Cancelled {cancelled_count} pending broker order(s) for {trade.symbol}.",
                    symbol=trade.symbol,
                )

        if trade.direction == Trade.DIRECTION_LONG:
            gross = (exit_price - float(trade.entry_price)) * trade.quantity
        else:
            gross = (float(trade.entry_price) - exit_price) * trade.quantity

        net_pnl = gross - brokerage

        trade.exit_price = round(exit_price, 2)
        trade.exit_time = timezone.now()
        trade.exit_reason = reason
        trade.pnl = round(net_pnl, 2)
        trade.brokerage = round(brokerage, 2)
        trade.status = Trade.STATUS_CLOSED
        trade.save(update_fields=[
            "exit_price",
            "exit_time",
            "exit_reason",
            "pnl",
            "brokerage",
            "status",
        ])

        self.log(f"Closed {trade.symbol} ({reason}) P&L ₹{net_pnl:.2f}", symbol=trade.symbol)

    def _cancel_trade_locally(self, trade: Trade, reason: str, message: str):
        trade.status = Trade.STATUS_CANCELLED
        trade.exit_time = timezone.now()
        trade.exit_reason = reason
        trade.save(update_fields=["status", "exit_time", "exit_reason"])
        self.log(message, level=StrategyLog.LEVEL_WARNING, symbol=trade.symbol)

    def _build_strategy(self, profile) -> PDHBreakoutStrategy:
        config = StrategyConfig(
            capital=Decimal(profile.capital),
            risk_per_trade_percent=Decimal(profile.risk_per_trade_percent),
            min_risk_amount=Decimal(profile.min_risk_amount),
            brokerage_per_trade=Decimal(profile.brokerage_per_trade),
            trade_start_time=profile.trade_start_time,
            trade_end_time=profile.trade_end_time,
            exit_time=profile.exit_time,
        )
        return PDHBreakoutStrategy(config=config)

    @staticmethod
    def _buffered_entry_price(entry_price: float, direction: str, profile) -> tuple[float, float, float]:
        """
        Returns:
            (effective_entry_price, buffer_percent, buffer_points)
        Buffer is only applied in live mode.
        """
        base_price = float(entry_price)
        if not bool(getattr(profile, "is_live_trading_enabled", False)):
            return base_price, 0.0, 0.0

        buffer_percent = max(0.0, float(getattr(profile, "entry_buffer_percent", 0) or 0.0))
        if buffer_percent == 0:
            return base_price, 0.0, 0.0

        buffer_points = base_price * (buffer_percent / 100.0)
        if str(direction or "").upper() == Trade.DIRECTION_LONG:
            effective_price = base_price + buffer_points
        else:
            effective_price = max(0.01, base_price - buffer_points)
        return effective_price, buffer_percent, buffer_points

    def _daily_loss_exceeded(self, session: TradingSession, profile) -> bool:
        closed_today = session.trades.filter(status=Trade.STATUS_CLOSED)
        total_pnl = sum(float(t.pnl) for t in closed_today)
        daily_limit = float(profile.capital) * (float(session.daily_loss_limit_percent) / 100)
        return total_pnl <= -daily_limit

    def _weekly_loss_exceeded(self, user, profile, session: TradingSession) -> bool:
        now = timezone.localtime()
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_pnl = (
            Trade.objects.filter(
                user=user,
                status=Trade.STATUS_CLOSED,
                exit_time__gte=week_start,
            )
            .values_list("pnl", flat=True)
        )
        total_weekly_pnl = sum(float(v or 0) for v in weekly_pnl)
        weekly_limit = float(profile.capital) * (float(session.weekly_loss_limit_percent) / 100)
        return total_weekly_pnl <= -weekly_limit

    def _get_or_create_opening_range(
        self,
        session: TradingSession,
        symbol_obj: WatchlistSymbol,
        data_5min,
        strategy: PDHBreakoutStrategy,
        reference_date=None,
    ):
        opening = OpeningRange.objects.filter(session=session, symbol=symbol_obj.symbol).first()
        if opening:
            return float(opening.high), float(opening.low)

        opening_high, opening_low = strategy.identify_opening_range(data_5min, reference_date=reference_date)
        if opening_high is None or opening_low is None:
            return None, None

        OpeningRange.objects.create(
            session=session,
            symbol=symbol_obj.symbol,
            token=symbol_obj.token,
            high=round(opening_high, 2),
            low=round(opening_low, 2),
        )
        self.log(
            f"Opening range {symbol_obj.symbol} H={opening_high:.2f}, L={opening_low:.2f}",
            symbol=symbol_obj.symbol,
        )
        return opening_high, opening_low

    def _check_order_status(self, trade: Trade, current_time) -> bool:
        if not (trade and self.client):
            return False

        meta = self._trade_meta(trade)
        target_order_id = str(meta.get("target_order_id") or "").strip()
        sl_order_id = str(meta.get("sl_order_id") or "").strip()

        if target_order_id and self.client.is_order_triggered(target_order_id):
            if sl_order_id:
                self.client.cancel_order(sl_order_id)
            self._close_trade(trade, float(trade.target_price), Trade.EXIT_TARGET, broker_exit=False)
            return True

        if sl_order_id and self.client.is_order_triggered(sl_order_id):
            if target_order_id:
                self.client.cancel_order(target_order_id)
            self._close_trade(trade, float(trade.stop_loss), Trade.EXIT_STOP, broker_exit=False)
            return True

        if not trade.order_id:
            return False

        broker_status = self.client.get_order_status(trade.order_id)
        if self.client.is_terminal_rejection_status(broker_status):
            self._cancel_trade_locally(
                trade,
                reason=Trade.EXIT_SIGNAL,
                message=f"Broker order {trade.order_id} is {broker_status}; trade marked cancelled.",
            )
            return True

        if broker_status and not self.client.is_filled_status(broker_status):
            if current_time and current_time >= trade.user.trading_profile.exit_time:
                cancelled_count, cancel_message = self.client.cancel_pending_orders_for_symbol(
                    symbol=trade.symbol,
                    token=trade.token,
                )
                note = (
                    f"Entry order not filled by exit time ({broker_status}). "
                    f"{cancel_message}"
                )
                if cancelled_count > 0:
                    note = f"{note} Cancelled {cancelled_count} pending order(s)."
                self._cancel_trade_locally(
                    trade,
                    reason=Trade.EXIT_TIME,
                    message=note,
                )
            return True

        return False

    def _update_trailing_stop(self, trade: Trade) -> None:
        if not self.client:
            return

        data_5min = self.client.fetch_historical_data(token=trade.token, interval="FIVE_MINUTE", days=1)
        if data_5min is None or len(data_5min) < 50:
            return

        current_candle = data_5min.iloc[-1]
        current_candle_time = current_candle["datetime"]
        last_time = self._last_candle_time.get(trade.id)
        if last_time is not None and current_candle_time <= last_time:
            return

        frame = PDHBreakoutStrategy._normalized_frame(data_5min)
        if frame.empty or len(frame) < 2:
            return

        prev_candle = frame.iloc[-2]
        stop_candidate = None
        if trade.direction == Trade.DIRECTION_LONG:
            prev_candle_low = float(prev_candle["low"])
            if prev_candle_low > float(trade.stop_loss):
                stop_candidate = prev_candle_low
                self.log("TRAIL %s stop would move to %.2f" % (trade.symbol, prev_candle_low))
        else:
            prev_candle_high = float(prev_candle["high"])
            if prev_candle_high < float(trade.stop_loss):
                stop_candidate = prev_candle_high
                self.log("TRAIL %s stop would move to %.2f" % (trade.symbol, prev_candle_high))

        if stop_candidate is None or stop_candidate == float(trade.stop_loss):
            self._last_candle_time[trade.id] = current_candle_time
            return

        meta = self._trade_meta(trade)
        sl_order_id = str(meta.get("sl_order_id") or "").strip()
        if trade.user.trading_profile.is_live_trading_enabled and self.client:
            if sl_order_id:
                self.client.cancel_order(sl_order_id)
            new_sl_oid = self.client.place_stoploss_market_order(
                symbol=trade.symbol,
                token=trade.token,
                direction=trade.direction,
                trigger_price=stop_candidate,
                quantity=trade.quantity,
            )
            if not new_sl_oid:
                self.log(f"TRAIL {trade.symbol} failed to place new SL order!", level=StrategyLog.LEVEL_ERROR)
                self._last_candle_time[trade.id] = current_candle_time
                return
            meta = dict(meta)
            meta["sl_order_id"] = new_sl_oid
            trade.meta = meta

        trade.stop_loss = round(stop_candidate, 2)
        trade.save(update_fields=["stop_loss", "meta"] if trade.user.trading_profile.is_live_trading_enabled else ["stop_loss"])
        self.log(
            f"TRAIL {trade.symbol} broker SL updated to {stop_candidate:.2f}",
            symbol=trade.symbol,
        )
        self._last_candle_time[trade.id] = current_candle_time

    def run(self):
        close_old_connections()
        session = TradingSession.objects.filter(id=self.session_id).select_related("user").first()
        if not session:
            return

        profile = session.user.trading_profile
        strategy = self._build_strategy(profile)
        self.client = SmartAPIClient.from_user_profile(profile)

        ok, message = self.client.validate_login()
        self.log(message, level=StrategyLog.LEVEL_INFO if ok else StrategyLog.LEVEL_ERROR)
        if not ok:
            session.status = TradingSession.STATUS_FAILED
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_at"])
            return

        session.status = TradingSession.STATUS_RUNNING
        session.save(update_fields=["status"])

        try:
            while not self._stop_event.is_set():
                close_old_connections()
                session = TradingSession.objects.filter(id=self.session_id).select_related("user").first()
                if not session or session.status not in {
                    TradingSession.STATUS_RUNNING,
                    TradingSession.STATUS_PENDING,
                }:
                    break

                now = timezone.localtime()
                current_time = now.time()

                watchlist = list(WatchlistSymbol.objects.filter(enabled=True).order_by("priority", "symbol"))
                if not watchlist:
                    self.log("No enabled watchlist symbols found.", level=StrategyLog.LEVEL_WARNING)
                    time.sleep(settings.TRADING_POLL_SECONDS)
                    continue

                nifty_15min = self.client.fetch_historical_data(token="13", interval="FIFTEEN_MINUTE", days=7)
                if nifty_15min is not None:
                    market_bias = strategy.get_market_bias(nifty_15min, reference_date=now.date())
                    if market_bias != session.market_bias:
                        session.market_bias = market_bias
                        session.save(update_fields=["market_bias"])

                if self._daily_loss_exceeded(session, profile):
                    self.log("Daily loss limit reached. Stopping session.", level=StrategyLog.LEVEL_WARNING)
                    session.status = TradingSession.STATUS_STOPPED
                    session.ended_at = timezone.now()
                    session.save(update_fields=["status", "ended_at"])
                    break

                if self._weekly_loss_exceeded(session.user, profile, session):
                    self.log("Weekly loss limit reached. Stopping session.", level=StrategyLog.LEVEL_WARNING)
                    session.status = TradingSession.STATUS_STOPPED
                    session.ended_at = timezone.now()
                    session.save(update_fields=["status", "ended_at"])
                    break

                open_trades = {
                    trade.symbol: trade
                    for trade in session.trades.filter(status=Trade.STATUS_OPEN)
                }

                for symbol, trade in open_trades.items():
                    if self._check_order_status(trade, current_time):
                        continue

                    data_5min = self.client.fetch_historical_data(token=trade.token, interval="FIVE_MINUTE", days=1)
                    if data_5min is None or len(data_5min) < 50:
                        continue

                    current_price = float(data_5min["close"].iloc[-1])
                    self._update_trailing_stop(trade)

                    if strategy.is_exit_time(current_time):
                        self._close_trade(trade, current_price, Trade.EXIT_TIME)
                        continue

                    if profile.is_live_trading_enabled:
                        meta = self._trade_meta(trade)
                        if meta.get("sl_order_id") and meta.get("target_order_id"):
                            continue

                    if trade.direction == Trade.DIRECTION_LONG:
                        if current_price <= float(trade.stop_loss):
                            trailing_active = float(trade.stop_loss) >= float(trade.entry_price)
                            reason = Trade.EXIT_TRAIL if trailing_active else Trade.EXIT_STOP
                            self._close_trade(trade, current_price, reason, broker_exit=profile.is_live_trading_enabled)
                            continue
                        if current_price >= float(trade.target_price):
                            self._close_trade(
                                trade,
                                current_price,
                                Trade.EXIT_TARGET,
                                broker_exit=profile.is_live_trading_enabled,
                            )
                            continue
                    else:
                        if current_price >= float(trade.stop_loss):
                            trailing_active = float(trade.stop_loss) <= float(trade.entry_price)
                            reason = Trade.EXIT_TRAIL if trailing_active else Trade.EXIT_STOP
                            self._close_trade(trade, current_price, reason, broker_exit=profile.is_live_trading_enabled)
                            continue
                        if current_price <= float(trade.target_price):
                            self._close_trade(
                                trade,
                                current_price,
                                Trade.EXIT_TARGET,
                                broker_exit=profile.is_live_trading_enabled,
                            )
                            continue

                if strategy.is_exit_time(current_time):
                    if not session.trades.filter(status=Trade.STATUS_OPEN).exists():
                        self.log("Exit time reached, session completed.")
                        session.status = TradingSession.STATUS_COMPLETED
                        session.ended_at = timezone.now()
                        session.save(update_fields=["status", "ended_at"])
                        break

                if session.trades.filter(status=Trade.STATUS_OPEN).exists():
                    time.sleep(settings.TRADING_POLL_SECONDS)
                    continue

                if not strategy.check_time_window(current_time):
                    time.sleep(settings.TRADING_POLL_SECONDS)
                    continue

                total_trades = session.trades.filter(
                    status__in=[Trade.STATUS_OPEN, Trade.STATUS_CLOSED],
                ).count()
                if total_trades >= session.max_trades_per_day:
                    time.sleep(settings.TRADING_POLL_SECONDS)
                    continue

                traded_symbols_today = set(
                    session.trades.filter(
                        status__in=[Trade.STATUS_OPEN, Trade.STATUS_CLOSED],
                    ).values_list("symbol", flat=True)
                )

                for stock in watchlist:
                    if stock.symbol in traded_symbols_today:
                        continue
                    total_trades = session.trades.filter(
                        status__in=[Trade.STATUS_OPEN, Trade.STATUS_CLOSED],
                    ).count()
                    if total_trades >= session.max_trades_per_day:
                        break

                    data_5min = self.client.fetch_historical_data(token=stock.token, interval="FIVE_MINUTE", days=7)
                    if data_5min is None or len(data_5min) < 50:
                        continue

                    opening_high, opening_low = self._get_or_create_opening_range(
                        session,
                        stock,
                        data_5min,
                        strategy,
                        reference_date=now.date(),
                    )
                    if opening_high is None:
                        continue

                    direction, signal_entry_price, reason = strategy.evaluate_entry(
                        data_5min=data_5min,
                        nifty_15min=nifty_15min,
                        opening_high=opening_high,
                        opening_low=opening_low,
                        current_time=current_time,
                        reference_date=now.date(),
                        client=self.client,
                    )
                    if not direction:
                        continue

                    entry_price, entry_buffer_percent, entry_buffer_points = self._buffered_entry_price(
                        signal_entry_price,
                        direction,
                        profile,
                    )
                    entry_meta = dict(getattr(strategy, "last_entry_meta", {}) or {})
                    size_multiplier = float(entry_meta.get("position_size_multiplier", 1.0))
                    gap_multiplier = float(entry_meta.get("gap_multiplier", 1.0))
                    trend_multiplier = float(entry_meta.get("trend_multiplier", 1.0))
                    stop_loss = opening_low if direction == Trade.DIRECTION_LONG else opening_high
                    if size_multiplier <= 0:
                        self.log(
                            f"Skipped {stock.symbol}: quantity resolved to 0 due to gap sizing rules.",
                            level=StrategyLog.LEVEL_INFO,
                            symbol=stock.symbol,
                        )
                        continue

                    risk_quantity = strategy.calculate_position_size(
                        entry_price,
                        stop_loss,
                        size_multiplier=size_multiplier,
                    )
                    if risk_quantity <= 0:
                        self.log(
                            f"Skipped {stock.symbol}: quantity resolved to 0 due to gap sizing rules.",
                            level=StrategyLog.LEVEL_INFO,
                            symbol=stock.symbol,
                        )
                        continue

                    if profile.is_live_trading_enabled:
                        available_margin = self.client.get_available_margin()
                        if available_margin <= 0:
                            self.log(
                                f"No available margin for {stock.symbol}",
                                level=StrategyLog.LEVEL_WARNING,
                                symbol=stock.symbol,
                            )
                            continue
                    else:
                        available_margin = float(profile.capital)

                    max_qty = self.client.get_max_quantity_for_margin(
                        entry_price,
                        available_margin,
                        margin_usage_percent=float(strategy.config.margin_usage_percent),
                        estimated_margin_factor=float(strategy.config.estimated_margin_factor),
                    )
                    if max_qty <= 0:
                        self.log(
                            f"Cannot trade {stock.symbol}: margin insufficient",
                            level=StrategyLog.LEVEL_WARNING,
                            symbol=stock.symbol,
                        )
                        continue

                    quantity = min(max_qty, max(1, int(risk_quantity)))
                    if quantity <= 0:
                        self.log(
                            f"Cannot trade {stock.symbol}: calculated quantity is zero",
                            level=StrategyLog.LEVEL_WARNING,
                            symbol=stock.symbol,
                        )
                        continue

                    target_price = strategy.calculate_target_price(entry_price, stop_loss, quantity)
                    order_id = ""
                    sl_order_id = ""
                    target_order_id = ""
                    broker_order_status = None

                    if profile.is_live_trading_enabled:
                        order_id = self.client.place_entry_order(
                            symbol=stock.symbol,
                            token=stock.token,
                            direction=direction,
                            entry_price=entry_price,
                            quantity=quantity,
                        )
                        if not order_id:
                            self.log(
                                f"Entry order failed for {stock.symbol}.",
                                level=StrategyLog.LEVEL_ERROR,
                                symbol=stock.symbol,
                            )
                            continue

                        if not self.client.wait_for_fill(order_id, timeout_sec=30):
                            self.log(
                                f"Entry order {order_id} not filled for {stock.symbol}.",
                                level=StrategyLog.LEVEL_ERROR,
                                symbol=stock.symbol,
                            )
                            self.client.cancel_order(order_id)
                            continue

                        broker_order_status = self.client.get_order_status(order_id)
                        if self.client.is_terminal_rejection_status(broker_order_status):
                            self.log(
                                f"Broker rejected/cancelled order {order_id} ({broker_order_status}).",
                                level=StrategyLog.LEVEL_ERROR,
                                symbol=stock.symbol,
                            )
                            continue

                        sl_order_id = self.client.place_stoploss_market_order(
                            symbol=stock.symbol,
                            token=stock.token,
                            direction=direction,
                            trigger_price=stop_loss,
                            quantity=quantity,
                        )
                        if not sl_order_id:
                            self.log(
                                f"SL order failed for {stock.symbol}; exiting at market.",
                                level=StrategyLog.LEVEL_ERROR,
                                symbol=stock.symbol,
                            )
                            self.client.place_market_order(stock.symbol, stock.token, direction, quantity)
                            continue

                        target_order_id = self.client.place_target_order(
                            symbol=stock.symbol,
                            token=stock.token,
                            direction=direction,
                            price=target_price,
                            quantity=quantity,
                        )
                        if not target_order_id:
                            self.log(
                                f"Target order failed for {stock.symbol}; trade will trail to exit.",
                                level=StrategyLog.LEVEL_WARNING,
                                symbol=stock.symbol,
                            )

                    Trade.objects.create(
                        session=session,
                        user=session.user,
                        symbol=stock.symbol,
                        token=stock.token,
                        direction=direction,
                        entry_price=round(entry_price, 2),
                        stop_loss=round(stop_loss, 2),
                        target_price=round(target_price, 2),
                        quantity=quantity,
                        risk_per_share=round(abs(entry_price - stop_loss), 4),
                        order_id=order_id,
                        status=Trade.STATUS_OPEN,
                        meta={
                            "signal_reason": reason,
                            "market_bias": session.market_bias,
                            "signal_entry_price": round(float(signal_entry_price), 2),
                            "order_entry_price": round(float(entry_price), 2),
                            "entry_buffer_percent": round(float(entry_buffer_percent), 3),
                            "entry_buffer_points": round(float(entry_buffer_points), 4),
                            "gap_type": entry_meta.get("gap_type"),
                            "gap_percent": entry_meta.get("gap_percent"),
                            "gap_status": entry_meta.get("gap_status"),
                            "gap_action": entry_meta.get("gap_action"),
                            "gap_quality_score": entry_meta.get("gap_quality_score"),
                            "previous_close": entry_meta.get("previous_close"),
                            "trend_15m": entry_meta.get("trend_15m"),
                            "trend_strength": entry_meta.get("trend_strength"),
                            "trend_distance_percent": entry_meta.get("trend_distance_percent"),
                            "trend_slope_percent": entry_meta.get("trend_slope_percent"),
                            "trend_status": entry_meta.get("trend_status"),
                            "trend_message": entry_meta.get("trend_message"),
                            "gap_multiplier": gap_multiplier,
                            "trend_multiplier": trend_multiplier,
                            "position_size_multiplier": size_multiplier,
                            "broker_order_status_at_entry": broker_order_status,
                            "entry_order_id": order_id,
                            "sl_order_id": sl_order_id,
                            "target_order_id": target_order_id,
                            "available_margin": round(float(available_margin), 2),
                            "margin_required_estimate": round(
                                float(entry_price) * quantity * float(strategy.config.estimated_margin_factor),
                                2,
                            ),
                        },
                    )
                    if size_multiplier < 1:
                        self.log(
                            (
                                f"{stock.symbol} position reduced: gap={gap_multiplier:.2f}, "
                                f"trend={trend_multiplier:.2f}, total={size_multiplier:.2f}."
                            ),
                            level=StrategyLog.LEVEL_INFO,
                            symbol=stock.symbol,
                        )
                    self.log(
                        f"{direction} signal {stock.symbol} @ {entry_price:.2f}, Qty={quantity}",
                        symbol=stock.symbol,
                    )
                    if entry_buffer_percent > 0:
                        self.log(
                            (
                                f"{stock.symbol} entry buffer applied: signal={float(signal_entry_price):.2f}, "
                                f"order={entry_price:.2f}, buffer={entry_buffer_percent:.3f}% ({entry_buffer_points:.4f})."
                            ),
                            symbol=stock.symbol,
                        )

                time.sleep(settings.TRADING_POLL_SECONDS)

        except Exception as exc:
            session = TradingSession.objects.filter(id=self.session_id).first()
            if session:
                session.status = TradingSession.STATUS_FAILED
                session.ended_at = timezone.now()
                session.save(update_fields=["status", "ended_at"])
            self.log(f"Runner error: {exc}", level=StrategyLog.LEVEL_ERROR)
        finally:
            if self.client:
                self.client.logout()

            session = TradingSession.objects.filter(id=self.session_id).first()
            if session and session.status == TradingSession.STATUS_RUNNING:
                session.status = TradingSession.STATUS_STOPPED
                session.ended_at = timezone.now()
                session.save(update_fields=["status", "ended_at"])

            RUNNERS.pop(self.session_id, None)


def start_session_runner(session_id: int) -> bool:
    existing = RUNNERS.get(session_id)
    if existing and existing.is_alive():
        return False

    runner = SessionRunner(session_id=session_id)
    RUNNERS[session_id] = runner
    runner.start()
    return True


def stop_session_runner(session_id: int):
    runner = RUNNERS.get(session_id)
    if runner:
        runner.stop()


def is_session_running(session_id: int) -> bool:
    runner = RUNNERS.get(session_id)
    return bool(runner and runner.is_alive())
