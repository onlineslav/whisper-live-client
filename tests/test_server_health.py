"""Tests for the stall defences: server_patch.py and the health probe.

server_patch runs inside the WhisperLive container, but it is stdlib only and
patches whatever class it is handed, so it is tested here against a stand-in.
The probe is run against a fake server that either answers speech or, as on
2026-10-06, takes it and says nothing.

Run with:  python -m unittest discover tests
"""

import asyncio
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

os.environ.setdefault("WHISPERTYPE_PAYLOAD_LOG", "0")

from websockets.asyncio.server import serve  # noqa: E402

import server_health  # noqa: E402
import server_manager  # noqa: E402
import server_patch  # noqa: E402


class StandInClient:
    """The shape of ServeClientFasterWhisper that the patch relies on."""
    SINGLE_MODEL_LOCK = threading.Lock()

    def __init__(self, fail=False):
        self.fail = fail

    def transcribe_audio(self, sample):
        # The stock code: acquire, work, release -- no finally.
        type(self).SINGLE_MODEL_LOCK.acquire()
        if self.fail:
            raise RuntimeError("decoder blew up")
        type(self).SINGLE_MODEL_LOCK.release()
        return [sample]


class ServerPatch(unittest.TestCase):

    def setUp(self):
        self.cls = type("Client", (StandInClient,), {})
        self.lock = server_patch.harden(self.cls)

    def test_a_failed_transcription_gives_the_lock_back(self):
        with self.assertRaises(RuntimeError):
            self.cls(fail=True).transcribe_audio("a")
        self.assertFalse(self.lock.locked())
        # And the next one is not stuck behind it.
        self.assertEqual(self.cls().transcribe_audio("b"), ["b"])

    def test_a_good_transcription_is_untouched(self):
        self.assertEqual(self.cls().transcribe_audio("a"), ["a"])
        self.assertFalse(self.lock.locked())

    def test_never_releases_a_lock_another_thread_holds(self):
        self.lock.acquire()
        try:
            self.assertFalse(_in_thread(self.lock.release_if_owned))
            self.assertTrue(self.lock.locked())
        finally:
            self.lock.release()

    def test_watch_exits_once_the_lock_is_held_too_long(self):
        exits = []
        self.lock.acquire()
        try:
            self.lock.since -= 30  # held for half a minute already
            server_patch.watch(self.lock, stall_s=20, exit_process=exits.append, poll_s=0)
        finally:
            self.lock.release()
        self.assertEqual(exits, [3])

    def test_held_for_is_zero_when_free(self):
        self.assertEqual(self.lock.held_for(), 0.0)

    def test_the_container_command_carries_the_patch(self):
        command = server_manager.ServerManager({})._server_command("/models/x")
        self.assertEqual(command[:2], ["python", "-c"])
        self.assertIn("def harden", command[2])
        self.assertEqual(command[3:], ["--port", "9090", "-fw", "/models/x"])
        compile(command[2], "server_patch", "exec")


def _in_thread(fn):
    result = []
    t = threading.Thread(target=lambda: result.append(fn()))
    t.start()
    t.join(5)
    return result[0]


class FakeWhisperLive:
    """Says SERVER_READY, then answers audio with text -- or never does."""

    def __init__(self, answers=True, ready=True):
        self.answers = answers
        self.ready = ready
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
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
        await websocket.recv()
        if self.ready:
            await websocket.send(json.dumps({"uid": "t", "message": "SERVER_READY"}))
        try:
            async for message in websocket:
                if message == b"END_OF_AUDIO":
                    return
                if self.answers:
                    await websocket.send(json.dumps(
                        {"uid": "t", "segments": [{"text": " Testing one two three."}]}))
        except Exception:
            pass

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def close(self):
        self._loop.call_soon_threadsafe(self._stop.set)


class Probe(unittest.TestCase):

    def run_probe(self, server, **timeouts):
        audio = server_health.load_probe_audio()
        try:
            return asyncio.run(server_health.probe(server.url, "m", audio, **timeouts))
        finally:
            server.close()

    def test_a_server_that_answers_is_ok(self):
        verdict, _ = self.run_probe(FakeWhisperLive())
        self.assertEqual(verdict, server_health.OK)

    def test_a_server_that_takes_speech_and_says_nothing_is_stalled(self):
        started = time.monotonic()
        verdict, _ = self.run_probe(FakeWhisperLive(answers=False), text_timeout=0.5)
        self.assertEqual(verdict, server_health.STALLED)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_server_that_never_gets_ready_is_stalled(self):
        verdict, _ = self.run_probe(FakeWhisperLive(ready=False), ready_timeout=0.5)
        self.assertEqual(verdict, server_health.STALLED)

    def test_nothing_listening_is_unreachable_not_stalled(self):
        """A server that is down is server_manager's job, not a stall."""
        audio = server_health.load_probe_audio()
        verdict, _ = asyncio.run(server_health.probe("ws://127.0.0.1:1", "m", audio))
        self.assertEqual(verdict, server_health.UNREACHABLE)

    def test_the_bundled_phrase_loads(self):
        audio = server_health.load_probe_audio()
        seconds = len(audio) / (16000 * 4)  # float32 mono 16 kHz
        self.assertGreater(seconds, 1.5)


if __name__ == "__main__":
    unittest.main()
