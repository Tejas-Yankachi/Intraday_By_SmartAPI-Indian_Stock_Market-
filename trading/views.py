from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from trading.models import (
    StrategyLog,
    Trade,
    TradingSession,
    WatchlistSymbol,
)
from trading.services.analytics import monthly_traceback, session_snapshot
from trading.services.dashboard_scanner import get_live_dashboard_snapshot
from trading.services.terminal_dashboard import build_terminal_dashboard_state, render_terminal_dashboard
from trading.services.runtime import is_session_running, start_session_runner, stop_session_runner
from trading.services.smartapi_gateway import SmartAPIClient

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9&-]{1,32}$")


def _normalize_nse_symbol(symbol: str) -> str:
    value = (symbol or "").strip().upper()
    if value.endswith(".NS"):
        value = value[:-3]
    if value.endswith("-EQ"):
        value = value[:-3]
    return value


def _next_watchlist_priority() -> int:
    max_priority = WatchlistSymbol.objects.aggregate(value=Max("priority")).get("value")
    if max_priority is None:
        return 1
    return int(max_priority) + 1



def _format_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _time_to_exit_text(now_time, exit_time) -> str:
    now_dt = datetime.combine(datetime.today(), now_time)
    exit_dt = datetime.combine(datetime.today(), exit_time)
    return _format_hms(int((exit_dt - now_dt).total_seconds()))


def _target_progress_percent(trade: Trade, current_price: float) -> float:
    entry = float(trade.entry_price)
    target = float(trade.target_price)

    if trade.direction == Trade.DIRECTION_LONG:
        denominator = target - entry
        progress = ((current_price - entry) / denominator) * 100 if denominator else 0
    else:
        denominator = entry - target
        progress = ((entry - current_price) / denominator) * 100 if denominator else 0

    return round(max(0, min(progress, 100)), 2)


def _attempt_live_broker_exit(client: SmartAPIClient, profile, trade: Trade) -> tuple[bool, str]:
    if not bool(profile.is_live_trading_enabled):
        return True, ""

    ok, exit_message, exit_response = client.exit_position_market(
        symbol=trade.symbol,
        token=trade.token,
        direction=trade.direction,
        quantity=trade.quantity,
    )
    if not ok:
        return False, exit_message

    exclude_order_ids = set()
    if exit_response:
        exit_order_id = client.extract_order_id(exit_response)
        if exit_order_id:
            exclude_order_ids.add(exit_order_id)
    cancelled_count, cancel_message = client.cancel_pending_orders_for_symbol(
        symbol=trade.symbol,
        token=trade.token,
        exclude_order_ids=exclude_order_ids,
    )
    note = exit_message
    if cancel_message:
        note = f"{note} {cancel_message}".strip()
    if cancelled_count > 0:
        note = f"{note} Cancelled {cancelled_count} pending order(s).".strip()
    return True, note


