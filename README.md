# Intraday Smart ORB (Django + SmartAPI)

Production-style Django project for a **5-minute Opening Range Breakout** strategy with:

- User registration and login
- Optional app-level TOTP MFA login
- SmartAPI credential profile management
- Real-time TOTP monitor and SmartAPI login test
- Live dashboard with session controls (start/stop/emergency exit)
- Strategy logs and open positions
- **Monthly deep traceback** analytics (daily + symbol win-rate breakdown)

## 1) Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your real secrets:

```bash
copy .env.example .env
```

## 2) Database

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
```

## 3) Run

```bash
python manage.py runserver
```

Open:

- `http://127.0.0.1:8000/accounts/register/`
- `http://127.0.0.1:8000/accounts/login/`
- `http://127.0.0.1:8000/dashboard/`

## 4) Monthly Win-Rate Traceback API

```text
GET /dashboard/api/monthly-traceback/
GET /dashboard/api/monthly-traceback/?year=2026&month=3
```

Returns:

- Month-level totals (trades, wins, losses, win rate, net P&L)
- Max win/loss streak
- Daily breakdown
- Symbol-wise breakdown

## 5) Volatility / Stop-Loss Miss Safety

If stop-loss execution is delayed during volatility, use the **Emergency Exit** button from dashboard.
It force-closes all open trades using latest fetched market price and journals the event.

## 6) Important Security Notes

- Do not commit real SmartAPI keys, PIN, or TOTP secrets into source control.
- Rotate keys if they were shared in chat/messages.
- Consider field-level encryption for credentials before production deployment.
