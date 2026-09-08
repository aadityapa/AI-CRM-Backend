"""
OpenAI chat completions with retries and rate-limit-safe backoff.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Sequence

from openai_client import OpenAIPurpose, get_openai_client
from prompt_logger import tracked_chat_completion

logger = logging.getLogger(__name__)


def _max_retries() -> int:
    try:
        return max(0, min(5, int(os.getenv("OPENAI_RETRY_MAX", "2"))))
    except (TypeError, ValueError):
        return 2


def _retry_delay(attempt: int) -> float:
    base = float(os.getenv("OPENAI_RETRY_BASE_DELAY_S", "0.8"))
    return min(8.0, base * (2**attempt))


def chat_completion_with_retry(
    *,
    messages: Sequence[dict[str, str]],
    model: str,
    temperature: float = 0.45,
    call_type: str = "generate_questions",
    db_target: str = "",
    purpose: OpenAIPurpose = "question",
    **log_kwargs: Any,
) -> str:
    """
    Call OpenAI chat completions; retry on transient failures.

    Returns assistant message text or raises on exhaustion.
    """
    client = get_openai_client(purpose)
    last_err: Exception | None = None
    attempts = _max_retries() + 1
    for attempt in range(attempts):
        try:
            res = tracked_chat_completion(
                client,
                model=model,
                messages=list(messages),
                temperature=temperature,
                call_type=call_type if attempt == 0 else f"{call_type}_retry",
                db_target=db_target,
                **log_kwargs,
            )
            return (res.choices[0].message.content or "").strip()
        except Exception as exc:
            last_err = exc
            msg = str(exc).lower()
            retryable = any(
                token in msg
                for token in ("rate limit", "timeout", "overloaded", "503", "502", "429", "connection")
            )
            if attempt >= attempts - 1 or not retryable:
                logger.warning(
                    "openai.chat.failed",
                    extra={"call_type": call_type, "attempt": attempt + 1, "error": str(exc)[:200]},
                )
                raise
            delay = _retry_delay(attempt)
            logger.info(
                "openai.chat.retry",
                extra={"call_type": call_type, "attempt": attempt + 1, "delay_s": delay},
            )
            time.sleep(delay)
    if last_err:
        raise last_err
    return ""
