import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Tests are deterministic and offline; individual tests explicitly enable mocked clients.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OFFLINE_MODE"] = "1"
os.environ["TELEMETRY_ENABLED"] = "false"
for name in list(os.environ):
    if name.startswith(("API_", "TELEMETRY_", "OTEL_", "DASHBOARD_", "FETCH_RATE_")):
        os.environ.pop(name, None)
os.environ["TELEMETRY_ENABLED"] = "false"
os.environ.pop("FETCH_ENABLED_PROVIDERS", None)
os.environ.pop("FETCH_MODEL_ENDPOINTS", None)
os.environ.pop("FETCH_AUXILIARY_MODEL", None)
os.environ.pop("FETCH_MERGE_MODEL", None)

# Recreate bounded state per test; rate-limit tests choose their own budgets.
import pytest


@pytest.fixture(autouse=True)
def isolated_rate_limits():
    from app.main import app
    from app.core.rate_limit import RateLimiter

    # Own patch context avoids changing ordering of existing auth fixtures.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FETCH_RATE_LIMIT_BACKEND", "memory")
        for name in list(os.environ):
            if name.startswith("FETCH_RATE_") and name != "FETCH_RATE_LIMIT_BACKEND":
                patch.delenv(name)
        app.state.rate_limiter = RateLimiter()
        yield
