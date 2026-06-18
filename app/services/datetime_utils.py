from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def parse_form_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
    return dt.astimezone(timezone.utc)
