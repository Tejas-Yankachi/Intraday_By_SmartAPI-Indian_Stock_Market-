from __future__ import annotations

from datetime import timedelta
from textwrap import shorten
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from trading.models import OpeningRange, StrategyLog, Trade, TradingSession, WatchlistSymbol

from .analytics import session_snapshot

ACTIVE_SESSION_STATUSES = {
    TradingSession.STATUS_RUNNING,
    TradingSession.STATUS_PENDING,
}


def _fmt_dt(value) -> str:
    if value is None:
        return "--"
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_money(value) -> str:
    if value is None or value == "":
        return "--"
    return f"₹{float(value):,.2f}"


def _fmt_price(value) -> str:
    if value is None or value == "":
        return "--"
    return f"{float(value):,.2f}"


def _fmt_percent(value) -> str:
    if value is None or value == "":
        return "--"
    return f"{float(value):.2f}%"


def _fmt_duration(start, end=None) -> str:
    if start is None:
        return "--"
    end = end or timezone.now()
    delta = end - start
    if delta.total_seconds() < 0:
        delta = timedelta(0)
    seconds = int(delta.total_seconds())
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _scope_queryset(session_id: int | None, user_id: int | None):
    queryset = TradingSession.objects.select_related("user")
    if session_id is not None:
        return queryset.filter(id=session_id), f"session #{session_id}", False
    if user_id is not None:
        return queryset.filter(user_id=user_id), f"user #{user_id}", False
    return queryset.filter(status__in=ACTIVE_SESSION_STATUSES), "active sessions", True


def _opening_ranges_for(session: TradingSession, limit: int) -> list[dict[str, Any]]:
    rows = []
    for item in session.opening_ranges.all().order_by("symbol")[:limit]:
        rows.append(
            {
                "symbol": item.symbol,
                "pdh": float(item.high),
                "pdl": float(item.low),
                "captured_at": timezone.localtime(item.captured_at),
            }
        )
    return rows


def _open_trades_for(session: TradingSession, limit: int) -> list[dict[str, Any]]:
    rows = []
    now = timezone.now()
    for trade in session.trades.filter(status=Trade.STATUS_OPEN).order_by("-entry_time")[:limit]:
        meta = trade.meta if isinstance(trade.meta, dict) else {}
        rows.append(
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "direction": trade.direction,
                "entry_price": float(trade.entry_price),
                "stop_loss": float(trade.stop_loss),
                "target_price": float(trade.target_price),
                "quantity": trade.quantity,
                "order_id": trade.order_id or str(meta.get("entry_order_id") or "--"),
                "age": _fmt_duration(trade.entry_time, now),
                "meta": meta,
            }
        )
    return rows


def _recent_logs_for(session: TradingSession, limit: int) -> list[dict[str, Any]]:
    rows = []
    for log in session.logs.all()[:limit]:
        rows.append(
            {
                "time": timezone.localtime(log.created_at).strftime("%H:%M:%S"),
                "level": log.level,
                "symbol": log.symbol,
                "message": log.message,
            }
        )
    return rows


def build_terminal_dashboard_state(
    session_id: int | None = None,
    user_id: int | None = None,
    max_sessions: int = 5,
    max_trades: int = 5,
    max_logs: int = 8,
) -> dict[str, Any]:
    now = timezone.localtime()
    scope_queryset, scope_label, allow_recent_fallback = _scope_queryset(session_id, user_id)

    sessions = list(scope_queryset.order_by("-started_at")[:max_sessions])
    scope_note = ""

    if not sessions and session_id is not None:
        return {
            "generated_at": now,
            "scope_label": scope_label,
            "scope_note": f"Session #{session_id} was not found.",
            "summary": {},
            "sessions": [],
            "error": f"Session #{session_id} not found.",
        }

    if not sessions and allow_recent_fallback:
        scope_note = "No active sessions found. Showing recent sessions instead."
        scope_queryset = TradingSession.objects.select_related("user")
        sessions = list(scope_queryset.order_by("-started_at")[:max_sessions])

    today = now.date()
    summary = {
        "enabled_watchlist": WatchlistSymbol.objects.filter(enabled=True).count(),
        "active_sessions": TradingSession.objects.filter(status__in=ACTIVE_SESSION_STATUSES).count(),
        "running_sessions": TradingSession.objects.filter(status=TradingSession.STATUS_RUNNING).count(),
        "pending_sessions": TradingSession.objects.filter(status=TradingSession.STATUS_PENDING).count(),
        "open_trades": Trade.objects.filter(status=Trade.STATUS_OPEN).count(),
        "closed_trades_today": Trade.objects.filter(status=Trade.STATUS_CLOSED, exit_time__date=today).count(),
    }

    if summary["closed_trades_today"]:
        closed_today_pnl = (
            Trade.objects.filter(status=Trade.STATUS_CLOSED, exit_time__date=today)
            .aggregate(total_pnl=Sum("pnl"))
            .get("total_pnl")
        )
        summary["closed_trades_today_pnl"] = float(closed_today_pnl or 0)
    else:
        summary["closed_trades_today_pnl"] = 0.0

    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        snapshot = session_snapshot(session)
        open_trades = _open_trades_for(session, max_trades)
        logs = _recent_logs_for(session, max_logs)
        opening_ranges = _opening_ranges_for(session, max_trades)

        started_at = timezone.localtime(session.started_at)
        ended_at = timezone.localtime(session.ended_at) if session.ended_at else None
        current_end = ended_at or now

        session_rows.append(
            {
                "id": session.id,
                "user": session.user.username,
                "status": session.status,
                "trading_date": session.trading_date.isoformat(),
                "market_bias": session.market_bias,
                "started_at": started_at,
                "ended_at": ended_at,
                "age": _fmt_duration(started_at, current_end),
                "closed": snapshot["total_trades"],
                "wins": snapshot["wins"],
                "losses": snapshot["losses"],
                "win_rate": snapshot["win_rate"],
                "net_pnl": snapshot["net_pnl"],
                "max_trades_per_day": session.max_trades_per_day,
                "daily_loss_limit_percent": float(session.daily_loss_limit_percent),
                "weekly_loss_limit_percent": float(session.weekly_loss_limit_percent),
                "open_trade_count": len(open_trades),
                "open_trades": open_trades,
                "opening_ranges": opening_ranges,
                "logs": logs,
                "notes": session.notes.strip(),
            }
        )

    return {
        "generated_at": now,
        "scope_label": scope_label,
        "scope_note": scope_note,
        "summary": summary,
        "sessions": session_rows,
        "error": None,
    }


