"""Logging setup: structured-ish lines to stdout AND a rotating file.

Guards against double-initialization so uvicorn (which configures its own
root logger) and the bot process can both call it safely.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import settings

_configured = False

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(component: str = "finance") -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream)

    if settings.log_dir:
        os.makedirs(settings.log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(settings.log_dir, f"{component}.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(file_handler)

    # uvicorn's access log is noisy; keep it but level it down
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
