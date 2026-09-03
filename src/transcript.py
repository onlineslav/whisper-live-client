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

That last part holds only while the server is honest about the in-progress
segment, and there is one case where it is not. Nothing is committed until
Whisper's decode of the buffer returns more than one segment, or the same text
comes back `same_output_threshold` times in a row; speak without pausing and
neither happens, because each decode returns one segment that keeps growing.
Once more than 25 unprocessed seconds have piled up behind it, `clip_audio`
skips `timestamp_offset` to the last five seconds of the buffer and the audio
under that segment is gone -- uncommitted, never revisited, its text replaced
in the next message by a segment starting twenty seconds later. Held to the
letter of "the pending segment is never accumulated", a whole passage the user
watched appear in the box vanishes from it.

So the pending segment is accumulated in exactly that case: when the next one
starts far enough past it that the span between them will never be transcribed
again. The words were already decoded, and the server discarding the audio
does not make them wrong. See `_rescue_abandoned`.
"""

# Whisper is known to emit these strings on silence/noise. Drop any segment
# whose normalized text matches. WhisperLive issue #185 tracks the upstream bug.
HALLUCINATION_PHRASES = {
    "", ".", "you", "thank you", "thanks for watching",
    "thank you for watching", "thanks", "okay", "ok", "bye",
    "the end", "subscribe", "please subscribe",
    "thanks for watching the video", "thank you very much",
}


# Thresholds for recognising a pending segment the server has abandoned rather
# than committed. All in seconds of the server's own timeline.
#
# How far the next pending segment must start past the abandoned one before the
# jump counts as one at all. A segment that is merely still growing keeps its
# start, and a re-cut moves it by a fraction of a second.
JUMP_SECONDS = 0.5
# The shortest gap worth rescuing. Below this the server is re-cutting segment
# boundaries, not dropping audio, and the same words are about to arrive again
# under a new span -- rescuing there buys a duplicated phrase and nothing else.
# `clip_audio` skips whole tens of seconds, so it clears this easily.
MIN_ABANDONED_SECONDS = 3.0
# Slack when asking whether a span is already covered by committed text. The
# server's pending end and the end it finally commits differ by a hundredth of
# a second or so, which must not read as an uncovered gap.
COVERED_SLACK_SECONDS = 0.25


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
        # The span the pending text covers, and how far the committed text
        # reaches. Together they say whether a pending segment that goes away
        # was committed, superseded, or simply dropped on the floor.
        self._pending_span: tuple[float, float] | None = None
        self._frontier = 0.0

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
        pending_span = None
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text", ""))
            if not seg.get("completed"):
                # At most one of these, and always last.
                pending = text
                pending_span = self._span(seg)
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
            self._frontier = max(self._frontier, span[1])
        # After the completed segments, so the frontier is up to date: whether
        # the outgoing pending segment was committed is most of the question.
        self._rescue_abandoned(pending_span)
        self._pending = pending
        self._pending_span = pending_span

    def _rescue_abandoned(self, new_span):
        """Keep the outgoing pending text if the server dropped its audio.

        Everything here is a reason not to: the words are already committed,
        the segment is merely still growing, or the span is about to be
        transcribed again under a new one. What survives all of them is a
        stretch of speech the server has skipped past and will never send
        again, which is worth keeping even though the seam it leaves may
        repeat a few words -- the next segment starts a little before the
        abandoned one ended, and without word timestamps there is no honest
        place to cut. A repeated phrase can be deleted; a lost sentence
        cannot be typed back from memory.
        """
        old_span = self._pending_span
        if old_span is None or new_span is None or not self._pending.strip():
            # Nothing to compare against. A pending segment that vanishes
            # without a replacement was almost always committed or judged
            # silence, and rescuing on that would duplicate half the capture.
            return
        start, end = old_span
        new_start = new_span[0]
        if old_span in self._placed:
            return  # Committed under its own span: already accumulated.
        if start < self._frontier - COVERED_SLACK_SECONDS:
            return  # Committed text already covers where this began.
        if new_start - start < JUMP_SECONDS:
            return  # The same segment, still growing.
        # What the server skipped, against what the new segment will cover
        # again. The audio under the overlap is still in its buffer, so those
        # words are coming back either way.
        abandoned = new_start - max(start, self._frontier)
        revisited = max(0.0, end - new_start)
        if abandoned < MIN_ABANDONED_SECONDS or abandoned <= revisited:
            return
        # Keyed on its span like any other, so that a completed segment
        # covering the same ground would replace this in place rather than
        # land beside it.
        self._placed[old_span] = len(self._done)
        self._done.append(self._pending)
        self._frontier = max(self._frontier, end)

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
