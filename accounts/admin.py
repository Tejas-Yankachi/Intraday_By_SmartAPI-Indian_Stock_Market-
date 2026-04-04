from django.contrib import admin

from .models import UserTradingProfile


@admin.register(UserTradingProfile)
class UserTradingProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "capital",
        "risk_per_trade_percent",
        "is_live_trading_enabled",
        "enable_app_totp_login",
        "updated_at",
    )
    search_fields = ("user__username", "smart_client_code")
