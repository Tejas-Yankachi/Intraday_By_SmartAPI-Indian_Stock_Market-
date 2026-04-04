from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def default_trade_start_time():
    return datetime.strptime("10:00", "%H:%M").time()


def default_trade_end_time():
    return datetime.strptime("12:30", "%H:%M").time()


def default_exit_time():
    return datetime.strptime("14:45", "%H:%M").time()


class UserTradingProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trading_profile",
    )
    profile_photo = models.ImageField(upload_to="profile_photos/", blank=True, null=True)
    smart_api_key = models.CharField(max_length=128, blank=True)
    smart_client_code = models.CharField(max_length=64, blank=True)
    smart_pin = models.CharField(max_length=16, blank=True)
    smart_totp_secret = models.CharField(max_length=128, blank=True)
    app_totp_secret = models.CharField(max_length=128, blank=True)
    enable_app_totp_login = models.BooleanField(default=False)

    capital = models.DecimalField(max_digits=12, decimal_places=2, default=10000)
    risk_per_trade_percent = models.DecimalField(max_digits=5, decimal_places=2, default=2)
    min_risk_amount = models.DecimalField(max_digits=12, decimal_places=2, default=5000)
    brokerage_per_trade = models.DecimalField(max_digits=10, decimal_places=2, default=40)
    entry_buffer_percent = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        default=Decimal("0.050"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("2.000"))],
        help_text="Applied only in live mode for ROBO limit entry. Example: 0.050 means 0.05%",
    )

    trade_start_time = models.TimeField(default=default_trade_start_time)
    trade_end_time = models.TimeField(default=default_trade_end_time)
    exit_time = models.TimeField(default=default_exit_time)

    is_live_trading_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Trading Profile: {self.user.username}"
