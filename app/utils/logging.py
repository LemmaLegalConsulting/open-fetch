import logging
import os
import sys

# Suppress noisy pkg_resources UserWarning from third-party packages during tests
import warnings

warnings.filterwarnings("ignore", category=UserWarning, message=".*pkg_resources.*")


def _log_level_from_env(name: str, default: str) -> int:
    value = os.getenv(name, default).strip().upper()
    return getattr(logging, value, getattr(logging, default.upper(), logging.INFO))


# Stream handler feeds platform logs. In production, INFO is needed to diagnose
# provider latency; local/test runs stay quieter by default.
default_stream_level = "INFO" if os.getenv("ENV") == "production" else "WARNING"
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(_log_level_from_env("STREAM_LOG_LEVEL", default_stream_level))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[stream_handler],
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
