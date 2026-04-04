from django.conf import settings
from django.contrib import admin
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.urls import include, path


def root_redirect(request):
    if request.user.is_authenticated:
        return redirect("trading:dashboard")
    return redirect("accounts:login")


urlpatterns = [
    path("", root_redirect, name="home"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("dashboard/", include("trading.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
