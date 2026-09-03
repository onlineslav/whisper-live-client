import asyncio
import collections
import json
import logging
import threading
import websockets

import payload_log
from PySide6.QtCore import QObject, Signal

# Audio held while the server loads its model, in bytes. WhisperLive accepts
# the socket, *then* loads the model, and only then starts reading -- so the
# first seconds of a capture would otherwise sit in the OS socket buffer, or
# be dropped outright.
#
# Measured against the GPU container on a cold start: SERVER_READY arrived
# 17.4s after the socket opened, for `tiny.en`. A larger model is slower, so
# this allows 60s at 16 kHz float32 mono (under 4 MB) -- generous enough to
# cover a cold load, bounded so a wedged server cannot grow it forever.
MAX_PENDING_AUDIO_BYTES = 16000 * 4 * 60

# Silence never reaches the decoder as silence: the faster-whisper VAD cuts it
# out first. The defaults (min_silence_duration_ms 2000, speech_pad_ms 400)
# leave over a second of a three-second pause in the clip, which is enough for
# Whisper to break the sentence on its own. Cutting sooner and padding less
# collapses the pause before it can. Lower than this starts clipping soft word
# onsets. Shared with final_pass.py so the replay hears what the stream heard.
VAD_PARAMETERS = {
    "onset": 0.5,
    "min_silence_duration_ms": 400,
    "speech_pad_ms": 100,
}


