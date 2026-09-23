"""Type the transcript into the target as it is spoken, and take it back again.

The words appear in the document itself, in its own font, wrapping and
reflowing the way the document's text really does -- because it *is* the
document's text. This replaces an earlier attempt that drew a convincing
picture of the same thing over the top of the application; the picture was
never quite right, and the difference between "nearly the real thing" and the
real thing turns out to be the whole point.

The obvious way to do it is to type the whole transcript again every time the
server sends a revision. The server sends one several times a second, so that
means erasing and retyping a sentence a few times a second, which flickers
badly and is slow enough to fall behind the speaker. Instead only the tail
that actually changed is touched: the common prefix of what is on screen and
what should be there is left exactly where it is, and the difference is
backspaced and retyped. In practice a revision changes the last word or two,
so a typical update is a handful of keystrokes.

What makes this safe enough to do at all is that it only ever runs against a
target that reported a real text caret -- `source == "caret"`, which means
UI Automation or the classic caret API confirmed an insertion point in a text
control. That guard is not decoration. Backspace sent to a window whose focus
is not in a text field is the browser's Back button, and this module's whole
job is sending Backspace.

Two more things keep it honest while it runs:

  * The foreground window is rechecked before every injection. Alt-tab away
    mid-dictation and the keystrokes would otherwise land in whatever is now
    in front. When the target is not in front, nothing is sent and nothing is
    recorded as sent, so the model of what is on screen stays true and the
    next update carries on from where it left off.

  * Everything typed is remembered exactly, so it can be removed exactly.
    Cancelling a capture, or confirming one whose finished text differs from
    the preview, erases precisely what was put in and no more.

The text goes in as posted window messages rather than as synthesised
keystrokes, and that is the difference between this working and not.

The obvious way is SendInput with KEYEVENTF_UNICODE -- the same mechanism the
final paste uses -- and it failed about half the time: characters lost, and
stranger, characters repeated, so that "meeting" arrived as "ng" and a run of
thirty d's appeared where one belonged. It was not a rate limit; batches of 40
events survived where batches of 20 did not, which is a race.

The cause is the low-level keyboard hook chain. Every WH_KEYBOARD_LL hook
installed anywhere on the desktop runs synchronously for every event before
SendInput returns, and an ordinary desktop has several -- a launcher, a
window manager, a remapper, and WhisperType's own hotkey listener among them.
Measured on the development machine: 960 microseconds per event, against a few
microseconds with no hooks installed. A sentence is a few hundred events, so
it sits a quarter of a second inside that chain, past Windows'
LowLevelHooksTimeout, at which point events are dropped -- and a remapping
hook that re-injects what it sees supplies the duplicates.

A posted WM_CHAR never enters the chain at all; it goes straight onto the
target window's message queue. Measured at 0.02 milliseconds per character,
about a hundred and sixty times faster, and correct in every trial. Backspace
posts the same way for the same reason.

The catch is that posted messages are not real input, so an application that
does its text entry somewhere other than WM_CHAR ignores them without saying
so. That is genuinely dangerous here: believing text went in when it did not
would mean backspacing over the user's own writing to "remove" it. So the
first insertion of every capture is checked -- if the caret did not move,
nothing arrived, and live typing stands down for that capture having touched
nothing.

It is still, unavoidably, real editing, and it leaves a mark on the target's
undo history. Measured in Notepad: one Ctrl+Z after a confirmed dictation
restores the document to exactly its pre-dictation state, which is the case
that matters and costs one press. Pressing again does not continue past it --
it brings the intermediate revisions back, and takes several more presses to
return to where one press had already arrived. So undo is correct but not
clean, and an editor with autocorrect or autocomplete may fight the inserted
text besides. Those are the costs of the text being real.
"""

import logging
import time

import caret_target
import win_input
from win_focus import get_foreground_window, is_window

logger = logging.getLogger("whispertype.livetype")

# How long to keep looking for the caret to move before concluding that the
# first insertion was ignored, in seconds. Only ever spent when something is
# actually wrong: a target that took the text has already moved its caret by
# the time this is first checked, and the check costs nothing then.
VERIFY_TIMEOUT = 0.25
VERIFY_INTERVAL = 0.02

