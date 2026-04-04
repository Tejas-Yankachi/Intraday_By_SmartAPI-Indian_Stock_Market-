import binascii
import os
from io import BytesIO

import pyotp
from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image, ImageOps

from .models import UserTradingProfile

PASSPORT_PHOTO_SIZE = (210, 270)


def _normalize_totp_secret(secret: str) -> str:
    # Some users copy secrets with spaces/hyphens for readability.
    return (secret or "").strip().replace(" ", "").replace("-", "")


def _validate_totp_secret(secret: str, label: str) -> str:
    normalized = _normalize_totp_secret(secret)
    if not normalized:
        return ""

    try:
        pyotp.TOTP(normalized).now()
    except (binascii.Error, TypeError, ValueError):
        raise ValidationError(f"{label} must be a valid Base32 TOTP secret.")

    return normalized


class RegistrationForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")


class TOTPAuthenticationForm(AuthenticationForm):
    otp_token = forms.CharField(
        required=False,
        max_length=6,
        label="Authenticator OTP",
        widget=forms.TextInput(attrs={"placeholder": "123456"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        user = self.get_user()

        if not user:
            return cleaned_data

        profile = getattr(user, "trading_profile", None)
        if not profile or not profile.enable_app_totp_login:
            return cleaned_data

        otp_token = cleaned_data.get("otp_token", "").strip()
        if not otp_token:
            raise ValidationError("OTP is required for your account.")

        if not profile.app_totp_secret:
            raise ValidationError("OTP is enabled but no TOTP secret is configured.")

        try:
            is_valid = pyotp.TOTP(profile.app_totp_secret).verify(otp_token, valid_window=1)
        except (binascii.Error, TypeError, ValueError):
            raise ValidationError("Stored app OTP secret is invalid. Update it in profile.")

        if not is_valid:
            raise ValidationError("Invalid OTP. Please retry with your current authenticator code.")

        return cleaned_data


class TradingProfileForm(forms.ModelForm):
    class Meta:
        model = UserTradingProfile
        fields = [
            "profile_photo",
            "smart_api_key",
            "smart_client_code",
            "smart_pin",
            "smart_totp_secret",
            "capital",
            "risk_per_trade_percent",
            "min_risk_amount",
            "brokerage_per_trade",
            "entry_buffer_percent",
            "trade_start_time",
            "trade_end_time",
            "exit_time",
            "is_live_trading_enabled",
            "enable_app_totp_login",
            "app_totp_secret",
        ]
        widgets = {
            "profile_photo": forms.ClearableFileInput(attrs={"accept": "image/*"}),
            "smart_pin": forms.PasswordInput(render_value=True),
            "smart_totp_secret": forms.PasswordInput(render_value=True),
            "app_totp_secret": forms.PasswordInput(render_value=True),
            "trade_start_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "trade_end_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "exit_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "entry_buffer_percent": forms.NumberInput(attrs={"step": "0.001", "min": "0", "max": "2"}),
        }

    def clean_smart_totp_secret(self):
        return _validate_totp_secret(self.cleaned_data.get("smart_totp_secret", ""), "Smart API TOTP secret")

    def clean_app_totp_secret(self):
        return _validate_totp_secret(self.cleaned_data.get("app_totp_secret", ""), "App TOTP secret")

    def clean_profile_photo(self):
        photo = self.cleaned_data.get("profile_photo")
        if not photo:
            return photo

        try:
            with Image.open(photo) as img:
                img = ImageOps.exif_transpose(img)
                resized = ImageOps.fit(
                    img,
                    PASSPORT_PHOTO_SIZE,
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )

                if resized.mode != "RGB":
                    resized = resized.convert("RGB")

                output = BytesIO()
                resized.save(output, format="JPEG", quality=90, optimize=True)
                output.seek(0)
        except OSError as exc:
            raise ValidationError("Upload a valid image file.") from exc

        base_name = os.path.splitext(photo.name)[0] or "profile_photo"
        return InMemoryUploadedFile(
            file=output,
            field_name="profile_photo",
            name=f"{base_name}.jpg",
            content_type="image/jpeg",
            size=output.getbuffer().nbytes,
            charset=None,
        )

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("enable_app_totp_login")
            and not cleaned.get("app_totp_secret")
            and "app_totp_secret" not in self.errors
        ):
            raise ValidationError("App TOTP secret is required when OTP login is enabled.")
        return cleaned
