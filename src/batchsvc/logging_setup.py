"""Structured logging.

JSON lines to stdout by default (log_json in config) -- one object per
log record, easy to ship to any log aggregator without a special
parser. Every field passed via `extra={...}` on a log call is merged
into the record (see main.py's request-logging middleware for the main
user of that).
"""

from __future__ import annotations

import json
import logging
import sys
import time

_STANDARD_RECORD_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, json_output: bool = True, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    if json_output:
        stream_handler.setFormatter(JsonFormatter())
    else:
        stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream_handler)