# A revision that would retype more than this many characters is treated as a
# rewrite rather than an edit. It changes nothing about what is sent -- the
# arithmetic is the same either way -- but it is worth noticing in the log,
# because a stream that does it repeatedly is a stream this is a poor fit for.
LARGE_REVISION = 120


def common_prefix_length(a: str, b: str) -> int:
    """How many characters `a` and `b` share from the start.

    The pivot of the whole module: everything before this point is already
    correct on screen and must not be touched, and everything after it is the
    edit. Kept as a free function so it can be tested without a Windows
    message queue anywhere in sight.
    """
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index


class LiveTyper:
    """Keeps the target's text in step with the transcript so far."""

    def __init__(self):
        self._hwnd = None
        # The control inside the window that actually handles text. Posted
        # messages go here, not to the frame around it.
        self._focus_hwnd = None
        self._typed = ""
        self._active = False
        # Where the caret was immediately before the first insertion, held
        # until that insertion has been seen to move it. None once the
        # application has proved it accepts posted text -- or once it has
        # proved it does not, and live typing has stood down.
        self._verify_from = None
        self._unverified = True

    # -- lifecycle ----------------------------------------------------------

    def can_type(self, target) -> bool:
        """True if `target` is a confirmed text caret and nothing less.

        See the module docstring for why this is the one check that matters.
        """
        return (target is not None and target.source == "caret"
                and bool(target.caret) and win_input.is_available())

    def begin(self, target, hwnd) -> bool:
        """Start typing into `hwnd`. False if this target does not qualify."""
        self.reset()
        if not self.can_type(target) or not hwnd or not is_window(hwnd):
            return False
        self._hwnd = hwnd
        self._focus_hwnd = caret_target.focused_control(hwnd)
        self._active = True
        self._verify_from = None
        self._unverified = True
        logger.debug("Live typing into 0x%X (control 0x%X).",
                     hwnd, self._focus_hwnd or 0)
        return True

    def reset(self):
        """Forget everything without touching the document."""
        self._hwnd = None
        self._focus_hwnd = None
        self._typed = ""
        self._active = False
        self._verify_from = None
        self._unverified = True

    @property
    def active(self) -> bool:
        return self._active

    @property
    def typed(self) -> str:
        """Exactly what this has put into the document and not taken back."""
        return self._typed

    # -- the edit itself ----------------------------------------------------

    def update(self, text: str) -> bool:
        """Make the document say `text`. False if nothing could be sent.

        Safe to call with the same string repeatedly; an update that changes
        nothing sends nothing.
        """
        if not self._active:
            return False
        # Settle any outstanding check first. Deferring it to here rather than
        # doing it inline at the previous insertion is what makes it free:
        # PostMessage returns before the target has processed anything, so an
        # immediate look at the caret always shows it unmoved. By the time the
        # next revision arrives -- a fraction of a second later -- the answer
        # is already there for the reading.
        self._settle_verification()
        if not self._active:
            return False
        text = text or ""
        if text == self._typed:
            return True
        if not self._target_is_ready():
            # Not an error, and deliberately not recorded: the document still
            # holds what it held, so the next update recomputes the same edit
            # against an accurate model and catches up.
            return False

        keep = common_prefix_length(self._typed, text)
        remove = len(self._typed) - keep
        addition = text[keep:]

        if remove > LARGE_REVISION or len(addition) > LARGE_REVISION:
            logger.debug("Large revision: -%d +%d characters.",
                         remove, len(addition))

        # Where the caret is before anything is inserted, kept only for the
        # one-off check that this application accepts posted text at all.
        first_insert = self._unverified and bool(addition)
        before = self._caret() if first_insert else None

        if remove:
            if not win_input.post_backspaces(self._focus_hwnd, remove):
                logger.warning("Backspaces refused; stopping live typing.")
                self._active = False
                return False
        # Recorded as soon as it is sent, not after the whole update: if the
        # insert below fails, what was removed really was removed.
        self._typed = self._typed[:keep]

        if addition:
            if not win_input.post_text(self._focus_hwnd, addition):
                logger.warning("Text could not be posted; stopping live typing.")
                self._active = False
                return False
            self._typed += addition

        if first_insert:
            self._unverified = False
            # Checked at the next update, or at clear() -- see
            # _settle_verification.
            self._verify_from = before
        return self._active

    def _settle_verification(self):
        """Confirm the first insertion actually reached the document.

        A posted WM_CHAR that the application does not handle is discarded in
        silence, so "it was sent" is not "it arrived". The caret is the
        witness: inserting characters moves it, and if it has not moved then
        nothing went in. Live typing then stands down *without* backspacing,
        because backspaces would delete text this never wrote -- the user's
        own.

        The usual case costs one caret read and no waiting, because by the
        time anything calls this the target has long since processed the
        messages. The wait below is only ever paid when the answer really is
        "nothing moved", which is once, at the end, in an application where
        this was never going to work.
        """
        if self._verify_from is None:
            return
        before = self._verify_from

        deadline = time.monotonic() + VERIFY_TIMEOUT
        while True:
            after = self._caret()
            if after is None or after != before:
                # Moved, or no longer answerable -- either way there is
                # nothing here to act on.
                self._verify_from = None
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(VERIFY_INTERVAL)

        logger.warning(
            "The caret has not moved, so this application ignores posted "
            "text. Standing down without removing anything.")
        self._verify_from = None
        self._active = False
        # Forgotten rather than erased: as far as the document is concerned
        # this never happened, and erasing it would erase something else.
        self._typed = ""

    def _caret(self):
        """The caret rectangle now, or None."""
        try:
            return caret_target.locate(self._hwnd).caret
        except Exception:
            return None

    def finish_if_matches(self, text: str) -> bool:
        """Keep the preview where it is when it is already the finished text.

        The confirm path's default is to take the preview back out and paste
        the finished transcript in its place, because the last segments can
        still firm up after the preview typed them. Usually, though, the two
        strings are the same -- and the document is then backspaced over
        character by character only to have the identical text pasted back.

        That round trip is all cost. It doubles the marks left on the target's
        undo stack, it spends a paste on a document that is already correct,
        and it is the one moment in the whole capture where the user's words
        exist nowhere but the clipboard: if the paste half fails -- a target
        that will not take an injected Ctrl+V, a clipboard another application
        is holding -- the delete has already happened and the dictation is
        gone from the screen.

        So when the finished text merely continues what is already there --
        which is what the paste's trailing space makes it -- the tail is
        posted and the preview keeps its place. True means the document now
        says `text` and the caller has nothing left to deliver.

        Verification is settled first, exactly as clear() settles it, so an
        application that only appeared to accept the preview still falls
        through to the paste rather than being credited with text it never
        took.
        """
        self._settle_verification()
        if not self._active or not self._typed:
            return False
        if not text.startswith(self._typed):
            # The finished text is not the preview plus more -- the server
            # revised it. It has to come out and be replaced.
            return False
        if not self._target_is_ready():
            return False
        remainder = text[len(self._typed):]
        if remainder and not win_input.post_text(self._focus_hwnd, remainder):
            logger.warning("Could not post the last %d characters; "
                           "falling back to the paste.", len(remainder))
            return False
        self._typed += remainder
        logger.debug("Kept %d live-typed characters in place, plus %d more.",
                     len(self._typed) - len(remainder), len(remainder))
        # Nothing left to take back: what is in the document is what was
        # wanted, and reset() is what stops anything later trying to remove
        # it.
        self.reset()
        return True

    def clear(self) -> bool:
        """Remove everything typed so far. True if the document is clean again.

        Called on cancel, and before the real paste on confirm when the
        finished text differs from the running preview, so the preview comes
        out and the finished text goes in.
        """
        # Never backspace over text that may not be ours: if the first
        # insertion turns out never to have arrived, this empties the model
        # and there is nothing left to remove.
        self._settle_verification()
        if not self._typed:
            return True
        if not self._target_is_ready():
            logger.warning(
                "Cannot reach the target to remove %d preview characters; "
                "they have been left in the document.", len(self._typed))
            return False
        if not win_input.post_backspaces(self._focus_hwnd, len(self._typed)):
            logger.warning("Could not remove the %d preview characters.",
                           len(self._typed))
            return False
        logger.debug("Removed %d preview characters.", len(self._typed))
        self._typed = ""
        return True

    def _target_is_ready(self) -> bool:
        """True if keystrokes sent right now would land in the target."""
        if not self._hwnd or not is_window(self._hwnd):
            return False
        return get_foreground_window() == self._hwnd
