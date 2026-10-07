"""Tests for the speech gate's threshold, which follows the mic's noise floor.

The levels are the ones measured on 2026-10-07 from an Audient iD14 that the
old fixed -38 dBFS gate shut out entirely.

Run with:  python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from audio_capture import (GATE_MAX_RMS, GATE_MIN_RMS, dbfs_from_rms,  # noqa: E402
                           gate_threshold)


def rms_at(dbfs: float) -> float:
    return 10 ** (dbfs / 20)


class GateThreshold(unittest.TestCase):

    def test_a_quiet_mic_lets_its_speech_through(self):
        # Room noise at -77..-72, speech peaking at -54 and -41.
        room = [rms_at(-77)] * 60 + [rms_at(-72)] * 40
        speech = [rms_at(-60)] * 20 + [rms_at(-54)] * 10 + [rms_at(-41)] * 5
        gate = gate_threshold(room + speech)
        self.assertLess(gate, rms_at(-54))
        self.assertGreater(gate, rms_at(-72), "room noise must stay shut out")

    def test_a_loud_mic_keeps_the_old_gate(self):
        room = [rms_at(-40)] * 100
        self.assertEqual(gate_threshold(room), GATE_MAX_RMS)

    def test_digital_silence_does_not_open_it_on_dither(self):
        self.assertEqual(gate_threshold([rms_at(-100)] * 100), GATE_MIN_RMS)

    def test_continuous_speech_still_finds_the_floor_between_words(self):
        # Mostly speech; the gaps between words are the quiet tenth.
        levels = [rms_at(-45)] * 85 + [rms_at(-75)] * 15
        self.assertLess(dbfs_from_rms(gate_threshold(levels)), -55)

    def test_nothing_heard_yet_uses_the_strict_gate(self):
        self.assertEqual(gate_threshold([]), GATE_MAX_RMS)


if __name__ == "__main__":
    unittest.main()
