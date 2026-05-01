import asyncio
import json
import logging
import threading
import websockets
from PySide6.QtCore import QObject, Signal

class WebSocketClient(QObject):
    """

    Manages the WebSocket connection to the transcription server in a separate thread.
    """
    connection_status_changed = Signal(str)  # Emits "Connecting", "Connected", "Disconnected", "Error"
    message_received = Signal(str)           # Emits the raw JSON string from the server
    
    def __init__(self, server_address, model="tiny.en", sample_rate=16000, channels=1, audio_format="pcm_s16le"):
        super().__init__()
        self.server_address = server_address
        self.model = model
        self.sample_rate = sample_rate
        self.channels = channels
        self.audio_format = audio_format
        self.logger = logging.getLogger("whisperboard.websocket")
        self.websocket = None
        self.thread = None
        self.loop = None
        self.is_running = False
        self.uid = "whisperboard-client" # A unique ID for the client
        self._eos_sent = False

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
        # Nudge the event loop to exit its wait and close the websocket cleanly
        if self.loop and self.loop.is_running():
            try:
                if self.websocket:
                    asyncio.run_coroutine_threadsafe(self.websocket.close(), self.loop)
                self.loop.call_soon_threadsafe(lambda: None)
            except Exception:
                self.logger.exception("Failed to request websocket shutdown.")
        if self.thread:
            self.thread.join()
        self.connection_status_changed.emit("Disconnected")
        self.logger.info("WebSocket client disconnected.")

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._main_loop())
        except Exception as e:
            self.connection_status_changed.emit("Error")
            self.logger.exception("WebSocket client error.")
        finally:
            if self.loop and not self.loop.is_closed():
                self.loop.close()

    async def _main_loop(self):
        while self.is_running:
            try:
                self.connection_status_changed.emit("Connecting")
                self.logger.info("Connecting to WebSocket server %s", self.server_address)
                async with websockets.connect(self.server_address) as websocket:
                    self.websocket = websocket
                    self.connection_status_changed.emit("Connected")
                    self.logger.info("WebSocket connected.")
                    self._eos_sent = False
                    
                    # Send handshake
                    await self.websocket.send(json.dumps({
                        "uid": self.uid,
                        "language": "en",
                        "task": "transcribe",
                        "model": self.model,
                        "sample_rate": self.sample_rate,
                        "format": self.audio_format,
                        "channels": self.channels,
                    }))
                    self.logger.debug("Handshake sent.")

                    # Listen for messages
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
                        self.message_received.emit(message)
                        self.logger.debug("Message received (%d bytes).", len(message))

            except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK, OSError) as e:
                self.connection_status_changed.emit("Disconnected")
                self.logger.warning("Connection closed: %s. Reconnecting...", e)
                await asyncio.sleep(1)  # Wait before retrying
            except Exception as e:
                self.connection_status_changed.emit("Error")
                self.logger.exception("Unexpected WebSocket error.")
                self.is_running = False # Stop on unexpected errors

    def send_audio(self, audio_chunk):
        if self.websocket and self.loop and self.is_running and not self._eos_sent:
            self.logger.debug("Sending audio chunk (%d bytes).", len(audio_chunk))
            asyncio.run_coroutine_threadsafe(self.websocket.send(audio_chunk), self.loop)

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

if __name__ == '__main__':
    # Example usage for testing
    import sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication(sys.argv)
    
    # Replace with your server address if different
    SERVER_URL = "ws://localhost:9090"
    
    client = WebSocketClient(SERVER_URL)
    
    def on_status_change(status):
        print(f"Connection status: {status}")
        if status == "Connected":
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
