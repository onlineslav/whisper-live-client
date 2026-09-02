r"""Re-transcribes a finished capture in one shot, for the text that gets pasted.

The live stream is a preview. It has to be, because WhisperLive transcribes a
rolling buffer and throws audio away as it goes: every time a segment is
committed (`update_segments` in the server's backend/base.py) the audio behind
it is dropped from the buffer, so the next pass over the microphone starts from
a clip that begins at the pause. Whisper, handed a clip that starts mid-thought
with no preceding audio, does the only thing it can -- capitalises the first
word and ends it with a full stop. That is where dictation with ordinary
mid-sentence pauses comes out chopped into sentences that were never spoken.

Nothing in the protocol can undo that after the fact; the audio is gone
server-side. So the capture is kept here as well, and on confirm it is replayed
to a second, short-lived connection all at once. The server's loop then finds
the whole utterance sitting in its buffer on the first iteration and transcribes
it in a single call, which is the same thing as running Whisper over a recording
of the capture -- one decoder pass, full context, punctuation chosen knowing how
the sentence ends. The result replaces the streamed text at paste time.

The handshake below is deliberately not the live one: everything that makes the
server commit early and discard audio is turned off, because committing early is
the entire problem this pass exists to avoid.

Failure is always survivable -- the caller falls back to the streamed text, which
is what it would have pasted anyway.
"""

import asyncio
import json
import logging
import threading
import time

import websockets
from PySide6.QtCore import QObject, Signal

from transcript import Transcript

# Bytes per second of the captured stream: 16 kHz, mono, float32.
BYTES_PER_SECOND = 16000 * 4

# Longest capture worth replaying. The server drops the oldest 30s from its
# buffer once it holds more than 45 (base.py add_frames), so past that the
# replay would silently lose its own beginning -- worse than the streamed text,
# which at least saw all of it. Captures this long are not what the app is for.
MAX_REPLAY_SECONDS = 40

# Shortest capture worth replaying. The server's loop ignores anything under a
# second of buffer, so a shorter clip would sit there until the timeout.
MIN_REPLAY_SECONDS = 1.2

# Frame size for the replay. The server's websockets default caps a received
# message at 1 MB; well under it, and large enough that a 40s capture goes over
# in a few dozen frames.
REPLAY_FRAME_BYTES = 65536

# How long the text must hold still before it is taken as final, once the
# segments account for the whole clip. Short, because by then the server is
# only re-sending a tail it has already settled on.
QUIET_MS = 250

# The same, before the clip is accounted for. Long, because silence here does
# not mean the server is done -- it means it is mid-decode, and a whole-clip
# decode is exactly the slow one. Cutting this short is how the pass would
# return half an utterance, which is worse than not running at all.
UNCOVERED_QUIET_MS = 1200

# How far short of the clip's end the segments may stop while still counting
# as covering it. Captures end on the audio gate's hangover, so the last
# segment usually stops a little before the last sample.
COVERAGE_SLACK_SECONDS = 1.0

# Connect timeout. The server is on this machine and already loaded -- it
# accepts at once or it is not there, and every second spent finding that out
# is a second of the user waiting for their text.
CONNECT_TIMEOUT_SECONDS = 1.5

# Hard ceiling on the whole round trip, from connect to giving up. Spent
# between the user confirming and the paste landing, so it is a latency budget
# as much as a timeout: past this the streamed text is the better trade.
TIMEOUT_MS = 3500


