import logging
import pyaudio
import threading
from PySide6.QtCore import QObject, Signal

class AudioCapture(QObject):
    """
    Captures audio from the microphone in a separate thread and emits it.
    """
    audio_chunk_ready = Signal(bytes)

    def __init__(self, channels=1, rate=16000, chunk_size=1024):
        super().__init__()
        self.channels = channels
        self.rate = rate
        self.chunk_size = chunk_size
        # WhisperLive server reads float32 frames and treats b"END_OF_AUDIO" as EOS
        self.format = pyaudio.paFloat32  # 32-bit float PCM
        self.audio_format = "f32le"  # Handshake/metadata format string
        self.logger = logging.getLogger("whisperboard.audio")

        self._p = pyaudio.PyAudio()
        self._stream = None
        self._thread = None
        self._is_running = False

    def start_streaming(self):
        if self._thread is None or not self._thread.is_alive():
            self._is_running = True
            self._thread = threading.Thread(target=self._run_capture, daemon=True)
            self._thread.start()
            self.logger.info("Audio capture started.")

    def stop_streaming(self):
        self._is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self.logger.info("Audio capture stopped.")
    
    def _run_capture(self):
        self._stream = None
        try:
            self._stream = self._p.open(format=self.format,
                                        channels=self.channels,
                                        rate=self.rate,
                                        input=True,
                                        frames_per_buffer=self.chunk_size)

            while self._is_running:
                try:
                    data = self._stream.read(self.chunk_size, exception_on_overflow=False)
                    self.audio_chunk_ready.emit(data)
                except IOError as e:
                    self.logger.warning("Audio capture warning: %s", e)
                    continue

        except Exception:
            self.logger.exception("Audio capture failed to open stream.")
        finally:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
                self._stream = None

    def shutdown(self):
        """Stop capture and release PyAudio. Call before app exit."""
        self.stop_streaming()
        self._p.terminate()

if __name__ == '__main__':
    # Example usage for testing
    import sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication(sys.argv)
    
    audio_capture = AudioCapture()

    def handle_chunk(chunk):
        print(f"Captured {len(chunk)} bytes of audio data.")

    audio_capture.audio_chunk_ready.connect(handle_chunk)
    
    # Start capturing
    audio_capture.start_streaming()

    # Stop capturing after 3 seconds
    QTimer.singleShot(3000, audio_capture.stop_streaming)
    # Quit the app after 4 seconds
    QTimer.singleShot(4000, app.quit)

    print("Test audio capture running for 3 seconds...")
    sys.exit(app.exec())
