from django.urls import path

from .views import (
    add_watchlist_symbol_view,
    close_trade_view,
    dashboard_data_view,
    dashboard_view,
    emergency_exit_view,
    monthly_traceback_view,
    watchlist_chart_view,
    start_session_view,
    stop_session_view,
    toggle_watchlist_symbol_view,
    remove_watchlist_symbol_view,
)

app_name = "trading"

urlpatterns = [
    path("", dashboard_view, name="dashboard"),
    path("start/", start_session_view, name="start_session"),
    path("stop/", stop_session_view, name="stop_session"),
    path("emergency-exit/", emergency_exit_view, name="emergency_exit"),
    path("watchlist/<int:symbol_id>/chart/", watchlist_chart_view, name="watchlist_chart"),
    path("watchlist/add/", add_watchlist_symbol_view, name="watchlist_add"),
    path("watchlist/<int:symbol_id>/toggle/", toggle_watchlist_symbol_view, name="watchlist_toggle"),
    path("watchlist/<int:symbol_id>/remove/", remove_watchlist_symbol_view, name="watchlist_remove"),
    path("trade/<int:trade_id>/close/", close_trade_view, name="close_trade"),
    path("api/dashboard/", dashboard_data_view, name="dashboard_api"),
    path("api/monthly-traceback/", monthly_traceback_view, name="monthly_traceback_api"),
]