def _risk_snapshot(user, profile, latest_session) -> dict:
    now_local = timezone.localtime()
    today = now_local.date()
    week_start = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    capital = float(profile.capital)
    risk_percent = float(profile.risk_per_trade_percent)
    risk_per_trade_amount = capital * (risk_percent / 100)

    daily_limit_percent = float(latest_session.daily_loss_limit_percent) if latest_session else 3.0
    weekly_limit_percent = float(latest_session.weekly_loss_limit_percent) if latest_session else 6.0
    max_trades_per_day = int(latest_session.max_trades_per_day) if latest_session else 2

    daily_loss_limit_amount = capital * (daily_limit_percent / 100)
    weekly_loss_limit_amount = capital * (weekly_limit_percent / 100)

    closed_today = Trade.objects.filter(
        user=user,
        status=Trade.STATUS_CLOSED,
        exit_time__date=today,
    ).order_by("-exit_time")
    closed_week = Trade.objects.filter(
        user=user,
        status=Trade.STATUS_CLOSED,
        exit_time__gte=week_start,
    )

    today_realized_pnl = sum(float(v or 0) for v in closed_today.values_list("pnl", flat=True))
    week_realized_pnl = sum(float(v or 0) for v in closed_week.values_list("pnl", flat=True))

    today_loss_amount = max(0.0, -today_realized_pnl)
    week_loss_amount = max(0.0, -week_realized_pnl)

    daily_loss_used_percent = (today_loss_amount / daily_loss_limit_amount * 100) if daily_loss_limit_amount else 0.0
    weekly_loss_used_percent = (week_loss_amount / weekly_loss_limit_amount * 100) if weekly_loss_limit_amount else 0.0

    trades_taken_today = Trade.objects.filter(user=user, entry_time__date=today).count()
    trades_remaining = max(0, max_trades_per_day - trades_taken_today)

    consecutive_losses_today = 0
    for pnl in closed_today.values_list("pnl", flat=True):
        if float(pnl or 0) <= 0:
            consecutive_losses_today += 1
        else:
            break

    current_equity = capital + sum(
        float(v or 0)
        for v in Trade.objects.filter(user=user, status=Trade.STATUS_CLOSED).values_list("pnl", flat=True)
    )
    current_drawdown_percent = ((capital - current_equity) / capital * 100) if capital else 0.0
    current_drawdown_percent = max(0.0, current_drawdown_percent)
    drawdown_limit_percent = 20.0

    block_new_entries = any(
        [
            today_loss_amount >= daily_loss_limit_amount if daily_loss_limit_amount else False,
            week_loss_amount >= weekly_loss_limit_amount if weekly_loss_limit_amount else False,
            trades_taken_today >= max_trades_per_day,
            consecutive_losses_today >= 2,
            current_drawdown_percent >= drawdown_limit_percent,
        ]
    )

    return {
        "risk_per_trade_amount": round(risk_per_trade_amount, 2),
        "daily_loss_limit_amount": round(daily_loss_limit_amount, 2),
        "weekly_loss_limit_amount": round(weekly_loss_limit_amount, 2),
        "today_loss_amount": round(today_loss_amount, 2),
        "week_loss_amount": round(week_loss_amount, 2),
        "daily_loss_used_percent": round(daily_loss_used_percent, 2),
        "weekly_loss_used_percent": round(weekly_loss_used_percent, 2),
        "trades_taken_today": trades_taken_today,
        "max_trades_per_day": max_trades_per_day,
        "trades_remaining": trades_remaining,
        "consecutive_losses_today": consecutive_losses_today,
        "current_drawdown_percent": round(current_drawdown_percent, 2),
        "drawdown_limit_percent": drawdown_limit_percent,
        "block_new_entries": block_new_entries,
    }


@login_required
def dashboard_view(request):
    running_session = (
        TradingSession.objects.filter(
            user=request.user,
            status__in=[TradingSession.STATUS_RUNNING, TradingSession.STATUS_PENDING],
        )
        .order_by("-started_at")
        .first()
    )

    latest_session = (
        running_session
        or TradingSession.objects.filter(user=request.user).order_by("-started_at").first()
    )

    session_data = session_snapshot(latest_session) if latest_session else None
    month_data = monthly_traceback(request.user)

    context = {
        "profile": request.user.trading_profile,
        "watchlist": WatchlistSymbol.objects.order_by("priority", "symbol"),
        "latest_session": latest_session,
        "session_data": session_data,
        "month_data": month_data,
        "open_trades": Trade.objects.filter(user=request.user, status=Trade.STATUS_OPEN),
        "recent_logs": StrategyLog.objects.filter(session=latest_session)[:20] if latest_session else [],
        "is_live_runner": bool(latest_session and is_session_running(latest_session.id)),
    }
    return render(request, "trading/dashboard.html", context)


@login_required
@require_GET
def watchlist_chart_view(request, symbol_id: int):
    selected_symbol = get_object_or_404(WatchlistSymbol, id=symbol_id)
    context = {
        "selected_symbol": selected_symbol,
        "watchlist": WatchlistSymbol.objects.order_by("priority", "symbol"),
        "tradingview_symbol": f"{selected_symbol.exchange}:{selected_symbol.symbol}",
    }
    return render(request, "trading/watchlist_chart.html", context)


