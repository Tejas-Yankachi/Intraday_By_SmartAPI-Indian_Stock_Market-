from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from trading.models import Trade, TradingSession


def session_snapshot(session: TradingSession) -> dict:
    trades = session.trades.all()
    closed = trades.filter(status=Trade.STATUS_CLOSED)

    total = closed.count()
    wins = closed.filter(pnl__gt=0).count()
    losses = closed.filter(pnl__lte=0).count()
    net_pnl = closed.aggregate(total=Sum("pnl")).get("total") or Decimal("0")

    return {
        "session_id": session.id,
        "status": session.status,
        "market_bias": session.market_bias,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / total) * 100, 2) if total else 0.0,
        "net_pnl": float(net_pnl),
    }


def monthly_traceback(user, year: int | None = None, month: int | None = None) -> dict:
    now = timezone.localtime()
    year = year or now.year
    month = month or now.month

    trades = (
        Trade.objects.filter(
            user=user,
            status=Trade.STATUS_CLOSED,
            entry_time__year=year,
            entry_time__month=month,
        )
        .order_by("entry_time")
        .select_related("session")
    )

    total = trades.count()
    wins = trades.filter(pnl__gt=0).count()
    losses = trades.filter(pnl__lte=0).count()
    net_pnl = trades.aggregate(total=Sum("pnl")).get("total") or Decimal("0")

    day_map = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0, "pnl": Decimal("0")})
    symbol_map = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0, "pnl": Decimal("0")})

    max_win_streak = 0
    max_loss_streak = 0
    current_win_streak = 0
    current_loss_streak = 0

    for trade in trades:
        day_key = trade.entry_time.date().isoformat()
        symbol_key = trade.symbol

        day_map[day_key]["total"] += 1
        symbol_map[symbol_key]["total"] += 1

        if trade.pnl > 0:
            day_map[day_key]["wins"] += 1
            symbol_map[symbol_key]["wins"] += 1
            current_win_streak += 1
            current_loss_streak = 0
        else:
            day_map[day_key]["losses"] += 1
            symbol_map[symbol_key]["losses"] += 1
            current_loss_streak += 1
            current_win_streak = 0

        max_win_streak = max(max_win_streak, current_win_streak)
        max_loss_streak = max(max_loss_streak, current_loss_streak)

        day_map[day_key]["pnl"] += trade.pnl
        symbol_map[symbol_key]["pnl"] += trade.pnl

    day_breakdown = []
    for day in sorted(day_map.keys()):
        row = day_map[day]
        day_breakdown.append(
            {
                "day": day,
                "total": row["total"],
                "wins": row["wins"],
                "losses": row["losses"],
                "win_rate": round((row["wins"] / row["total"]) * 100, 2) if row["total"] else 0.0,
                "net_pnl": float(row["pnl"]),
            }
        )

    symbol_breakdown = []
    for symbol in sorted(symbol_map.keys()):
        row = symbol_map[symbol]
        symbol_breakdown.append(
            {
                "symbol": symbol,
                "total": row["total"],
                "wins": row["wins"],
                "losses": row["losses"],
                "win_rate": round((row["wins"] / row["total"]) * 100, 2) if row["total"] else 0.0,
                "net_pnl": float(row["pnl"]),
            }
        )

    return {
        "month": f"{year:04d}-{month:02d}",
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / total) * 100, 2) if total else 0.0,
        "net_pnl": float(net_pnl),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "daily_breakdown": day_breakdown,
        "symbol_breakdown": symbol_breakdown,
    }