def _render_open_trades(trades: list[dict[str, Any]]) -> list[str]:
    if not trades:
        return ["    No open trades."]

    lines = [
        "    SYMBOL           DIR    ENTRY      SL         TARGET     QTY   AGE       ORDER",
        "    " + "-" * 75,
    ]
    for trade in trades:
        lines.append(
            f"    {trade['symbol'][:15]:<15} "
            f"{trade['direction'][:5]:<5} "
            f"{_fmt_price(trade['entry_price']):>9} "
            f"{_fmt_price(trade['stop_loss']):>10} "
            f"{_fmt_price(trade['target_price']):>10} "
            f"{trade['quantity']:>5} "
            f"{trade['age']:>8} "
            f"{str(trade['order_id'])[:10]:<10}"
        )
    return lines


def _render_opening_ranges(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["    No PDH/PDL levels captured yet."]

    lines = [
        "    SYMBOL           PDH        PDL        CAPTURED AT",
        "    " + "-" * 60,
    ]
    for row in rows:
        lines.append(
            f"    {row['symbol'][:15]:<15} "
            f"{_fmt_price(row['pdh']):>9} "
            f"{_fmt_price(row['pdl']):>9} "
            f"{_fmt_dt(row['captured_at'])}"
        )
    return lines


def _render_logs(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["    No strategy logs yet."]

    lines = ["    TIME     LEVEL  SYMBOL        MESSAGE", "    " + "-" * 78]
    for row in rows:
        lines.append(
            "    "
            f"{row['time']}  "
            f"{row['level']:<5}  "
            f"{(row['symbol'] or '--')[:12]:<12}  "
            f"{shorten(row['message'], width=88, placeholder='...')}"
        )
    return lines


def render_terminal_dashboard(state: dict[str, Any]) -> str:
    now = state.get("generated_at")
    summary = state.get("summary", {})
    sessions = state.get("sessions", [])
    scope_label = state.get("scope_label", "sessions")
    scope_note = state.get("scope_note", "")
    error = state.get("error")

    lines: list[str] = []
    lines.append("╔══════════════════════════════════════════════════════════════════════════════╗")
    lines.append("║                         BACKEND EXECUTION DASHBOARD                          ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════════════╝")
    lines.append(f"Updated: {_fmt_dt(now)} | Scope: {scope_label}")
    if scope_note:
        lines.append(scope_note)
    lines.append(
        "Summary: "
        f"Watchlist {summary.get('enabled_watchlist', 0)} | "
        f"Active {summary.get('active_sessions', 0)} | "
        f"Running {summary.get('running_sessions', 0)} | "
        f"Pending {summary.get('pending_sessions', 0)} | "
        f"Open Trades {summary.get('open_trades', 0)} | "
        f"Closed Today {summary.get('closed_trades_today', 0)} | "
        f"Closed Today P&L {_fmt_money(summary.get('closed_trades_today_pnl', 0.0))}"
    )
    lines.append("")

    if error:
        lines.append(f"ERROR: {error}")
        return "\n".join(lines)

    if not sessions:
        lines.append("No backend sessions found.")
        return "\n".join(lines)

    for idx, session in enumerate(sessions):
        if idx > 0:
            lines.append("")
        lines.append("-" * 78)
        lines.append(
            f"Session #{session['id']} | {session['user']} | {session['status']} | "
            f"Bias: {session['market_bias']} | Date: {session['trading_date']}"
        )
        lines.append(
            f"Started: {_fmt_dt(session['started_at'])} | "
            f"Ended: {_fmt_dt(session['ended_at'])} | "
            f"Age: {session['age']}"
        )
        lines.append(
            f"Closed Trades: {session['closed']} | Wins: {session['wins']} | Losses: {session['losses']} | "
            f"Win Rate: {_fmt_percent(session['win_rate'])} | Net P&L: {_fmt_money(session['net_pnl'])}"
        )
        lines.append(
            f"Open Trades: {session['open_trade_count']} | Max Trades/Day: {session['max_trades_per_day']} | "
            f"Daily Loss %: {_fmt_percent(session['daily_loss_limit_percent'])} | "
            f"Weekly Loss %: {_fmt_percent(session['weekly_loss_limit_percent'])}"
        )
        if session["notes"]:
            lines.append(f"Notes: {shorten(session['notes'], width=100, placeholder='...')}")
        lines.append("  Open Trades")
        lines.extend(_render_open_trades(session["open_trades"]))
        lines.append("  PDH/PDL Levels")
        lines.extend(_render_opening_ranges(session["opening_ranges"]))
        lines.append("  Recent Logs")
        lines.extend(_render_logs(session["logs"]))

    return "\n".join(lines)