@login_required
@require_POST
def add_watchlist_symbol_view(request):
    symbol_input = request.POST.get("symbol", "")
    token_input = (request.POST.get("token", "") or "").strip()
    symbol = _normalize_nse_symbol(symbol_input)

    if not symbol:
        messages.error(request, "Enter an NSE symbol.")
        return redirect("trading:dashboard")

    if not SYMBOL_PATTERN.match(symbol):
        messages.error(request, "Use a valid NSE symbol (letters, numbers, '&' or '-').")
        return redirect("trading:dashboard")

    resolved = None
    if not token_input:
        profile = request.user.trading_profile
        client = SmartAPIClient.from_user_profile(profile)
        resolved = client.resolve_nse_symbol_token(symbol)
        if resolved is None:
            messages.error(
                request,
                "Token lookup failed. Enter token manually or verify SmartAPI credentials/network in profile.",
            )
            return redirect("trading:dashboard")

    token = token_input or resolved["token"]
    final_symbol = resolved["symbol"] if resolved else symbol
    defaults = {
        "token": token,
        "exchange": "NSE",
        "enabled": True,
    }

    obj = WatchlistSymbol.objects.filter(symbol=final_symbol).first()
    if obj:
        for field, value in defaults.items():
            setattr(obj, field, value)
        obj.save(update_fields=["token", "exchange", "enabled"])
        messages.success(request, f"{final_symbol} updated in NSE watchlist.")
    else:
        WatchlistSymbol.objects.create(
            symbol=final_symbol,
            priority=_next_watchlist_priority(),
            **defaults,
        )
        messages.success(request, f"{final_symbol} added to NSE watchlist.")
    return redirect("trading:dashboard")


@login_required
@require_POST
def toggle_watchlist_symbol_view(request, symbol_id: int):
    symbol_row = get_object_or_404(WatchlistSymbol, id=symbol_id)
    symbol_row.enabled = not symbol_row.enabled
    symbol_row.save(update_fields=["enabled"])

    state_text = "enabled" if symbol_row.enabled else "disabled"
    messages.success(request, f"{symbol_row.symbol} {state_text} in watchlist.")
    return redirect("trading:dashboard")


@login_required
@require_POST
def remove_watchlist_symbol_view(request, symbol_id: int):
    symbol_row = get_object_or_404(WatchlistSymbol, id=symbol_id)
    symbol_name = symbol_row.symbol
    symbol_row.delete()
    messages.success(request, f"{symbol_name} removed from watchlist.")
    return redirect("trading:dashboard")


@login_required
@require_POST
def start_session_view(request):
    profile = request.user.trading_profile
    if not SmartAPIClient.from_user_profile(profile).credentials.is_valid():
        messages.error(request, "Please configure SmartAPI credentials in profile before starting.")
        return redirect("accounts:profile")

    running = TradingSession.objects.filter(
        user=request.user,
        status__in=[TradingSession.STATUS_RUNNING, TradingSession.STATUS_PENDING],
    ).first()
    if running:
        messages.warning(request, f"Session {running.id} is already active.")
        return redirect("trading:dashboard")

    if not WatchlistSymbol.objects.filter(enabled=True).exists():
        messages.error(request, "Add and enable at least one NSE stock in watchlist before starting.")
        return redirect("trading:dashboard")

    session = TradingSession.objects.create(
        user=request.user,
        status=TradingSession.STATUS_PENDING,
        max_trades_per_day=2,
    )

    started = start_session_runner(session.id)
    if not started:
        messages.error(request, "Unable to start session runner.")
    else:
        messages.success(request, f"Session {session.id} started.")

    return redirect("trading:dashboard")


@login_required
@require_POST
def stop_session_view(request):
    session = (
        TradingSession.objects.filter(
            user=request.user,
            status__in=[TradingSession.STATUS_RUNNING, TradingSession.STATUS_PENDING],
        )
        .order_by("-started_at")
        .first()
    )

    if not session:
        messages.info(request, "No active session found.")
        return redirect("trading:dashboard")

    stop_session_runner(session.id)
    session.status = TradingSession.STATUS_STOPPED
    session.ended_at = timezone.now()
    session.save(update_fields=["status", "ended_at"])

    StrategyLog.objects.create(session=session, level=StrategyLog.LEVEL_WARNING, message="Session stopped by user")
    messages.success(request, f"Session {session.id} stopped.")
    return redirect("trading:dashboard")


