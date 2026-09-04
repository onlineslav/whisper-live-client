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

from live_type import LiveTyper, common_prefix_length


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


class KeepingThePreview(unittest.TestCase):
    """finish_if_matches: when the document may be left exactly as it is.

    The decision, not the injection. A typer is stood up with its model of
    what it has typed already set, and the two calls that touch Windows are
    replaced -- what is under test is which way it goes, because going the
    wrong way either leaves the preview in beside a paste of the same words,
    or credits the document with text nobody put there.
    """

    def typer(self, typed, ready=True, posted=True):
        typer = LiveTyper()
        typer._hwnd = 1
        typer._focus_hwnd = 1
        typer._typed = typed
        typer._active = True
        typer._unverified = False
        typer.posted = []
        typer._target_is_ready = lambda: ready
        def post(hwnd, text):
            typer.posted.append(text)
            return posted
        import live_type
        typer._post = post
        return typer

    def run_with(self, typer, text):
        import live_type
        original = live_type.win_input.post_text
        live_type.win_input.post_text = typer._post
        try:
            return typer.finish_if_matches(text)
        finally:
            live_type.win_input.post_text = original

    def test_kept_when_the_paste_only_adds_its_trailing_space(self):
        typer = self.typer("hello there")
        self.assertTrue(self.run_with(typer, "hello there "))
        self.assertEqual(typer.posted, [" "])
        # Reset, so nothing later tries to take back what was left in.
        self.assertFalse(typer.active)
        self.assertEqual(typer.typed, "")

    def test_not_kept_when_the_final_pass_rewrote_the_text(self):
        typer = self.typer("hello there")
        self.assertFalse(self.run_with(typer, "Hello, there. "))
        self.assertEqual(typer.posted, [])
        # Still armed: the caller is about to take the preview back out.
        self.assertTrue(typer.active)

    def test_not_kept_when_nothing_was_typed(self):
        typer = self.typer("")
        self.assertFalse(self.run_with(typer, "hello there "))

    def test_not_kept_when_the_target_is_out_of_reach(self):
        typer = self.typer("hello there", ready=False)
        self.assertFalse(self.run_with(typer, "hello there "))
        self.assertTrue(typer.active)

    def test_not_kept_when_the_tail_could_not_be_posted(self):
        typer = self.typer("hello there", posted=False)
        self.assertFalse(self.run_with(typer, "hello there "))
        self.assertTrue(typer.active)


if __name__ == "__main__":
    unittest.main()
