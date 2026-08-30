"""Crash-safe JSON state persistence shared by stateful domain services."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_FILE_WRITE_LOCK = threading.RLock()


def write_json_atomically(path: str, payload: Any) -> None:
    """Write JSON and its recovery copy without exposing a partial main file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    backup_path = f"{path}.bak"
    with _FILE_WRITE_LOCK:
        fd, temporary_path = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary_path, path)
                    break
                except (PermissionError, OSError):
                    if attempt == 4:
                        with open(path, "w", encoding="utf-8") as file:
                            json.dump(payload, file, ensure_ascii=False, indent=2)
                    else:
                        time.sleep(0.02 * (2 ** attempt))
            try:
                with open(backup_path, "w", encoding="utf-8") as backup:
                    json.dump(payload, backup, ensure_ascii=False, indent=2)
            except OSError as exc:
                logger.debug("Backup write failed for %s: %s", path, exc)
        finally:
            if os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


def load_json_with_backup_recovery(path: str, default: Any = None) -> Any:
    """Load JSON, recovering from the adjacent backup when necessary."""
    backup_path = f"{path}.bak"
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as main_error:
        try:
            with open(backup_path, "r", encoding="utf-8") as backup:
                recovered = json.load(backup)
            logger.warning("State recovery from backup for %s: %s", path, main_error)
            write_json_atomically(path, recovered)
            return recovered
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default