@login_required
@require_POST
def emergency_exit_view(request):
    open_trades = Trade.objects.filter(user=request.user, status=Trade.STATUS_OPEN).select_related("session")
    if not open_trades.exists():
        messages.info(request, "No open trades to close.")
        return redirect("trading:dashboard")

    profile = request.user.trading_profile
    client = SmartAPIClient.from_user_profile(profile)
    if profile.is_live_trading_enabled:
        connected, message = client.validate_login()
        if not connected:
            messages.error(request, f"Broker exit unavailable: {message}")
            return redirect("trading:dashboard")
    else:
        client.validate_login()

    closed = 0
    for trade in open_trades:
        broker_ok, broker_note = _attempt_live_broker_exit(client, profile, trade)
        if not broker_ok:
            StrategyLog.objects.create(
                session=trade.session,
                symbol=trade.symbol,
                level=StrategyLog.LEVEL_ERROR,
                message=f"Emergency exit failed at broker: {broker_note}",
            )
            continue

        data = client.fetch_historical_data(token=trade.token, interval="FIVE_MINUTE", days=1)
        exit_price = float(data["close"].iloc[-1]) if data is not None and len(data) else float(trade.entry_price)

        if trade.direction == Trade.DIRECTION_LONG:
            gross = (exit_price - float(trade.entry_price)) * trade.quantity
        else:
            gross = (float(trade.entry_price) - exit_price) * trade.quantity

        net_pnl = gross - float(profile.brokerage_per_trade)
        trade.exit_price = round(exit_price, 2)
        trade.exit_time = timezone.now()
        trade.exit_reason = Trade.EXIT_MANUAL
        trade.pnl = round(net_pnl, 2)
        trade.brokerage = profile.brokerage_per_trade
        trade.status = Trade.STATUS_CLOSED
        trade.save(update_fields=["exit_price", "exit_time", "exit_reason", "pnl", "brokerage", "status"])

        StrategyLog.objects.create(
            session=trade.session,
            symbol=trade.symbol,
            level=StrategyLog.LEVEL_WARNING,
            message=f"Emergency manual exit. Net P&L ₹{net_pnl:.2f}. {broker_note}".strip(),
        )
        closed += 1

    if closed:
        messages.success(request, f"Emergency exit completed for {closed} open trade(s).")
    else:
        messages.warning(request, "No trades were closed. Check broker connectivity and open orders.")
    return redirect("trading:dashboard")


@login_required
@require_GET
def dashboard_data_view(request):
    profile = request.user.trading_profile
    live_scan = get_live_dashboard_snapshot(request.user)
    price_map = live_scan.get("price_map", {})

    latest_session = TradingSession.objects.filter(user=request.user).order_by("-started_at").first()
    month_data = monthly_traceback(request.user)
    now_local = timezone.localtime()

    open_trades = []
    for trade in Trade.objects.filter(user=request.user, status=Trade.STATUS_OPEN):
        current_price = float(price_map.get(trade.symbol, float(trade.entry_price)))
        if trade.direction == Trade.DIRECTION_LONG:
            gross_unrealized = (current_price - float(trade.entry_price)) * trade.quantity
            trailing_active = float(trade.stop_loss) >= float(trade.entry_price)
        else:
            gross_unrealized = (float(trade.entry_price) - current_price) * trade.quantity
            trailing_active = float(trade.stop_loss) <= float(trade.entry_price)

        unrealized_pnl = gross_unrealized - float(profile.brokerage_per_trade)
        entry_value = float(trade.entry_price) * trade.quantity
        unrealized_pnl_percent = (unrealized_pnl / entry_value) * 100 if entry_value else 0
        time_in_trade_seconds = int((now_local - timezone.localtime(trade.entry_time)).total_seconds())

        open_trades.append(
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "direction": trade.direction,
                "entry_price": float(trade.entry_price),
                "current_price": round(current_price, 2),
                "stop_loss": float(trade.stop_loss),
                "target_price": float(trade.target_price),
                "target_progress_percent": _target_progress_percent(trade, current_price),
                "quantity": trade.quantity,
                "entry_time": timezone.localtime(trade.entry_time).strftime("%H:%M:%S"),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pnl_percent": round(unrealized_pnl_percent, 2),
                "trailing_status": "TRAILING/BE" if trailing_active else "INITIAL",
                "time_in_trade": _format_hms(time_in_trade_seconds),
                "time_until_exit": _time_to_exit_text(now_local.time(), profile.exit_time),
            }
        )

    logs = [
        {
            "time": timezone.localtime(log.created_at).strftime("%H:%M:%S"),
            "level": log.level,
            "symbol": log.symbol,
            "message": log.message,
        }
        for log in StrategyLog.objects.filter(session=latest_session)[:20]
    ] if latest_session else []

    session_data = session_snapshot(latest_session) if latest_session else None

    payload = {
        "server_time": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
        "trading_mode": "LIVE" if profile.is_live_trading_enabled else "PAPER",
        "session": session_data,
        "api_connection": live_scan.get("api_connection", {}),
        "market_context": live_scan.get("market_context", {}),
        "indices": live_scan.get("indices", []),
        "funds": live_scan.get("funds", {}),
        "risk": _risk_snapshot(request.user, profile, latest_session),
        "signal_table": live_scan.get("signal_table", []),
        "opening_ranges": live_scan.get("opening_ranges", []),
        "backend_execution": render_terminal_dashboard(
            build_terminal_dashboard_state(
                user_id=request.user.id,
                max_sessions=3,
                max_trades=3,
                max_logs=5,
            )
        ),
        "month_traceback": month_data,
        "open_trades": open_trades,
        "logs": logs,
    }
    return JsonResponse(payload)