class FinalPass(QObject):
    """One replay of one capture. Emits exactly once per run()."""

    # The re-transcribed text, or "" if this pass produced nothing usable.
    # Always emitted, on the thread that constructed the object.
    finished = Signal(str)

    def __init__(self, server_address, model, vad_parameters=None, parent=None):
        super().__init__(parent)
        self.server_address = server_address
        self.model = model
        self.vad_parameters = vad_parameters
        self.logger = logging.getLogger("whisperboard.finalpass")
        self._thread = None
        self._generation = 0

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @staticmethod
    def is_replayable(audio: bytes) -> bool:
        """Whether a capture is worth replaying at all."""
        seconds = len(audio) / BYTES_PER_SECOND
        return MIN_REPLAY_SECONDS <= seconds <= MAX_REPLAY_SECONDS

    def run(self, audio: bytes):
        """Replay `audio` on a background thread. `finished` follows, always."""
        # A pass still in flight belongs to a capture the user has moved on
        # from. Bumping the generation makes its result unwelcome rather than
        # trying to stop it mid-recv.
        self._generation += 1
        generation = self._generation
        self._thread = threading.Thread(
            target=self._run_blocking, args=(audio, generation), daemon=True)
        self._thread.start()

    def abandon(self):
        """Disown a pass in flight; its result will not be emitted."""
        self._generation += 1

    def _run_blocking(self, audio: bytes, generation: int):
        text = ""
        started = time.monotonic()
        try:
            text = asyncio.run(self._replay(audio))
        except Exception:
            # Includes the connection simply not being there. The caller has a
            # transcript either way, so this is worth a line in the log and
            # nothing more.
            self.logger.info("Final pass did not complete; keeping the streamed text.",
                             exc_info=True)
        elapsed = (time.monotonic() - started) * 1000
        if generation != self._generation:
            self.logger.debug("Final pass finished after %.0fms but was superseded.", elapsed)
            return
        self.logger.info("Final pass finished in %.0fms (%d chars).", elapsed, len(text))
        self.finished.emit(text)

    def _handshake(self) -> str:
        options = {
            "uid": "whisperboard-final-pass",
            "language": "en",
            "task": "transcribe",
            "model": self.model,
            "use_vad": True,
            # Every knob that makes the server commit a segment early, and so
            # drop the audio behind it, is pushed out of reach. The clip is
            # already complete when it arrives: there is nothing to gain by
            # finalising part of it, and a commit costs exactly the context
            # this pass exists to preserve.
            #
            # clip_audio especially. It runs before the first transcribe and
            # skips the offset to the last five seconds whenever more than 25
            # unprocessed seconds are sitting in the buffer -- which, for a
            # capture that arrives all at once, is the normal case rather than
            # the pathological one it was written for.
            "same_output_threshold": 60,
            "clip_audio": False,
            "send_last_n_segments": 60,
            "vad_parameters": self.vad_parameters,
        }
        if not options["vad_parameters"]:
            del options["vad_parameters"]
        return json.dumps(options)

    async def _replay(self, audio: bytes) -> str:
        transcript = Transcript()
        duration = len(audio) / BYTES_PER_SECOND
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        quiet_until = None
        # How far into the clip the segments reach. The server timestamps them
        # against the audio it was given, so this says plainly whether it has
        # read to the end yet -- which a gap in the messages does not.
        covered = 0.0

        async with websockets.connect(
                self.server_address, open_timeout=CONNECT_TIMEOUT_SECONDS) as ws:
            await ws.send(self._handshake())
            while True:
                now = time.monotonic()
                timeout = deadline - now
                if quiet_until is not None:
                    timeout = min(timeout, quiet_until - now)
                if timeout <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout)
                except asyncio.TimeoutError:
                    # Either the text has held still long enough, or the whole
                    # budget is spent. Both mean: take what there is.
                    break

                message = raw.decode("utf-8", errors="ignore") if isinstance(
                    raw, (bytes, bytearray)) else raw
                try:
                    payload = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue

                kind = str(payload.get("message", ""))
                status = str(payload.get("status", ""))
                if kind == "SERVER_READY":
                    await self._send_audio(ws, audio)
                    continue
                if status in ("WAIT", "ERROR") or kind in ("ERROR", "DISCONNECT"):
                    # Nothing to wait for: the server is full, broken, or done
                    # with us. The streamed text stands.
                    self.logger.info("Final pass turned away by the server: %s", message)
                    return ""

                covered = max(covered, self._covered_to(payload))
                if transcript.update(payload):
                    window = (QUIET_MS if covered >= duration - COVERAGE_SLACK_SECONDS
                              else UNCOVERED_QUIET_MS)
                    quiet_until = time.monotonic() + window / 1000

        return transcript.text()

    @staticmethod
    def _covered_to(payload) -> float:
        """How far into the clip this message's segments reach, in seconds."""
        segments = payload.get("segments")
        if not isinstance(segments, list):
            return 0.0
        ends = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                ends.append(float(seg["end"]))
            except (KeyError, TypeError, ValueError):
                continue
        return max(ends, default=0.0)

    async def _send_audio(self, ws, audio: bytes):
        """Push the whole capture as fast as the socket takes it.

        Speed is the point. The server's transcription loop ignores a buffer
        under a second and otherwise transcribes whatever it holds, so the
        sooner the clip is complete the more likely the first pass over it is
        also the only one -- one decode, whole utterance, full context.
        """
        for start in range(0, len(audio), REPLAY_FRAME_BYTES):
            await ws.send(audio[start:start + REPLAY_FRAME_BYTES])
        self.logger.debug("Replayed %.1fs of audio for the final pass.",
                          len(audio) / BYTES_PER_SECOND)
