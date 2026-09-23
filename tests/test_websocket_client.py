"""Connection-handling tests for WebSocketClient, against a fake server.

The fake speaks just enough WhisperLive to matter here: it reads the
handshake, reports SERVER_READY after a delay, records every audio frame per
connection, and closes on END_OF_AUDIO. The tests drive the client the way
the app does -- send_audio from another thread, send_eos at the end of a
capture -- and check what the server actually received, in what order.

Run with:  python -m unittest discover tests
"""

import asyncio
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

os.environ.setdefault("WHISPERTYPE_PAYLOAD_LOG", "0")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from websocket_client import WebSocketClient  # noqa: E402

# Status signals are emitted on the client's thread and queued to this one,
# exactly as they are in the app, so something here has to deliver them.
APP = QCoreApplication.instance() or QCoreApplication([])


class FakeServer:
    """A WhisperLive stand-in on a free local port, in its own thread."""

    def __init__(self, ready_delay=0.0):
        self.ready_delay = ready_delay
        # One list of received audio frames per connection, in order.
        self.connections: list[list[bytes]] = []
        self._current = None
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started.wait(5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        self._stop = asyncio.Event()
        async with serve(self._handle, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._started.set()
            await self._stop.wait()

    async def _handle(self, websocket):
        frames: list[bytes] = []
        self.connections.append(frames)
        self._current = websocket
        await websocket.recv()  # handshake
        await asyncio.sleep(self.ready_delay)
        await websocket.send('{"uid": "t", "message": "SERVER_READY", "backend": "fake"}')
        try:
            async for message in websocket:
                if message == b"END_OF_AUDIO":
                    await websocket.close()
                    return
                frames.append(message)
        except Exception:
            pass

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def drop(self):
        """Close the live connection from the server side, as a crash would."""
        websocket = self._current
        asyncio.run_coroutine_threadsafe(websocket.close(), self._loop).result(5)

    def close(self):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def chunk(n: int) -> bytes:
    return n.to_bytes(4, "little") * 16


class ClientTestCase(unittest.TestCase):
    reconnect_after_capture = True
    ready_delay = 0.0

    def setUp(self):
        self.server = FakeServer(self.ready_delay)
        self.client = WebSocketClient(
            self.server.url, reconnect_after_capture=self.reconnect_after_capture)
        self.statuses = []
        self.client.connection_status_changed.connect(
            lambda status, detail: self.statuses.append(status))

    def tearDown(self):
        self.client.disconnect()
        self.server.close()

    def connect_and_wait(self):
        self.client.connect()
        self.assertTrue(wait_for(lambda: self.client.is_ready), "never ready")


class DroppedConnection(ClientTestCase):

    def test_audio_spoken_through_a_drop_reaches_the_replacement(self):
        """What is said while the connection is down is held, then sent to
        the new session ahead of anything newer."""
        self.connect_and_wait()
        self.client.send_audio(chunk(1))
        self.assertTrue(wait_for(lambda: self.server.connections[0] == [chunk(1)]))

        self.server.drop()
        self.assertTrue(wait_for(lambda: not self.client.is_ready))
        self.client.send_audio(chunk(2))
        self.client.send_audio(chunk(3))

        self.assertTrue(wait_for(lambda: self.client.is_ready), "did not reconnect")
        self.client.send_audio(chunk(4))
        self.assertTrue(wait_for(lambda: len(self.server.connections) == 2
                                 and len(self.server.connections[1]) == 3))
        self.assertEqual(self.server.connections[1], [chunk(2), chunk(3), chunk(4)])

    def test_status_reports_the_drop(self):
        self.connect_and_wait()
        self.server.drop()
        self.assertTrue(wait_for(lambda: "Disconnected" in self.statuses))
        self.assertTrue(wait_for(lambda: self.statuses[-1] == "Ready"))


class SlowModelLoad(ClientTestCase):
    ready_delay = 0.3

    def test_no_chunk_is_stranded_or_reordered_around_ready(self):
        """Audio streams in from another thread while SERVER_READY lands.
        Every chunk must arrive, once, in order -- the race was a chunk that
        saw "not ready", then got buffered just after the flush."""
        self.client.connect()
        stop = threading.Event()
        sent = []

        def speak():
            n = 0
            while not stop.is_set():
                self.client.send_audio(chunk(n))
                sent.append(n)
                n += 1
                time.sleep(0.001)

        speaker = threading.Thread(target=speak)
        # Only once the socket is open: before that, send_audio has no loop
        # and drops audio by design.
        self.assertTrue(wait_for(lambda: self.client.loop is not None
                                 and "Loading" in self.statuses))
        speaker.start()
        self.assertTrue(wait_for(lambda: self.client.is_ready))
        time.sleep(0.2)
        stop.set()
        speaker.join()
        self.assertTrue(wait_for(lambda: len(self.server.connections[0]) == len(sent)),
                        f"{len(self.server.connections[0])} of {len(sent)} arrived")
        self.assertEqual(self.server.connections[0], [chunk(n) for n in sent])


class SpentSession(ClientTestCase):

    def test_not_ready_once_end_of_audio_is_sent(self):
        """The server closes the session in answer to END_OF_AUDIO, so the
        next capture must not see it as ready and stream into it."""
        self.connect_and_wait()
        self.client.send_eos()
        self.assertFalse(self.client.is_ready)
        self.assertTrue(wait_for(lambda: len(self.server.connections) == 2
                                 and self.client.is_ready), "no fresh session")

    def test_audio_after_end_of_audio_goes_to_the_next_session(self):
        self.connect_and_wait()
        self.client.send_eos()
        # The next capture starts at once, before the redial has finished.
        self.client.reset_eos()
        self.client.send_audio(chunk(7))
        self.assertTrue(wait_for(lambda: len(self.server.connections) == 2
                                 and self.server.connections[1] == [chunk(7)]))
        self.assertEqual(self.server.connections[0], [])


class StaysClosedAfterCapture(ClientTestCase):
    reconnect_after_capture = False

    def test_connect_restarts_a_thread_that_stopped(self):
        self.connect_and_wait()
        self.client.send_eos()
        self.assertTrue(wait_for(lambda: not self.client.thread.is_alive()))
        self.client.connect()
        self.assertTrue(wait_for(lambda: self.client.is_ready), "did not restart")
        self.assertEqual(len(self.server.connections), 2)


if __name__ == "__main__":
    unittest.main()
