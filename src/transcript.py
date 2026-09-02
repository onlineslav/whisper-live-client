r"""Rebuilds a whole transcript out of WhisperLive's rolling segment window.

WhisperLive does not send the transcript, it sends the tail of it:
`ServeClientBase.prepare_segments` slices `transcript[-send_last_n_segments:]`
and appends the one in-progress segment. Past that many segments the earliest
words simply stop appearing in the messages, so a client that renders each
message verbatim watches the start of its own dictation get eaten. Segments are
cut at pauses, which is why pausing mid-thought is what brings it on.

Completed segments are immutable server-side -- `update_segments` only ever
appends to `transcript`, and never edits an entry once appended. The window is
therefore a contiguous tail of an append-only list, and keying each segment on
the span it covers is enough to recognise the ones already seen and hold on to
the ones that have scrolled out of the window. Only the trailing
`completed: false` segment is volatile; the server rewrites it on nearly every
message until it either promotes it or drops it, so it is rendered but never
accumulated.
"""

# Whisper is known to emit these strings on silence/noise. Drop any segment
# whose normalized text matches. WhisperLive issue #185 tracks the upstream bug.
HALLUCINATION_PHRASES = {
    "", ".", "you", "thank you", "thanks for watching",
    "thank you for watching", "thanks", "okay", "ok", "bye",
    "the end", "subscribe", "please subscribe",
    "thanks for watching the video", "thank you very much",
}


def is_hallucination(text: str) -> bool:
    normalized = text.strip().lower().strip(".,!?\"' ")
    return normalized in HALLUCINATION_PHRASES


class Transcript:
    """One capture's worth of text, folded together message by message."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Start a new capture. Server-side timestamps restart with the
        connection, so carrying spans across captures would alias one onto
        another."""
        self._done: list[str] = []
        # Span -> index in _done, so a segment still inside the server's
        # window updates in place instead of being appended a second time.
        self._placed: dict[tuple[float, float], int] = {}
        self._pending = ""

    def update(self, message) -> bool:
        """Fold one server message in. True if the rendered text changed."""
        if not isinstance(message, dict):
            return False
        before = self.text()
        segments = message.get("segments")
        if isinstance(segments, list):
            self._merge(segments)
        else:
            # Shapes other WhisperLive versions use. Neither carries the
            # timestamps the merge keys on, so they can only replace the
            # volatile tail -- which is what this client did with every
            # message before, and is no worse for these.
            single = message.get("segment")
            if isinstance(single, dict):
                self._pending = str(single.get("text", ""))
            elif "text" in message:
                self._pending = str(message.get("text", ""))
        return self.text() != before

    def _merge(self, segments):
        pending = ""
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text", ""))
            if not seg.get("completed"):
                # At most one of these, and always last.
                pending = text
                continue
            span = self._span(seg)
            if span is None:
                # No usable timestamps: it cannot be placed, and guessing
                # would risk duplicating it. The window will carry it again
                # on the next message.
                continue
            index = self._placed.get(span)
            if index is None:
                self._placed[span] = len(self._done)
                self._done.append(text)
            else:
                self._done[index] = text
        self._pending = pending

    @staticmethod
    def _span(seg):
        try:
            # Rounded because the server formats these as strings, and a
            # float round-trip is not obliged to land on the same value.
            return (round(float(seg["start"]), 3), round(float(seg["end"]), 3))
        except (KeyError, TypeError, ValueError):
            return None

    def text(self) -> str:
        """Everything heard this capture, hallucinated filler removed.

        Filtering happens here rather than on the way in: a segment the
        server later revises out of filler territory should come back, and
        one that becomes filler should go away.
        """
        parts = [t for t in self._done if not is_hallucination(t)]
        if not is_hallucination(self._pending):
            parts.append(self._pending)
        # Segments arrive with their own leading spaces.
        return "".join(parts).strip()
