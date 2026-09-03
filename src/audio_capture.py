import array
import collections
import logging
import math
import pyaudio
import threading
from PySide6.QtCore import QObject, Signal

# Level reported for a digitally silent chunk. Real microphones never reach it
# -- it is the "no signal at all" rail the meter parks on between captures.
SILENCE_DBFS = -100.0


def dbfs_from_rms(rms: float) -> float:
    """Convert an RMS level (float32 samples in -1..1) to dBFS."""
    if rms <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(rms))


class AudioCapture(QObject):
    """
    Captures audio from the microphone in a separate thread and emits it.
    """
    audio_chunk_ready = Signal(bytes)
    # Level of every chunk in dBFS, emitted whether or not the chunk passes the
    # speech gate below -- the meter is there to show the user that the mic is
    # live, so it has to move during silence too. Raw dB rather than a 0-1
    # reading: the meter's own auto-ranging works in the dB domain, and this
    # side of the wire has no business deciding what "full scale" means.
    level_changed = Signal(float)

    def __init__(self, channels=1, rate=16000, chunk_size=1024):
        super().__init__()
        self.channels = channels
        self.rate = rate
        self.chunk_size = chunk_size
        # WhisperLive server reads float32 frames and treats b"END_OF_AUDIO" as EOS
        self.format = pyaudio.paFloat32  # 32-bit float PCM
        self.audio_format = "f32le"  # Handshake/metadata format string
        self.logger = logging.getLogger("whispertype.audio")

        # Client-side voice activity gating. Whisper hallucinates words when fed
        # near-silent audio, and the server's own VAD leaks. Gating here means
        # silence never reaches the server, so it cannot hallucinate on it.
        #   - threshold: RMS of a chunk (float32 samples in -1..1) below this is
        #     treated as silence. ~0.012 sits above mic noise floor, below speech.
        #   - hangover: keep streaming this many chunks after speech stops so
        #     trailing word tails and short inter-word pauses are not clipped.
        #   - preroll: chunks of pre-speech audio flushed on speech onset so the
        #     first syllable is not lost. (~64ms per chunk at 1024/16000.)
        self._silence_threshold = 0.012
        self._hangover_chunks = 8
        self._preroll_chunks = 3

        self._p = pyaudio.PyAudio()
        self._stream = None
        self._thread = None
        self._is_running = False
        self._meter_only = False

    def start_streaming(self, meter_only: bool = False):
        """Open the mic and stream.

        `meter_only` opens the mic for its level alone and emits no audio
        chunks -- it drives the Settings window's meter preview. Without it the
        preview would feed the live WebSocket session, and a persistent
        connection would quietly transcribe whatever was said while someone was
        only trying to pick a meter style.
        """
        if self._thread is None or not self._thread.is_alive():
            self._meter_only = meter_only
            self._is_running = True
            self._thread = threading.Thread(target=self._run_capture, daemon=True)
            self._thread.start()
            self.logger.info("Audio capture started.")

    def stop_streaming(self):
        self._is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self.logger.info("Audio capture stopped.")
    
    @staticmethod
    def _chunk_rms(data: bytes) -> float:
        """Root-mean-square level of a float32 PCM chunk."""
        samples = array.array("f")
        samples.frombytes(data)
        if not samples:
            return 0.0
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    def _run_capture(self):
        self._stream = None
        preroll = collections.deque(maxlen=self._preroll_chunks)
        speaking = False
        silent_run = 0
        try:
            self._stream = self._p.open(format=self.format,
                                        channels=self.channels,
                                        rate=self.rate,
                                        input=True,
                                        frames_per_buffer=self.chunk_size)

            while self._is_running:
                try:
                    data = self._stream.read(self.chunk_size, exception_on_overflow=False)
                except IOError as e:
                    self.logger.warning("Audio capture warning: %s", e)
                    continue

                rms = self._chunk_rms(data)
                self.level_changed.emit(dbfs_from_rms(rms))
                if self._meter_only:
                    continue
                is_speech = rms >= self._silence_threshold
                if is_speech:
                    silent_run = 0
                    if not speaking:
                        # Speech onset: flush buffered pre-roll so the first
                        # syllable is not clipped, then mark as speaking.
                        for buffered in preroll:
                            self.audio_chunk_ready.emit(buffered)
                        preroll.clear()
                        speaking = True
                    self.audio_chunk_ready.emit(data)
                elif speaking:
                    # Silence after speech: keep streaming through the hangover
                    # window (covers word tails and brief pauses), then stop.
                    silent_run += 1
                    if silent_run <= self._hangover_chunks:
                        self.audio_chunk_ready.emit(data)
                    else:
                        speaking = False
                        preroll.append(data)
                else:
                    # Sustained silence: buffer as pre-roll, send nothing.
                    preroll.append(data)

        except Exception:
            self.logger.exception("Audio capture failed to open stream.")
        finally:
            # Park the meter at silence: the last chunk read is usually
            # mid-speech, and without this the meter would stay pinned there
            # until the next capture starts.
            self.level_changed.emit(SILENCE_DBFS)
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
