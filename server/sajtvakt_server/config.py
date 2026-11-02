"""Inställningar från miljövariabler (se .env.example)."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = Path(_env("SAJTVAKT_DATA", "./data")).resolve()
DB_PATH = DATA_DIR / "sajtvakt.db"
STORE_DIR = DATA_DIR / "store"
TMP_DIR = DATA_DIR / "tmp"

TZ = ZoneInfo(_env("SAJTVAKT_TZ", "Europe/Stockholm"))
PUBLIC_URL = _env("SAJTVAKT_PUBLIC_URL", "https://sajtvakt.se").rstrip("/")

# Standardvärden – kan ändras i panelen under Inställningar.
DEFAULTS = {
    "backup_hour": _env("BACKUP_HOUR", "2"),
    "retention_days": _env("RETENTION_DAYS", "30"),
    "alert_email": _env("ALERT_EMAIL", ""),
    "auto_update": "1",
    "security_scan": "weekly",  # off | weekly | daily – Wordfence-skanning i nattkörningen
}

SMTP = {
    "host": _env("SMTP_HOST"),
    "port": int(_env("SMTP_PORT", "587") or 587),
    "user": _env("SMTP_USER"),
    "password": _env("SMTP_PASSWORD"),
    "sender": _env("SMTP_FROM") or _env("SMTP_USER"),
    "tls": _env("SMTP_TLS", "starttls"),  # starttls | ssl | none
}

ADMIN_PASSWORD = _env("SAJTVAKT_ADMIN_PASSWORD")
SECURE_COOKIES = _env("SAJTVAKT_SECURE_COOKIES", "1") == "1"

MONITOR_INTERVAL = int(_env("MONITOR_INTERVAL", "300") or 300)   # sekunder mellan upptidskoller
MAX_PARALLEL_JOBS = int(_env("MAX_PARALLEL_JOBS", "3") or 3)
SCHEDULER_ENABLED = _env("SAJTVAKT_SCHEDULER", "1") == "1"

for d in (DATA_DIR, STORE_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
