"""Tests for stepping aside when ayaNet's own voice has the hotkey.

Run with:  python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from win_focus import ayanet_takes_hotkey  # noqa: E402


class AyanetTakesHotkeyTests(unittest.TestCase):
    def test_f13_in_an_ayanet_island_belongs_to_ayanet(self):
        for title in ("ayaNet: dump", "ayaNet: guardian", "ayaNet: filed"):
            self.assertTrue(ayanet_takes_hotkey("<f13>", title))
        self.assertTrue(ayanet_takes_hotkey("<F13>", "ayaNet: dump"))

    def test_other_windows_keep_the_hotkey(self):
        for title in ("", "Notepad", "ayaNet", "notes about ayaNet: dump",
                      "ayanet: dump"):
            self.assertFalse(ayanet_takes_hotkey("<f13>", title))

    def test_other_hotkeys_are_never_taken(self):
        self.assertFalse(ayanet_takes_hotkey("<ctrl>+<shift>+`", "ayaNet: dump"))
        self.assertFalse(ayanet_takes_hotkey("<shift>+<f13>", "ayaNet: dump"))
        self.assertFalse(ayanet_takes_hotkey("", "ayaNet: dump"))


if __name__ == "__main__":
    unittest.main()
