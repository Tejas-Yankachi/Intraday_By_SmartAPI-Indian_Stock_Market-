# Intraday Smart PDH/PDL - 5-Minute Opening Range Breakout Strategy

[![Django Version](https://img.shields.io/badge/Django-4.2-green.svg)](https://www.djangoproject.com/)
[![Python Version](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-red.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/code%20style-black-black.svg)](https://github.com/psf/black)

## 🚀 Professional Algorithmic Trading Platform

A production-grade trading platform built with Django and SmartAPI that implements a sophisticated **5-minute Opening Range Breakout (ORB)** strategy. Features real-time trade execution, multi-factor authentication, comprehensive analytics, and robust risk management.

### 🎯 Key Features

#### 🔐 Enterprise Security
- **Multi-Factor Authentication**: TOTP-based 2FA at application level
- **Credential Vault**: Secure storage of SmartAPI credentials with encryption
- **Session Management**: Real-time session controls with emergency kill switch
- **Audit Logging**: Complete journal of all trading activities

#### 📊 Advanced Trading Engine
- **5-Minute ORB Strategy**: Backtested PDH/PDL (Previous Day High/Low) breakout
- **Real-time Monitoring**: Live position tracking and performance metrics
- **Volatility Protection**: Stop-loss miss safety with emergency exit mechanism
- **Risk Controls**: Configurable position sizing and risk parameters

#### 📈 Analytics & Reporting
- **Monthly Traceback**: Deep analytics with daily and symbol-wise breakdown
- **Performance Metrics**: Win rates, profit/loss, streak analysis
- **Visual Dashboard**: Real-time charts and trade visualization
- **Export Capabilities**: JSON/CSV export for external analysis

## 🏗️ System Architecture

┌─────────────────┐ ┌──────────────┐ ┌─────────────────┐
│ Django WSGI │────▶│ SmartAPI │────▶│ Angel Broking │
│ Application │ │ Gateway │ │ Trading Engine │
└─────────────────┘ └──────────────┘ └─────────────────┘
│ │ │
▼ ▼ ▼
┌─────────────────┐ ┌──────────────┐ ┌─────────────────┐
│ PostgreSQL │ │ Redis │ │ Celery │
│ (Primary DB) │ │ (Cache) │ │ (Tasks) │
└─────────────────┘ └──────────────┘ └─────────────────┘

## 📋 Prerequisites

- **Python 3.10+** - Core runtime
- **PostgreSQL 13+** - Production database (SQLite for development)
- **Redis 6+** - Caching and session storage
- **SmartAPI Account** - Angel Broking API access
- **TOTP Authenticator** - Google Authenticator or compatible

## 🔧 Installation & Setup

### 1. Clone Repository
```bash
git clone https://github.com/yourusername/intraday-smart-pdh-pdl.git
cd intraday-smart-pdh-pdl

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (Linux/Mac)
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
# Run migrations
python manage.py makemigrations
python manage.py migrate

# Create superuser for admin access
python manage.py createsuperuser

python manage.py runserver
# Install production dependencies
pip install gunicorn gevent

# Run with Gunicorn
gunicorn --worker-class gevent --workers 4 --bind 0.0.0.0:8000 core.wsgi:application

# Build image
docker build -t intraday-trader .

# Run container
docker run -d -p 8000:8000 --env-file .env intraday-trader

{
  "month": "2026-03",
  "summary": {
    "total_trades": 45,
    "winning_trades": 28,
    "losing_trades": 17,
    "win_rate": 62.22,
    "net_pl": 12500.50,
    "gross_profit": 25000.00,
    "gross_loss": -12500.00,
    "profit_factor": 2.0,
    "max_win_streak": 5,
    "max_loss_streak": 3,
    "avg_win": 892.86,
    "avg_loss": -735.29,
    "largest_win": 3500.00,
    "largest_loss": -1200.00
  },
  "daily_breakdown": [
    {
      "date": "2026-03-01",
      "trades": 3,
      "wins": 2,
      "losses": 1,
      "win_rate": 66.67,
      "pnl": 1250.00,
      "avg_rr": 1.5
    }
  ],
  "symbol_breakdown": [
    {
      "symbol": "NIFTY",
      "trades": 15,
      "wins": 10,
      "losses": 5,
      "win_rate": 66.67,
      "pnl": 7500.00,
      "avg_holding_minutes": 45
    }
  ]
}
