from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from trading.services.terminal_dashboard import build_terminal_dashboard_state, render_terminal_dashboard


class Command(BaseCommand):
    help = "Show a live terminal dashboard for backend execution."

    def add_arguments(self, parser):
        parser.add_argument("--session-id", type=int, dest="session_id", help="Focus on one session id.")
        parser.add_argument("--user-id", type=int, dest="user_id", help="Focus on sessions for one user id.")
        parser.add_argument("--max-sessions", type=int, default=5, dest="max_sessions", help="Maximum sessions to show.")
        parser.add_argument("--max-trades", type=int, default=5, dest="max_trades", help="Maximum open trades per session.")
        parser.add_argument("--max-logs", type=int, default=8, dest="max_logs", help="Maximum logs per session.")
        parser.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds.")
        parser.add_argument("--once", action="store_true", help="Render one frame and exit.")

    def handle(self, *args, **options):
        interval = float(options["interval"])
        if interval <= 0:
            raise CommandError("--interval must be greater than zero.")

        session_id = options.get("session_id")
        user_id = options.get("user_id")
        max_sessions = int(options.get("max_sessions") or 5)
        max_trades = int(options.get("max_trades") or 5)
        max_logs = int(options.get("max_logs") or 8)
        once = bool(options.get("once"))
        interactive = bool(getattr(self.stdout, "isatty", lambda: False)()) and not once

        if session_id is not None and user_id is not None:
            raise CommandError("Use only one of --session-id or --user-id.")

        try:
            while True:
                state = build_terminal_dashboard_state(
                    session_id=session_id,
                    user_id=user_id,
                    max_sessions=max_sessions,
                    max_trades=max_trades,
                    max_logs=max_logs,
                )
                rendered = render_terminal_dashboard(state)

                if interactive:
                    self.stdout.write("\x1b[2J\x1b[H", ending="")

                self.stdout.write(rendered)
                self.stdout.write("")

                if state.get("error"):
                    raise CommandError(state["error"])

                if once:
                    return

                time.sleep(interval)
        except KeyboardInterrupt:
            if not once:
                self.stdout.write("\nDashboard stopped.")
            return
