from types import SimpleNamespace

from app.utils.backoff import custom_wait_strategy


def test_custom_wait_strategy_caps_retry_after_header():
    exc = SimpleNamespace(
        response=SimpleNamespace(headers={"retry-after": "60"}),
    )
    retry_state = SimpleNamespace(outcome=SimpleNamespace(exception=lambda: exc))

    assert custom_wait_strategy(retry_state, max_wait_seconds=2) == 2


def test_custom_wait_strategy_caps_message_delay():
    exc = Exception("Rate limit exceeded. Try again in 45s")
    retry_state = SimpleNamespace(outcome=SimpleNamespace(exception=lambda: exc))

    assert custom_wait_strategy(retry_state, max_wait_seconds=3) == 3
