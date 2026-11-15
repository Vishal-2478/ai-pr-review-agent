"""Structured JSON logs with secrets masked before they leave the process."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from backend.observability.context import current_review_id
from backend.security.secret_masking import mask


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask(record.getMessage()),
        }
        review_id = current_review_id.get()
        if review_id:
            entry["review_id"] = review_id
        if record.exc_info:
            entry["exc"] = mask(self.formatException(record.exc_info))[-2000:]
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "arq.worker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
