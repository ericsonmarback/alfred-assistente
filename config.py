"""Configuração lida do ambiente. Segredos ficam só no .env."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Defina {name} no arquivo .env antes de iniciar o Alfred.")
    return value


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} precisa ser um número inteiro.") from exc


TELEGRAM_TOKEN = _required("TELEGRAM_TOKEN")
GEMINI_API_KEY = _required("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"

# Se vazio, o primeiro chat privado que falar com o bot vira o dono.
TELEGRAM_CHAT_ID = _optional_int("TELEGRAM_CHAT_ID")

GOOGLE_CREDENTIALS_FILE = ROOT / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = ROOT / os.getenv("GOOGLE_TOKEN_FILE", "token.json")
STATE_FILE = ROOT / "state.json"
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary").strip() or "primary"

TIMEZONE = ZoneInfo("America/Sao_Paulo")
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "30"))
VISIT_DURATION_MINUTES = int(os.getenv("VISIT_DURATION_MINUTES", "60"))
LECTURE_DURATION_MINUTES = int(os.getenv("LECTURE_DURATION_MINUTES", "120"))

OFFER_PRICE = "R$ 880 (2x R$ 440)"
CLT_CLAUSE = "Cláusula do Art. 462 da CLT para materiais de continuidade ativa"
