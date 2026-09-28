import logging
import json
import os
from datetime import datetime, timezone
from pathlib import Path


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if hasattr(record, "data"):
            payload.update(record.data)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str = "az") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = _JsonFormatter()

    # Stdout handler - always on (Docker captures it)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # File handler - writes to /app/logs/app.log inside the container.
    # ROTATING since 2026-09-28: a plain handler grows without bound, and the
    # size refusals added that day are lines a caller with no session can
    # write as fast as they can send. The footprint is LOG_MAX_MB x
    # (LOG_BACKUPS + 1), 10 MB x 6 by default.
    log_dir = Path(os.getenv("LOG_DIR", "/app/logs"))
    try:
        from logging.handlers import RotatingFileHandler
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "app.log", encoding="utf-8",
            maxBytes=int(float(os.getenv("LOG_MAX_MB", "10")) * 1024 * 1024),
            backupCount=int(os.getenv("LOG_BACKUPS", "5")))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        # Running outside Docker - file logging skipped
        pass

    logger.propagate = False
    return logger


def log(event: str, **kwargs):
    """Convenience wrapper: log.info with structured extra fields."""
    record = logging.LogRecord(
        name="az", level=logging.INFO, pathname="", lineno=0,
        msg=event, args=(), exc_info=None
    )
    record.data = kwargs
    get_logger().handle(record)


def log_error(event: str, **kwargs):
    record = logging.LogRecord(
        name="az", level=logging.ERROR, pathname="", lineno=0,
        msg=event, args=(), exc_info=None
    )
    record.data = kwargs
    get_logger().handle(record)