@login_required
@require_GET
def monthly_traceback_view(request):
    year = request.GET.get("year")
    month = request.GET.get("month")

    try:
        year = int(year) if year else None
        month = int(month) if month else None
    except ValueError:
        return JsonResponse({"error": "year and month must be numeric"}, status=400)

    data = monthly_traceback(request.user, year=year, month=month)
    return JsonResponse(data)


@login_required
@require_POST
def close_trade_view(request, trade_id: int):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    trade = get_object_or_404(Trade, id=trade_id, user=request.user)
    if trade.status != Trade.STATUS_OPEN:
        if is_ajax:
            return JsonResponse({"success": False, "message": "Trade already closed."}, status=400)
        messages.info(request, "Trade already closed.")
        return redirect("trading:dashboard")

    profile = request.user.trading_profile
    client = SmartAPIClient.from_user_profile(profile)
    if profile.is_live_trading_enabled:
        connected, message = client.validate_login()
        if not connected:
            if is_ajax:
                return JsonResponse({"success": False, "message": f"Broker exit unavailable: {message}"}, status=400)
            messages.error(request, f"Broker exit unavailable: {message}")
            return redirect("trading:dashboard")
    else:
        client.validate_login()

    broker_ok, broker_note = _attempt_live_broker_exit(client, profile, trade)
    if not broker_ok:
        if is_ajax:
            return JsonResponse({"success": False, "message": broker_note}, status=400)
        messages.error(request, f"Manual close failed at broker: {broker_note}")
        return redirect("trading:dashboard")

    data = client.fetch_historical_data(token=trade.token, interval="FIVE_MINUTE", days=1)
    exit_price = float(data["close"].iloc[-1]) if data is not None and len(data) else float(trade.entry_price)

    if trade.direction == Trade.DIRECTION_LONG:
        gross = (exit_price - float(trade.entry_price)) * trade.quantity
    else:
        gross = (float(trade.entry_price) - exit_price) * trade.quantity

    net_pnl = gross - float(profile.brokerage_per_trade)
    trade.exit_price = round(exit_price, 2)
    trade.exit_time = timezone.now()
    trade.exit_reason = Trade.EXIT_MANUAL
    trade.pnl = round(net_pnl, 2)
    trade.brokerage = Decimal(profile.brokerage_per_trade)
    trade.status = Trade.STATUS_CLOSED
    trade.save(update_fields=["exit_price", "exit_time", "exit_reason", "pnl", "brokerage", "status"])

    StrategyLog.objects.create(
        session=trade.session,
        symbol=trade.symbol,
        level=StrategyLog.LEVEL_WARNING,
        message=f"Manual trade close by user. Net P&L ₹{net_pnl:.2f}. {broker_note}".strip(),
    )
    if is_ajax:
        return JsonResponse({"success": True, "message": f"{trade.symbol} closed.", "pnl": float(trade.pnl)})
    messages.success(request, f"Trade {trade.symbol} closed manually.")
    return redirect("trading:dashboard")