class WebSocketClient(QObject):
    """

    Manages the WebSocket connection to the transcription server in a separate thread.
    """
    # (status, detail). Status is one of "Connecting", "Loading", "Waiting",
    # "Ready", "Disconnected", "Error"; detail is shown to the user verbatim.
    #
    # "Loading" exists because an accepted TCP connection says nothing about
    # whether the server can transcribe: WhisperLive sends SERVER_READY only
    # once the model is in memory, which on a cold container is tens of
    # seconds after the socket opens. Treating connect as ready is what made
    # the tray claim it was ready while dictation quietly went nowhere.
    connection_status_changed = Signal(str, str)
    message_received = Signal(str)           # Emits the raw JSON string from the server
    
    def __init__(self, server_address, model="distil-small.en", sample_rate=16000, channels=1,
                 audio_format="pcm_s16le", reconnect_after_capture=True):
        super().__init__()
        # Whether to dial straight back after the server hangs up on our
        # END_OF_AUDIO. Warm and free when the server shares one model across
        # connections; when it does not, every reconnect loads another copy of
        # the model server-side, so holding off until the next capture keeps
        # at most one alive.
        self.reconnect_after_capture = reconnect_after_capture
        self.server_address = server_address
        self.model = model
        self.sample_rate = sample_rate
        self.channels = channels
        self.audio_format = audio_format
        self.logger = logging.getLogger("whispertype.websocket")
        self.websocket = None
        self.thread = None
        self.loop = None
        self.is_running = False
        self.uid = "whispertype-client"
        self._eos_sent = False
        self._stop_event: asyncio.Event | None = None
        self._server_ready = False
        # Audio recorded before SERVER_READY arrives, flushed in order once it
        # does. A deque so the oldest is what gets dropped if the cap is hit.
        self._pending_audio = collections.deque()
        self._pending_bytes = 0

    def connect(self):
        if self.thread is None or not self.thread.is_alive():
            self.is_running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            self.logger.info("Starting WebSocket client thread for %s", self.server_address)

    def disconnect(self):
        if not self.is_running:
            return
        self.is_running = False
        if self.loop and self.loop.is_running():
            try:
                if self._stop_event:
                    self.loop.call_soon_threadsafe(self._stop_event.set)
                if self.websocket:
                    asyncio.run_coroutine_threadsafe(self.websocket.close(), self.loop)
            except Exception:
                self.logger.exception("Failed to request websocket shutdown.")
        if self.thread:
            self.thread.join(timeout=3.0)
            if self.thread.is_alive():
                self.logger.warning("WebSocket thread did not exit cleanly within timeout.")
        self.logger.info("WebSocket client disconnected.")

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._stop_event = asyncio.Event()
            self.loop.run_until_complete(self._main_loop())
        except Exception:
            self._emit_status("Error", "The connection thread failed — see the log")
            self.logger.exception("WebSocket client error.")
        finally:
            self._stop_event = None
            if self.loop and not self.loop.is_closed():
                self.loop.close()
            # The client thread has fully stopped — make sure the app knows the
            # connection is gone. Without this, connection_status stays stale at
            # "Connected" and the next capture streams into a dead socket.
            self._emit_status("Disconnected", "")

    def _emit_status(self, status: str, detail: str = ""):
        self.connection_status_changed.emit(status, detail)

    async def _interruptible_sleep(self, seconds: float):
        """Sleep up to `seconds`, waking early if disconnect() sets _stop_event."""
        if self._stop_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._stop_event.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _main_loop(self):
        while self.is_running:
            try:
                self._emit_status("Connecting", f"Connecting to {self.server_address}")
                self.logger.info("Connecting to WebSocket server %s", self.server_address)
                async with websockets.connect(self.server_address) as websocket:
                    self.websocket = websocket
                    self._server_ready = False
                    self._emit_status("Loading", f"Loading the {self.model} model")
                    self.logger.info("WebSocket connected; awaiting SERVER_READY.")
                    self._eos_sent = False

                    # Note: sample_rate / format / channels are silently ignored by the
                    # WhisperLive server (it hardcodes 16k mono float32). The VAD/dedup
                    # knobs below are the ones that actually reduce hallucinations on
                    # short dictation utterances. See docs in README.
                    await self.websocket.send(json.dumps({
                        "uid": self.uid,
                        "language": "en",
                        "task": "transcribe",
                        "model": self.model,
                        "use_vad": True,
                        # How many identical transcriptions in a row before the
                        # server commits the pending segment. Committing also
                        # DROPS the audio behind it (base.py, timestamp_offset
                        # + get_audio_chunk_for_processing), so the next pass
                        # starts from a clip beginning at the pause -- with no
                        # preceding audio, Whisper capitalizes and punctuates it
                        # as a fresh sentence. Each iteration is one transcribe
                        # of the whole buffer (~100-400ms), so the upstream
                        # default of 10 is roughly 2-4s of silence; 4 was under
                        # two, which split ordinary mid-thought pauses into
                        # separate sentences. clip_audio still bounds the buffer
                        # at 25s, so a high value here cannot run away.
                        "same_output_threshold": 12,
                        "vad_parameters": VAD_PARAMETERS,
                        # How much of the transcript each message repeats. The
                        # client reassembles the whole thing from these windows
                        # (transcript.py), so this only has to be wide enough
                        # that no segment slips past unseen: one pass of the
                        # server's update_segments can append several completed
                        # segments before it sends anything, and a segment that
                        # never lands in a single window is gone for good.
                        # Repeating 30 short strings ~20x/second is nothing
                        # over a local socket.
                        "send_last_n_segments": 30,
                        "clip_audio": True,
                    }))
                    self.logger.debug("Handshake sent.")

                    while self.is_running:
                        raw_message = await self.websocket.recv()
                        if isinstance(raw_message, (bytes, bytearray)):
                            try:
                                message = raw_message.decode("utf-8", errors="ignore")
                            except Exception:
                                self.logger.warning("Dropped binary message of len %d (decode failed).", len(raw_message))
                                continue
                        else:
                            message = raw_message
                        payload_log.log_raw(message)
                        if self._handle_control_message(message):
                            continue
                        self.message_received.emit(message)
                        self.logger.debug("Message received (%d bytes).", len(message))

            except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK, OSError) as e:
                self._server_ready = False
                self._emit_status("Disconnected", self._closed_detail(e))
                if self._eos_sent and not self.reconnect_after_capture:
                    # Leave the socket closed until the next capture asks for
                    # it. connect() starts a fresh thread from on_hotkey_activated.
                    self.logger.info("Connection closed after EOS; staying closed until the next capture.")
                    self.is_running = False
                elif self._eos_sent:
                    # Expected: WhisperLive closes the socket in response to our
                    # END_OF_AUDIO. Reconnect immediately so the next capture
                    # has a fresh, live connection ready with minimal delay.
                    self.logger.info("Connection closed after EOS; reconnecting.")
                else:
                    self.logger.warning("Connection closed: %s. Reconnecting...", e)
                    await self._interruptible_sleep(1)
            except Exception:
                self._server_ready = False
                self._emit_status("Error", "Unexpected connection error — retrying")
                self.logger.exception("Unexpected WebSocket error; will retry.")
                await self._interruptible_sleep(5)

    def _closed_detail(self, error) -> str:
        """Explain a dropped connection in the terms the user can act on."""
        if self._eos_sent:
            return ""
        text = str(error).lower()
        if isinstance(error, ConnectionRefusedError) or "refused" in text:
            return "Nothing is listening — the WhisperLive server is not running"
        if "timed out" in text or isinstance(error, TimeoutError):
            return "The server did not respond"
        return "The connection to the server dropped"

    def _handle_control_message(self, message: str) -> bool:
        """Consume WhisperLive's status messages. True if this was one.

        These carry the server's real readiness, which is the whole point:
        SERVER_READY means the model is loaded, WAIT means another client is
        ahead of us in the queue. Neither is transcript, so neither should
        reach the capture box.
        """
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False

        # `message` doubles as the status field in some server versions and as
        # a free-text detail in others, so both are read.
        kind = str(payload.get("message", ""))
        status = str(payload.get("status", ""))

        if kind == "SERVER_READY":
            backend = payload.get("backend", "")
            self._server_ready = True
            self._emit_status("Ready", f"Model loaded ({backend})" if backend else "Model loaded")
            self._flush_pending_audio()
            return True

        if status == "WAIT":
            # WhisperLive reports the estimated wait in minutes.
            try:
                minutes = float(kind)
                detail = f"Server busy — about {max(1, round(minutes))} min until a slot frees up"
            except (TypeError, ValueError):
                detail = "Server busy — waiting for a free slot"
            self._emit_status("Waiting", detail)
            return True

        if status == "ERROR" or kind == "ERROR":
            detail = str(payload.get("message") or "The server reported an error")
            self._emit_status("Error", detail if detail != "ERROR" else "The server reported an error")
            return True

        if kind == "DISCONNECT":
            self._server_ready = False
            self._emit_status("Disconnected", "The server closed the session")
            return True

        return False

    def _flush_pending_audio(self):
        """Send everything recorded while the model was still loading."""
        if not self._pending_audio:
            return
        chunks = list(self._pending_audio)
        self._pending_audio.clear()
        self._pending_bytes = 0
        self.logger.info("Flushing %d buffered audio chunks held during model load.", len(chunks))
        for chunk in chunks:
            self._dispatch_audio(chunk)

    def send_audio(self, audio_chunk):
        if not (self.websocket and self.loop and self.is_running and not self._eos_sent):
            return
        if not self._server_ready:
            # Hold it rather than dropping it. The server is not reading yet,
            # and the words spoken during a cold model load are exactly the
            # ones the user will assume were transcribed.
            self._pending_audio.append(audio_chunk)
            self._pending_bytes += len(audio_chunk)
            while self._pending_bytes > MAX_PENDING_AUDIO_BYTES and self._pending_audio:
                self._pending_bytes -= len(self._pending_audio.popleft())
            return
        self._dispatch_audio(audio_chunk)

    def _dispatch_audio(self, audio_chunk):
        self.logger.debug("Sending audio chunk (%d bytes).", len(audio_chunk))
        fut = asyncio.run_coroutine_threadsafe(self.websocket.send(audio_chunk), self.loop)
        fut.add_done_callback(self._on_send_done)

    def _on_send_done(self, fut):
        try:
            fut.result()
        except Exception:
            self.logger.debug("Audio chunk dropped: connection was transitioning.")

    def send_eos(self):
        """Sends the End of Stream message."""
        if self.websocket and self.loop and self.is_running:
            if self._eos_sent:
                return
            self._eos_sent = True
            # WhisperLive expects a binary sentinel
            self.logger.debug("Sending EOS marker (binary END_OF_AUDIO).")
            asyncio.run_coroutine_threadsafe(self.websocket.send(b"END_OF_AUDIO"), self.loop)

    def reset_eos(self):
        """Allow sending EOS again on the next capture."""
        self._eos_sent = False
        # Audio held over from a previous capture is stale by now; sending it
        # would prepend the last utterance to this one.
        self._pending_audio.clear()
        self._pending_bytes = 0

    @property
    def is_ready(self) -> bool:
        """True when the server has confirmed its model is loaded."""
        return self._server_ready

if __name__ == '__main__':
    # Example usage for testing
    import sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication(sys.argv)
    
    # Replace with your server address if different
    SERVER_URL = "ws://localhost:9090"
    
    client = WebSocketClient(SERVER_URL)
    
    def on_status_change(status, detail=""):
        print(f"Connection status: {status} ({detail})")
        if status == "Ready":
            # Simulate sending a blank audio chunk and EOS after 2 seconds
            QTimer.singleShot(2000, lambda: client.send_audio(b'\x00'*1024))
            QTimer.singleShot(2500, client.send_eos)
            print("Sent dummy audio and EOS.")

    def on_message(message):
        print(f"Received from server: {message}")

    client.connection_status_changed.connect(on_status_change)
    client.message_received.connect(on_message)
    
    client.connect()

    print("Test client running for 10 seconds...")
    QTimer.singleShot(10000, app.quit)
    
    sys.exit(app.exec())
