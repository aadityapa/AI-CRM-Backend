import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    IST_TZ = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST_TZ = timezone(timedelta(hours=5, minutes=30))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc.astimezone(IST_TZ)
        payload = {
            "ts": now_utc.isoformat(),
            "ist_date": now_ist.strftime("%Y-%m-%d"),
            "ist_time": now_ist.strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "event",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "client",
            "candidate_name",
            "interview_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=True)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
