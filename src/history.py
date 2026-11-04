"""Reads earlier dictations back out of the transcription history log.

The log is append-only text, one entry per line, written by
WhisperTypeApp.write_to_history:

    [2026-11-04 15:10:39] CONFIRMED: So, we are planning stuff for ...

The capture box lets the user step back through these with Left and Right
while a capture keeps recording, and paste one of them instead of what is
being said now. Read on demand -- the first arrow press -- rather than at every
capture start, so opening the box never waits on the disk.
"""

import os
import re
from dataclasses import dataclass

# The newest this many are offered. Stepping back further than this one
# keypress at a time is not a thing anyone does; the full log is a tray click
# away for that.
MAX_ENTRIES = 300
# Only the tail of the file is read -- it grows without bound, and the newest
# entries are all that is ever offered.
TAIL_BYTES = 512 * 1024

_ENTRY = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ([A-Z_]+): ?(.*)$")


@dataclass(frozen=True)
class Entry:
    timestamp: str  # "YYYY-MM-DD HH:MM:SS", local time, as written
    status: str     # CONFIRMED, CANCELLED, NOT_PASTED, ...
    text: str


def parse(lines) -> list[Entry]:
    """Entries in file order (oldest first), empty ones dropped.

    A line that does not start an entry continues the one before it, so a
    transcript that ever carried a newline comes back whole.
    """
    entries: list[list[str]] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        match = _ENTRY.match(line)
        if match:
            entries.append([match.group(1), match.group(2), match.group(3)])
        elif entries:
            entries[-1][2] += "\n" + line
    return [Entry(ts, status, text.strip()) for ts, status, text in entries
            if text.strip()]


def recent(path: str, limit: int = MAX_ENTRIES) -> list[Entry]:
    """The newest `limit` entries, newest first, repeats folded together.

    A dictation re-pasted from history is logged again, and offering the same
    text twice in a row would only be a wasted keypress.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # Most likely landed mid-entry.
            data = f.read()
    except OSError:
        return []
    entries = parse(data.decode("utf-8", errors="replace").splitlines())
    newest_first: list[Entry] = []
    for entry in reversed(entries):
        if newest_first and newest_first[-1].text == entry.text:
            continue
        newest_first.append(entry)
        if len(newest_first) >= limit:
            break
    return newest_first
