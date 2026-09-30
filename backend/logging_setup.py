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
        # The traceback (29 Sep 2026): every `logger.exception(...)` /
        # `exc_info=True` in the app used to reach the log WITHOUT it.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(payload, ensure_ascii=True)


class ProactorDisconnectFilter(logging.Filter):
    """Drops Windows' "Exception in callback
    _ProactorBasePipeTransport._call_connection_lost()" — a browser closing a
    keep-alive socket (ConnectionResetError / WinError 10054) that asyncio
    logs at ERROR every few seconds. Harmless, and it buried the real errors:
    137 of them in one afternoon's log. Anything else from asyncio passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "asyncio":
            return True
        if "_call_connection_lost" not in record.getMessage():
            return True
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is None or isinstance(exc, (ConnectionResetError, ConnectionAbortedError)))


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ProactorDisconnectFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
