import pyotp
import tempfile
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image


class TradingProfileTOTPTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="tejas", password="test-pass-123")
        self.profile = self.user.trading_profile
        self.client.force_login(self.user)

    def _profile_payload(self, **overrides):
        payload = {
            "smart_api_key": "demo-api-key",
            "smart_client_code": "AACF355372",
            "smart_pin": "1484",
            "smart_totp_secret": "",
            "capital": "10000.00",
            "risk_per_trade_percent": "2.00",
            "min_risk_amount": "5000.00",
            "brokerage_per_trade": "40.00",
            "entry_buffer_percent": "0.050",
            "trade_start_time": "10:00",
            "trade_end_time": "12:30",
            "exit_time": "14:45",
            "is_live_trading_enabled": "on",
            "app_totp_secret": "",
        }
        payload.update(overrides)
        return payload

    def test_profile_post_rejects_invalid_smart_totp_secret(self):
        response = self.client.post(
            reverse("accounts:profile"),
            data=self._profile_payload(smart_totp_secret="735388"),
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("smart_totp_secret", form.errors)
        self.assertIn("Base32", form.errors["smart_totp_secret"][0])

    def test_profile_post_normalizes_valid_smart_totp_secret(self):
        raw_secret = pyotp.random_base32()
        formatted_secret = f"{raw_secret[:4]}-{raw_secret[4:8]} {raw_secret[8:]}"
        response = self.client.post(
            reverse("accounts:profile"),
            data=self._profile_payload(smart_totp_secret=formatted_secret),
        )

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.smart_totp_secret, raw_secret)

    def test_profile_page_handles_invalid_saved_totp_secrets(self):
        self.profile.smart_totp_secret = "735388"
        self.profile.app_totp_secret = "bad-secret!"
        self.profile.save(update_fields=["smart_totp_secret", "app_totp_secret"])

        response = self.client.get(reverse("accounts:profile"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["smart_totp"])
        self.assertIsNone(response.context["app_totp"])

    def test_totp_live_endpoint_handles_invalid_saved_totp_secrets(self):
        self.profile.smart_totp_secret = "735388"
        self.profile.app_totp_secret = "bad-secret!"
        self.profile.save(update_fields=["smart_totp_secret", "app_totp_secret"])

        response = self.client.get(reverse("accounts:totp_live"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["smart_totp"])
        self.assertIsNone(payload["app_totp"])

    def test_profile_post_saves_profile_photo(self):
        image_file = SimpleUploadedFile(
            "avatar.gif",
            (
                b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
                b"\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
                b"\x00\x02\x02L\x01\x00;"
            ),
            content_type="image/gif",
        )

        with tempfile.TemporaryDirectory() as media_root:
            with self.settings(MEDIA_ROOT=media_root):
                response = self.client.post(
                    reverse("accounts:profile"),
                    data=self._profile_payload(profile_photo=image_file),
                )

                self.assertEqual(response.status_code, 302)
                self.profile.refresh_from_db()
                self.assertTrue(self.profile.profile_photo.name.startswith("profile_photos/avatar"))
                self.assertTrue(self.profile.profile_photo.storage.exists(self.profile.profile_photo.name))
                self.assertTrue(self.profile.profile_photo.name.endswith(".jpg"))
                with self.profile.profile_photo.open("rb") as file_obj:
                    with Image.open(file_obj) as saved_img:
                        self.assertEqual(saved_img.size, (210, 270))


class LoginRedirectTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="tejas", password="test-pass-123")

    def _login_payload(self):
        return {
            "username": "tejas",
            "password": "test-pass-123",
            "otp_token": "",
        }

    def test_login_ignores_post_only_next_url(self):
        login_url = reverse("accounts:login")
        start_url = reverse("trading:start_session")
        response = self.client.post(f"{login_url}?next={start_url}", data=self._login_payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("trading:dashboard"))

    def test_login_respects_safe_get_next_url(self):
        login_url = reverse("accounts:login")
        profile_url = reverse("accounts:profile")
        response = self.client.post(f"{login_url}?next={profile_url}", data=self._login_payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, profile_url)
