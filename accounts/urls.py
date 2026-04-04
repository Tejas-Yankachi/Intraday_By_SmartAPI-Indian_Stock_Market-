from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import (
    UserLoginView,
    profile_view,
    register_view,
    smartapi_connection_test,
    totp_live_view,
)

app_name = "accounts"

urlpatterns = [
    path("login/", UserLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("register/", register_view, name="register"),
    path("profile/", profile_view, name="profile"),
    path("api/totp/", totp_live_view, name="totp_live"),
    path("api/smartapi-test/", smartapi_connection_test, name="smartapi_test"),
]
