from __future__ import annotations
import re
from typing import Any, Callable, Optional, Awaitable
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_exception,
    Retrying,
)
from tenacity import AsyncRetrying


def _parse_rate_limit_delay(exc: BaseException) -> Optional[float]:
    """Parse the delay from a rate limit error message, if present."""
    msg = str(exc).lower()
    match = re.search(r"try again in (\d+\.?\d*)s", msg)
    if match:
        return float(match.group(1))
    return None


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(
        code in msg
        for code in ("insufficient_quota", "context_length", "context length")
    ):
        return False
    if "rate limit" in msg or "429" in msg or "too many requests" in msg:
        return True
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status in {429, 529}:
        return True
    try:
        body = getattr(resp, "text", "") or ""
        if status == 400 and any(
            s in body.lower() for s in ["rate limit", "too many requests"]
        ):
            return True
    except Exception:
        pass
    return False


def custom_wait_strategy(retry_state, max_wait_seconds: float = 600) -> float:
    exc = retry_state.outcome.exception()
    capped_max_wait = max(1, max_wait_seconds)
    if exc:
        # First, try to get the delay from the 'Retry-After' header
        response = getattr(exc, "response", None)
        if response and hasattr(response, "headers"):
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return max(1, min(float(retry_after), capped_max_wait))
                except (ValueError, TypeError):
                    pass  # Fallback to other methods if parsing fails

        # If header is not present, try parsing from the error message body
        delay = _parse_rate_limit_delay(exc)
        if delay is not None:
            return max(1, min(delay, capped_max_wait))

    # Fallback to exponential backoff
    return wait_random_exponential(min=1, max=capped_max_wait)(retry_state)


async def run_with_backoff_async(
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    max_attempts: int = 5,
    max_wait_seconds: float = 600,
    **kwargs: Any,
) -> Any:
    async for attempt in AsyncRetrying(
        wait=lambda retry_state: custom_wait_strategy(
            retry_state,
            max_wait_seconds=max_wait_seconds,
        ),
        stop=stop_after_attempt(max(1, max_attempts)),
        retry=retry_if_exception(_is_rate_limit_error),
        reraise=True,
    ):
        with attempt:
            return await func(*args, **kwargs)


def run_with_backoff(
    func: Callable[..., Any],
    *args: Any,
    max_attempts: int = 5,
    max_wait_seconds: float = 600,
    **kwargs: Any,
) -> Any:
    for attempt in Retrying(
        wait=lambda retry_state: custom_wait_strategy(
            retry_state,
            max_wait_seconds=max_wait_seconds,
        ),
        stop=stop_after_attempt(max(1, max_attempts)),
        retry=retry_if_exception(_is_rate_limit_error),
        reraise=True,
    ):
        with attempt:
            return func(*args, **kwargs)
