from django.contrib import admin

from .models import OpeningRange, StrategyLog, Trade, TradingSession, WatchlistSymbol


@admin.register(WatchlistSymbol)
class WatchlistSymbolAdmin(admin.ModelAdmin):
    list_display = ("symbol", "token", "exchange", "enabled", "priority")
    list_editable = ("enabled", "priority")
    search_fields = ("symbol", "token")


@admin.register(TradingSession)
class TradingSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "trading_date", "status", "market_bias", "started_at", "ended_at")
    list_filter = ("status", "trading_date")
    search_fields = ("user__username",)


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "symbol",
        "direction",
        "entry_price",
        "exit_price",
        "quantity",
        "pnl",
        "status",
        "exit_reason",
    )
    list_filter = ("direction", "status", "exit_reason", "symbol")
    search_fields = ("symbol", "order_id", "user__username")


@admin.register(OpeningRange)
class OpeningRangeAdmin(admin.ModelAdmin):
    list_display = ("session", "symbol", "high", "low", "captured_at")
    search_fields = ("symbol",)


@admin.register(StrategyLog)
class StrategyLogAdmin(admin.ModelAdmin):
    list_display = ("session", "symbol", "level", "message", "created_at")
    list_filter = ("level", "symbol")
    search_fields = ("message", "symbol")
