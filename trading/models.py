from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User


class Watchlist(models.Model):
    name = models.CharField(max_length=255)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='watchlists')

    def __str__(self):
        return self.name


class WatchlistStock(models.Model):
    watchlist = models.ForeignKey(Watchlist, on_delete=models.CASCADE, related_name='stocks')
    stock_symbol = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.stock_symbol} in {self.watchlist.name}"


class WatchlistSymbol(models.Model):
    symbol = models.CharField(max_length=32, unique=True)
    token = models.CharField(max_length=32)
    exchange = models.CharField(max_length=16, default="NSE")
    enabled = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["priority", "symbol"]

    def __str__(self):
        return self.symbol


class TradingSession(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_RUNNING = "RUNNING"
    STATUS_STOPPED = "STOPPED"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_STOPPED, "Stopped"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trading_sessions")
    trading_date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    market_bias = models.CharField(max_length=16, default="NEUTRAL")
    notes = models.TextField(blank=True)

    daily_loss_limit_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("3.00"))
    weekly_loss_limit_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("6.00"))
    max_trades_per_day = models.PositiveSmallIntegerField(default=2)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Session {self.id} - {self.user.username} ({self.status})"


class Trade(models.Model):
    DIRECTION_LONG = "LONG"
    DIRECTION_SHORT = "SHORT"
    DIRECTION_CHOICES = [(DIRECTION_LONG, "Long"), (DIRECTION_SHORT, "Short")]

    STATUS_OPEN = "OPEN"
    STATUS_CLOSED = "CLOSED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    EXIT_TARGET = "TARGET"
    EXIT_STOP = "STOP"
    EXIT_TRAIL = "TRAIL"
    EXIT_EMA = "EMA"
    EXIT_TIME = "TIME"
    EXIT_MANUAL = "MANUAL"
    EXIT_SIGNAL = "SIGNAL"

    session = models.ForeignKey(TradingSession, on_delete=models.CASCADE, related_name="trades")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trades")

    symbol = models.CharField(max_length=32)
    token = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES)

    entry_price = models.DecimalField(max_digits=12, decimal_places=2)
    stop_loss = models.DecimalField(max_digits=12, decimal_places=2)
    target_price = models.DecimalField(max_digits=12, decimal_places=2)
    exit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    quantity = models.PositiveIntegerField()
    risk_per_share = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    brokerage = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    pnl = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    exit_reason = models.CharField(max_length=16, default="")

    order_id = models.CharField(max_length=64, blank=True)
    entry_time = models.DateTimeField(default=timezone.now)
    exit_time = models.DateTimeField(null=True, blank=True)

    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-entry_time"]

    def __str__(self):
        return f"{self.symbol} {self.direction} ({self.status})"


class OpeningRange(models.Model):
    session = models.ForeignKey(TradingSession, on_delete=models.CASCADE, related_name="opening_ranges")
    symbol = models.CharField(max_length=32)
    token = models.CharField(max_length=32)
    high = models.DecimalField(max_digits=12, decimal_places=2)
    low = models.DecimalField(max_digits=12, decimal_places=2)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "symbol")
        ordering = ["symbol"]

    def __str__(self):
        return f"{self.symbol}: {self.high}/{self.low}"


class StrategyLog(models.Model):
    LEVEL_INFO = "INFO"
    LEVEL_WARNING = "WARN"
    LEVEL_ERROR = "ERROR"

    LEVEL_CHOICES = [
        (LEVEL_INFO, "Info"),
        (LEVEL_WARNING, "Warning"),
        (LEVEL_ERROR, "Error"),
    ]

    session = models.ForeignKey(TradingSession, on_delete=models.CASCADE, related_name="logs")
    symbol = models.CharField(max_length=32, blank=True)
    level = models.CharField(max_length=8, choices=LEVEL_CHOICES, default=LEVEL_INFO)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.level}: {self.message[:60]}"
