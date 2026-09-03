r"""Verbatim capture of WhisperLive's messages, for designing inline insertion.

Inline dictation types a segment into the target document the moment the server
calls it final, and never touches it again. The whole design therefore rests on
two things this client does not currently read: which field marks a segment
final, and how much a segment's text still moves before it gets there. Both
vary between WhisperLive versions, so they are read off real sessions here
rather than guessed at from the docs.

Records to `%APPDATA%\WhisperType\payload_capture.jsonl`, one JSON object per
line, timestamped from process start so a message can be placed against the
capture it belongs to. On by default while the insertion model is being
settled; set WHISPERTYPE_PAYLOAD_LOG=0 to silence it. Delete this module once
inline insertion is built.
"""

import json
import logging
import os
import threading
import time

from settings_window import APP_DATA_DIR

PAYLOAD_FILE = os.path.join(APP_DATA_DIR, "payload_capture.jsonl")

_logger = logging.getLogger("whispertype.payloads")
_start = time.monotonic()
# Messages arrive on the WebSocket thread while capture events are emitted from
# the GUI thread; without the lock their lines interleave mid-write.
_lock = threading.Lock()
_enabled = os.environ.get("WHISPERTYPE_PAYLOAD_LOG", "1") not in ("0", "", "false", "False")
_warned = False


def is_enabled() -> bool:
    return _enabled


def _write(record: dict):
    global _warned
    if not _enabled:
        return
    record["t"] = round(time.monotonic() - _start, 3)
    try:
        line = json.dumps(record, ensure_ascii=False)
    except (TypeError, ValueError):
        # A payload that will not serialise is itself worth knowing about, but
        # it must not take the capture down with it.
        line = json.dumps({"t": record["t"], "kind": "unserializable"})
    try:
        with _lock:
            with open(PAYLOAD_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        # Diagnostics must never break dictation. Complain once, then go quiet.
        if not _warned:
            _warned = True
            _logger.exception("Payload capture disabled: could not write %s", PAYLOAD_FILE)


def log_raw(message: str):
    """Record one server message exactly as it arrived, before any parsing."""
    _write({"kind": "recv", "raw": message})


def log_event(name: str, **fields):
    """Record a client-side marker so messages can be grouped into captures."""
    _write({"kind": "event", "name": name, **fields})
