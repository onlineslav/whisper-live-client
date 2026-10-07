"""Ask the server to transcribe something, and see whether it does.

A WhisperLive server can be up, accept connections and answer SERVER_READY,
and still never transcribe another word: on 2026-10-06 one sat like that for
twelve hours, and every capture showed "Listening..." over a moving meter and
nothing else. Nothing short of real speech tells that apart from a healthy
server -- silence is cut by the server's VAD and produces no reply either way.

So the probe speaks: it opens its own connection, sends a short recorded
phrase, and waits for any text to come back. The phrase is a bundled WAV, so
the check needs no microphone and cannot be fooled by a quiet room.

Verdicts:
  OK           text came back.
  STALLED      the server took the session and then transcribed nothing. This
               is the one that justifies restarting it.
  UNREACHABLE  no connection, or it dropped. Not a stall: the server is down
               or restarting, which server_manager already handles.
  BUSY         the server is full (WAIT). Not a stall either.
"""

import asyncio
import json
import logging
import os
import sys
import threading
import wave

import websockets
from PySide6.QtCore import QObject, Signal

OK = "ok"
STALLED = "stalled"
UNREACHABLE = "unreachable"
BUSY = "busy"

# Long enough for a cold model load -- the probe may be the connection that
# triggers it. A server that never says SERVER_READY at all within this is
# stalled too, just earlier.
READY_TIMEOUT_S = 60.0
# A warm server answers the phrase in about a second. Generous, because a
# false STALLED restarts the server out from under the user.
TEXT_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 5.0

PROBE_UID = "whispertype-healthcheck"


def _asset_path(name: str) -> str:
    # A PyInstaller build unpacks data files under sys._MEIPASS.
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def load_probe_audio(path: str | None = None) -> bytes:
    """The probe phrase as float32 mono 16 kHz PCM, which is what the server reads."""
    import array
    with wave.open(path or _asset_path("health_probe.wav")) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise ValueError("health probe WAV must be 16-bit mono 16 kHz")
        pcm = array.array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    samples = array.array("f", (s / 32768.0 for s in pcm))
    # Trailing silence, so the server has a pause to end the phrase on.
    samples.extend([0.0] * 16000)
    return samples.tobytes()


def _has_text(payload: dict) -> bool:
    return any(str(s.get("text", "")).strip() for s in payload.get("segments") or [])


async def probe(address: str, model: str, audio: bytes,
                ready_timeout: float = READY_TIMEOUT_S,
                text_timeout: float = TEXT_TIMEOUT_S) -> tuple[str, str]:
    """Run one probe. Returns (verdict, detail)."""
    loop = asyncio.get_running_loop()
    try:
        websocket = await asyncio.wait_for(websockets.connect(address), CONNECT_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
        return UNREACHABLE, f"could not connect: {e!r}"

    try:
        # The same handshake as a capture, so the probe takes the path a
        # capture takes -- VAD included.
        from websocket_client import VAD_PARAMETERS
        await websocket.send(json.dumps({
            "uid": PROBE_UID,
            "language": "en",
            "task": "transcribe",
            "model": model,
            "use_vad": True,
            "vad_parameters": VAD_PARAMETERS,
        }))

        deadline = loop.time() + ready_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return STALLED, f"no SERVER_READY within {ready_timeout:.0f}s"
            try:
                payload = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
            except asyncio.TimeoutError:
                continue
            if payload.get("status") == "WAIT":
                return BUSY, "server full"
            if payload.get("message") == "SERVER_READY":
                break

        started = loop.time()
        # All at once: the server buffers frames and transcribes whatever it
        # holds, so pacing it in real time would only make the probe slower.
        await websocket.send(audio)
        deadline = started + text_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return STALLED, f"no text within {text_timeout:.0f}s of speech"
            try:
                raw = await asyncio.wait_for(websocket.recv(), remaining)
            except asyncio.TimeoutError:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and _has_text(payload):
                return OK, f"text after {loop.time() - started:.1f}s"
    except websockets.exceptions.ConnectionClosed as e:
        return UNREACHABLE, f"connection closed: {e!r}"
    except OSError as e:
        return UNREACHABLE, f"connection failed: {e!r}"
    finally:
        try:
            await websocket.send(b"END_OF_AUDIO")
        except Exception:
            pass
        try:
            await asyncio.wait_for(websocket.close(), 3)
        except Exception:
            pass


class HealthProbe(QObject):
    """Runs probes on a worker thread, one at a time.

    `finished` carries (verdict, detail, context): `context` is whatever the
    caller passed to start(), handed back so it can tell what the probe was for.
    """
    finished = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger("whispertype.health")
        self._thread = None
        self._audio = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, address: str, model: str, context: str = "") -> bool:
        """Start a probe. False, and nothing started, if one is still running."""
        if self.is_running():
            return False
        if self._audio is None:
            try:
                self._audio = load_probe_audio()
            except (OSError, ValueError, EOFError):
                self.logger.exception("Health probe audio is missing or unreadable; probes are off.")
                return False
        self._thread = threading.Thread(target=self._run, args=(address, model, context),
                                        name="health-probe", daemon=True)
        self._thread.start()
        return True

    def _run(self, address: str, model: str, context: str):
        try:
            verdict, detail = asyncio.run(probe(address, model, self._audio))
        except Exception as e:  # noqa: BLE001 -- a broken probe must never pass as a stall
            self.logger.exception("Health probe failed to run.")
            verdict, detail = UNREACHABLE, f"probe error: {e!r}"
        self.logger.log(logging.INFO if verdict == OK else logging.WARNING,
                        "Health probe (%s): %s, %s", context or "-", verdict, detail)
        self.finished.emit(verdict, detail, context)
