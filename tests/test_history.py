"""Tests for reading earlier dictations back out of the history log.

Run with:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import history  # noqa: E402

SAMPLE = """\
[2026-11-04 15:00:53] CONFIRMED: first thing
[2026-11-04 15:05:00] CANCELLED: second thing
[2026-11-04 15:10:58] CONFIRMED:
[2026-11-04 15:11:58] CONFIRMED: third thing
that ran onto a second line
[2026-11-04 15:12:00] CONFIRMED: third thing
that ran onto a second line
[2026-11-04 15:14:03] NOT_PASTED: fourth thing
"""


class HistoryTests(unittest.TestCase):
    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8",
                                             suffix=".log")
        handle.write(text)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_parse_skips_empty_and_joins_continuations(self):
        entries = history.parse(SAMPLE.splitlines())
        self.assertEqual([e.text for e in entries], [
            "first thing", "second thing",
            "third thing\nthat ran onto a second line",
            "third thing\nthat ran onto a second line",
            "fourth thing"])
        self.assertEqual(entries[1].status, "CANCELLED")
        self.assertEqual(entries[0].timestamp, "2026-11-04 15:00:53")

    def test_recent_is_newest_first_without_repeats(self):
        path = self._write(SAMPLE)
        texts = [e.text for e in history.recent(path)]
        self.assertEqual(texts, [
            "fourth thing", "third thing\nthat ran onto a second line",
            "second thing", "first thing"])

    def test_recent_respects_limit(self):
        path = self._write(SAMPLE)
        self.assertEqual(len(history.recent(path, limit=2)), 2)

    def test_missing_file_is_empty(self):
        self.assertEqual(history.recent("Z:/no/such/history.log"), [])

    def test_tail_read_drops_partial_first_line(self):
        lines = "".join(f"[2026-11-04 10:00:{i % 60:02d}] CONFIRMED: entry {i}\n"
                        for i in range(20000))
        path = self._write(lines)
        entries = history.recent(path, limit=5000)
        self.assertEqual(entries[0].text, "entry 19999")
        self.assertTrue(all(e.text.startswith("entry ") for e in entries))


if __name__ == "__main__":
    unittest.main()
