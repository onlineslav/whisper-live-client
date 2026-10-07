"""Runs inside the WhisperLive container, in front of the stock run_server.py.

server_manager hands this file's source to the container as `python -c`, with
run_server.py's own arguments after it. It hardens the server, then runs
run_server.py exactly as the image would have.

Why it exists: on 2026-10-06 the server went on accepting connections and
answering SERVER_READY, but no connection transcribed another word until the
container was restarted -- twelve hours later. In single-model mode every
connection's transcription thread takes one class-wide lock around the model
(ServeClientFasterWhisper.SINGLE_MODEL_LOCK), and the stock code neither
releases it on an exception nor notices a holder that never comes back. One
thread stuck holding it stalls every connection after it, silently.

So this:
  * releases the lock if a transcription raises while holding it;
  * watches how long it is held, and once that is far past anything a real
    transcription takes, dumps every thread's stack to the log and exits.
    The container runs with `--restart unless-stopped`, so Docker brings a
    fresh server straight back; the stacks say what hung, next time it does;
  * dumps every thread's stack on SIGUSR1, so the app can ask a server it
    suspects for the same evidence before restarting it;
  * stalls on purpose on SIGUSR2, to rehearse all of the above.

Stdlib only, and nothing here may stop the server starting: if WhisperLive's
internals have moved, the patch is skipped with a message and the server runs
stock.
"""

import faulthandler
import os
import runpy
import signal
import sys
import threading
import time

# Seconds the model lock may be held before the server is taken to be hung.
# The lock covers one call to faster-whisper's transcribe(): VAD and feature
# extraction over at most ~30s of audio, well under a second on the GPU and a
# few on the CPU build. Twenty is an order of magnitude past that.
STALL_S = 20.0


def _say(message: str):
    # Straight to stderr: logging is not configured until run_server.py has
    # imported the server, and these lines must reach `docker logs` regardless.
    print(f"[whispertype] {message}", file=sys.stderr, flush=True)


class OwnedLock:
    """threading.Lock that knows which thread holds it, and since when."""

    def __init__(self):
        self._lock = threading.Lock()
        self.owner = None
        self.since = 0.0

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self.owner = threading.get_ident()
            self.since = time.monotonic()
        return acquired

    def release(self):
        self.owner = None
        self._lock.release()

    def release_if_owned(self) -> bool:
        """Release it if the calling thread holds it. True if it did."""
        if self.owner != threading.get_ident():
            return False
        self.release()
        return True

    def locked(self) -> bool:
        return self._lock.locked()

    def held_for(self) -> float:
        """Seconds the current holder has had it; 0 when free."""
        if self.owner is None:
            return 0.0
        return time.monotonic() - self.since

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def harden(client_class) -> OwnedLock:
    """Swap in an OwnedLock and make transcribe_audio always give it back.

    The wrapper does not depend on what transcribe_audio does inside: if the
    calling thread still holds the lock when the original returns or raises,
    the original failed between acquire and release, and it is released here.
    """
    lock = OwnedLock()
    client_class.SINGLE_MODEL_LOCK = lock
    original = client_class.transcribe_audio

    def transcribe_audio(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        finally:
            if lock.release_if_owned():
                _say("Released the model lock after a failed transcription.")

    client_class.transcribe_audio = transcribe_audio
    return lock


def watch(lock: OwnedLock, stall_s: float = STALL_S, exit_process=os._exit,
          poll_s: float = 1.0):
    """Exit the server once the lock has been held for longer than `stall_s`.

    os._exit rather than sys.exit: the threads that would have to unwind for
    a clean exit are the ones that are stuck.
    """
    while True:
        time.sleep(poll_s)
        held = lock.held_for()
        if held > stall_s:
            _say(f"The model lock has been held for {held:.0f}s; the server is hung. "
                 "Thread stacks follow. Exiting so Docker restarts the container.")
            faulthandler.dump_traceback(all_threads=True)
            exit_process(3)
            return


def rehearse_stall_on(sig, lock: OwnedLock):
    """On `sig`, take the model lock and never give it back.

    The 2026-10-06 stall, on demand, so the defences against it can be seen
    to work: `docker kill --signal SIGUSR2 whispertype-server`.
    """
    def handler(signum, frame):
        _say("Rehearsing a stall: the model lock is taken and will not be released.")
        threading.Thread(target=lock.acquire, name="whispertype-rehearsed-stall",
                         daemon=True).start()
    signal.signal(sig, handler)


def main():
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    try:
        from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper
        lock = harden(ServeClientFasterWhisper)
        threading.Thread(target=watch, args=(lock,), name="whispertype-stall-watch",
                         daemon=True).start()
        rehearse_stall_on(signal.SIGUSR2, lock)
        _say(f"Server patch applied: model lock hardened, stall watch at {STALL_S:.0f}s.")
    except Exception as e:  # noqa: BLE001 -- the server must start regardless
        _say(f"Server patch NOT applied; running stock: {e!r}")
    sys.argv = ["run_server.py"] + sys.argv[1:]
    runpy.run_path("run_server.py", run_name="__main__")


if __name__ == "__main__":
    main()
