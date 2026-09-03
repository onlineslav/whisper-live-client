"""The delta arithmetic behind the live preview.

Only the pure part is covered here -- what to remove and what to add for a
given revision. The injection itself needs a real window with a real caret in
it and is exercised by hand; see live_type.py for what was measured and why
the mechanism is posted messages rather than synthesised keystrokes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from live_type import common_prefix_length


def edit_for(typed: str, text: str):
    """(characters to backspace, characters to insert) -- update()'s decision."""
    keep = common_prefix_length(typed, text)
    return len(typed) - keep, text[keep:]


class CommonPrefixTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(common_prefix_length("", ""), 0)
        self.assertEqual(common_prefix_length("", "abc"), 0)
        self.assertEqual(common_prefix_length("abc", ""), 0)

    def test_identical(self):
        self.assertEqual(common_prefix_length("abc", "abc"), 3)

    def test_divergence(self):
        self.assertEqual(common_prefix_length("abc", "abd"), 2)
        self.assertEqual(common_prefix_length("xyz", "abc"), 0)

    def test_one_extends_the_other(self):
        self.assertEqual(common_prefix_length("abc", "abcdef"), 3)
        self.assertEqual(common_prefix_length("abcdef", "abc"), 3)

    def test_is_symmetric(self):
        for a, b in (("", "a"), ("ab", "abc"), ("abc", "abd"), ("xy", "xy")):
            self.assertEqual(common_prefix_length(a, b),
                             common_prefix_length(b, a))


class EditTests(unittest.TestCase):
    def test_pure_append_removes_nothing(self):
        # The common case while someone keeps talking.
        self.assertEqual(edit_for("so I was", "so I was thinking"),
                         (0, " thinking"))

    def test_revision_backspaces_only_the_tail(self):
        # The whole point: the server changing its mind about the last word
        # must not retype the sentence in front of it.
        remove, add = edit_for("so I was thinking we could meet",
                               "so I was thinking we could move the meeting")
        self.assertEqual(remove, 3)          # "eet"
        self.assertEqual(add, "ove the meeting")

    def test_first_text_is_all_addition(self):
        self.assertEqual(edit_for("", "hello"), (0, "hello"))

    def test_unchanged_text_is_a_no_op(self):
        self.assertEqual(edit_for("hello", "hello"), (0, ""))

    def test_shortening_removes_the_difference(self):
        # A revision that drops a trailing word entirely.
        self.assertEqual(edit_for("hello there world", "hello there"),
                         (6, ""))

    def test_complete_rewrite(self):
        remove, add = edit_for("abc", "xyz")
        self.assertEqual(remove, 3)
        self.assertEqual(add, "xyz")

    def test_edit_reconstructs_the_target(self):
        # The invariant update() relies on: applying the edit to what is on
        # screen has to produce exactly the new text, for any pair.
        pairs = [
            ("", "one"), ("one", "one two"), ("one two", "one 2"),
            ("hello there world", "hello there"), ("abc", "xyz"),
            ("a" * 40, "a" * 20 + "b" * 20), ("same", "same"),
        ]
        for typed, text in pairs:
            remove, add = edit_for(typed, text)
            rebuilt = typed[:len(typed) - remove] + add
            self.assertEqual(rebuilt, text, f"{typed!r} -> {text!r}")


if __name__ == "__main__":
    unittest.main()
