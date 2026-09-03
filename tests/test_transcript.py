r"""Regression tests for the transcript folder, built from real server traffic.

Every fixture below is a trimmed recording of what WhisperLive actually sent
during a capture, taken from `%APPDATA%\WhisperType\payload_capture.jsonl`.
The spans are the real ones, because the whole of `_rescue_abandoned` is a
judgement about spans and inventing tidy ones would test nothing.

Run with:  python -m unittest discover tests
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from transcript import Transcript  # noqa: E402


def message(*segments):
    """One server message. Each segment is (start, end, text, completed)."""
    return {"segments": [
        {"start": f"{start:.3f}", "end": f"{end:.3f}",
         "text": text, "completed": completed}
        for start, end, text, completed in segments]}


def fold(*messages):
    transcript = Transcript()
    for msg in messages:
        transcript.update(msg)
    return transcript.text()


class AbandonedPendingSegment(unittest.TestCase):
    """The bug: `clip_audio` discards audio under an uncommitted segment."""

    # Capture of 2026-09-02 20:52, the one that pasted as "...longer piece of
    # text. 247, 840K plus dog." with twenty seconds missing from the middle.
    OPENING = (1.084, 7.904, " Um, okay. So we're going to try for a longer "
                             "piece of text.", True)
    LOST = (9.054, 32.614, " Um, yeah, blah, blah, blah, coffee, nothing is "
                           "committed, change RN, sake, source, blah, uh, moved "
                           "behind it, verified frost lightness went 247, 840K, "
                           "plus, um,", False)
    AFTER_CLIP = (28.242, 32.492, " 247, 840K plus", False)

    def test_text_survives_the_clip(self):
        text = fold(message(self.OPENING, self.LOST),
                    message(self.OPENING, self.AFTER_CLIP))
        self.assertIn("nothing is committed", text)
        self.assertIn("verified frost lightness", text)

    def test_rescued_text_keeps_its_place(self):
        text = fold(message(self.OPENING, self.LOST),
                    message(self.OPENING, self.AFTER_CLIP))
        self.assertLess(text.index("longer piece of text"),
                        text.index("nothing is committed"))

    def test_nothing_is_rescued_before_the_clip(self):
        """While the segment is merely growing it stays out of the transcript,
        so the box does not show the same words twice as they are revised."""
        growing = (9.054, 20.254, " Um, yeah, blah, blah, coffee, nothing is "
                                  "committed,", False)
        text = fold(message(self.OPENING, growing),
                    message(self.OPENING, self.LOST))
        self.assertEqual(text.count("nothing is committed"), 1)


class NormalTrafficIsUnchanged(unittest.TestCase):
    """Every other way a pending segment can go away must not duplicate it."""

    def test_commit_is_not_duplicated(self):
        """The committed span rarely matches the pending one exactly -- the
        end moves by a hundredth of a second -- which must not read as a
        segment the server abandoned."""
        pending = (1.084, 7.884, " Um, okay, so we're going to try for a "
                                 "longer piece of text.", False)
        committed = (1.084, 7.904, " Um, okay. So we're going to try for a "
                                   "longer piece of text.", True)
        following = (9.194, 11.434, " Uh, yeah, that worked.", False)
        text = fold(message(pending), message(committed, following))
        self.assertEqual(text.count("longer piece of text"), 1)

    def test_short_recut_is_not_rescued(self):
        """A boundary that moves a second or two is the server re-cutting the
        same speech, not dropping any: the words arrive again under the new
        span."""
        committed = (28.242, 35.402, " 247, 840K plus dog.", True)
        before = (35.402, 38.848, " Okay, that's weird.", False)
        after = (37.322, 39.322, " Okay, that's weird.", False)
        text = fold(message(committed, before), message(committed, after))
        self.assertEqual(text.count("that's weird"), 1)

    def test_overlapping_reread_is_not_rescued(self):
        """When the next segment starts back inside the old one, the server
        still holds that audio and is about to transcribe it again."""
        before = (22.09, 43.8, " Weird. If I leave a long gap, it seems okay.",
                  False)
        after = (30.0, 52.96, " Weird. If I leave a long gap, it seems okay. "
                              "I don't know.", False)
        text = fold(message(before), message(after))
        self.assertEqual(text.count("If I leave a long gap"), 1)

    def test_reconnect_restarting_timestamps_is_not_rescued(self):
        """A dropped socket restarts the server's clock at zero. Spans then
        run backwards, which is not a forward skip and must not be read as
        one -- the rescue would place the old text against a span the new
        session is about to reuse.

        The in-flight segment is lost when that happens, as it was before the
        rescue existed. That is a separate hole (nothing in the message says
        the session restarted; it has to be inferred from the socket), so the
        assertion here is only that the text is not duplicated.
        """
        first = (49.28, 74.1, " and then the connection went away.", False)
        restarted = (0.0, 2.0, " Back again.", False)
        text = fold(message(first), message(restarted))
        self.assertLessEqual(text.count("connection went away"), 1)


class RecordedSessions(unittest.TestCase):
    """Replay whole captures out of the payload log, when one is present.

    This is the test that caught the bug in the first place; the fixtures
    above are what it was distilled into. It is skipped on a machine without
    a recording rather than shipping seven megabytes of the author's dictation.
    """

    LOG = os.path.join(os.getenv("APPDATA", ""), "WhisperType",
                       "payload_capture.jsonl")

    # (words the capture lost, a phrase from the part it never lost)
    KNOWN_LOSSES = [
        ("depends on how it feels with long pauses", "10 items long"),
        ("verified frost lightness", "It got cut off randomly"),
        ("just going to keep talking", "It cut off some text"),
    ]

    def captures(self):
        captures, current = [], None
        with open(self.LOG, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("kind") == "event":
                    if record.get("name") == "capture_start":
                        current = []
                        captures.append(current)
                elif record.get("kind") == "recv" and current is not None:
                    try:
                        payload = json.loads(record["raw"])
                    except (ValueError, KeyError):
                        continue
                    if isinstance(payload, dict):
                        current.append(payload)
        return captures

    def test_known_losses_are_recovered(self):
        if not os.path.exists(self.LOG):
            self.skipTest("no payload recording on this machine")
        texts = []
        for messages in self.captures():
            transcript = Transcript()
            for msg in messages:
                transcript.update(msg)
            texts.append(transcript.text())
        for lost, anchor in self.KNOWN_LOSSES:
            capture = [t for t in texts if anchor in t]
            self.assertTrue(capture, f"no capture anchored on {anchor!r}")
            self.assertIn(lost, capture[0])


if __name__ == "__main__":
    unittest.main()
