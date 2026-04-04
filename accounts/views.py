import binascii
from datetime import datetime
import re
from urllib.parse import urlsplit

import pyotp
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from trading.services.smartapi_gateway import SmartAPIClient

from .forms import RegistrationForm, TOTPAuthenticationForm, TradingProfileForm


_POST_ONLY_REDIRECT_PATHS = {
    "/dashboard/start/",
    "/dashboard/stop/",
    "/dashboard/emergency-exit/",
}
_POST_ONLY_CLOSE_TRADE_PATTERN = re.compile(r"^/dashboard/trade/\d+/close/$")


def _safe_totp(secret: str | None):
    if not secret:
        return None

    try:
        return pyotp.TOTP(secret).now()
    except (binascii.Error, TypeError, ValueError):
        return None


class UserLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = TOTPAuthenticationForm

    def get_success_url(self):
        redirect_to = self.get_redirect_url()
        if not redirect_to:
            return reverse("trading:dashboard")

        redirect_path = urlsplit(redirect_to).path
        if (
            redirect_path in _POST_ONLY_REDIRECT_PATHS
            or _POST_ONLY_CLOSE_TRADE_PATTERN.fullmatch(redirect_path)
        ):
            return reverse("trading:dashboard")

        return redirect_to


def register_view(request):
    if request.user.is_authenticated:
        return redirect("trading:dashboard")

    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Account created successfully.")
        return redirect("accounts:profile")

    return render(request, "accounts/register.html", {"form": form})


@login_required
def profile_view(request):
    profile = request.user.trading_profile

    # Pre-populate missing credentials from environment defaults once.
    defaults = {
        "smart_api_key": profile.smart_api_key or settings.SMART_API_DEFAULTS.get("api_key", ""),
        "smart_client_code": profile.smart_client_code or settings.SMART_API_DEFAULTS.get("client_code", ""),
        "smart_pin": profile.smart_pin or settings.SMART_API_DEFAULTS.get("pin", ""),
        "smart_totp_secret": profile.smart_totp_secret or settings.SMART_API_DEFAULTS.get("totp_secret", ""),
    }

    form = TradingProfileForm(request.POST or None, request.FILES or None, instance=profile, initial=defaults)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Trading profile updated.")
        return redirect("accounts:profile")

    smart_totp = _safe_totp(profile.smart_totp_secret)
    app_totp = _safe_totp(profile.app_totp_secret)

    context = {
        "form": form,
        "smart_totp": smart_totp,
        "app_totp": app_totp,
        "seconds_remaining": 30 - (datetime.now().second % 30),
    }
    return render(request, "accounts/profile.html", context)


@login_required
@require_GET
def totp_live_view(request):
    profile = request.user.trading_profile

    payload = {
        "seconds_remaining": 30 - (datetime.now().second % 30),
        "smart_totp": _safe_totp(profile.smart_totp_secret),
        "app_totp": _safe_totp(profile.app_totp_secret),
    }
    return JsonResponse(payload)


@login_required
@require_GET
def smartapi_connection_test(request):
    client = SmartAPIClient.from_user_profile(request.user.trading_profile)
    success, message = client.validate_login()
    return JsonResponse({"success": success, "message": message})
