r"""Brings the WhisperLive server up, and reports honestly on how far it got.

WhisperType launches itself at login; the server it talks to does not. The
result was an app that sat in the tray looking ready while nothing was
listening on port 9090, and a capture box stuck on "Connecting..." forever.

This module owns the other half: it checks whether something is already
serving, starts Docker Desktop if the daemon is down, pulls the image if it is
missing, and runs (or restarts) the container -- emitting a state and a
human-readable detail at every step, so the tray can show what is actually
happening instead of a spinner.

Everything here shells out to the `docker` CLI on a worker thread. The Docker
HTTP API over a named pipe would avoid the subprocess overhead, but the CLI is
the interface the README already documents and the one the user can reproduce
by hand when something goes wrong.

Only local servers are managed. If `server_address` points at another machine,
this stays out of the way entirely -- see `is_local_address`.
"""

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal

# States, coarsest first. `detail` carries the specifics for the tooltip.
STATE_DISABLED = "disabled"          # not managing this server at all
STATE_CHECKING = "checking"          # probing the port / asking Docker
STATE_STARTING_DOCKER = "docker"     # waiting for the Docker daemon
STATE_PULLING = "pulling"            # downloading the server image
STATE_FETCHING_MODEL = "model"      # downloading Whisper weights into the cache
STATE_STARTING = "starting"          # docker run / docker start
STATE_RUNNING = "running"            # something is listening on the port
STATE_FAILED = "failed"              # gave up; detail says why
STATE_PAUSED = "paused"              # stopped on purpose, to free the GPU

# Docker object names. They carried the pre-rename "whisperboard-" prefix for
# a while, because renaming them naively orphans a running container -- which
# keeps port 9090 and is restarted by Docker on every boot -- and abandons a
# model cache holding gigabytes of downloaded weights.
#
# So the rename is done, and each hazard is handled where it lives: the stale
# container is removed on the first start under the new name (see
# _retire_legacy_objects), and the cache volume, which cannot be renamed at
# all, is adopted where it already exists rather than replaced (see
# _model_cache_volume). A machine that has never run the old name never sees
# either path.
CONTAINER_NAME = "whispertype-server"
LEGACY_CONTAINER_NAME = "whisperboard-server"

# Whisper weights are downloaded on first use of each model and cached here
# inside the container. Held in a named volume because the alternative is
# re-downloading gigabytes every time the container is recreated -- and the
# container *is* recreated whenever the image or port changes, or the GPU
# image falls back to the CPU one. Switching models in Settings costs one
# download instead of one per container lifetime.
MODEL_CACHE_VOLUME = "whispertype-models"
LEGACY_MODEL_CACHE_VOLUME = "whisperboard-models"
MODEL_CACHE_PATH = "/root/.cache/huggingface"

# WhisperLive loads a *separate* model for every client connection, and it
# closes the socket after each END_OF_AUDIO -- so one dictation is one model
# instance. Cleanup is only `self.exit = True` and the reclaim lags behind the
# next connection's load, which measured 7.1 GB of resident models on an 8 GB
# card after normal use with distil-large-v3. At that point the GPU is full,
# clocks drop, and every model gets slower: tiny.en went from 1.27s to 5.03s
# to first word.
#
# The server has a single-model mode that loads one model and shares it across
# connections, but it is gated behind a custom model *path* -- naming a model
# in the handshake never reaches it. A downloaded model's snapshot directory
# is exactly such a path, so resolving it and passing -fw unlocks the mode,
# which removes both the per-connection load and the accumulation.
#
# Because -fw fixes the model for the life of the container, the model is part
# of the container's identity: changing it in Settings recreates the container.
# How often to look for an app we should get out of the way of. Slow on
# purpose: this decides whether to stop a container, and reacting a few seconds
# later than a game's splash screen costs nothing.
APP_POLL_S = 20.0

MODEL_FETCH_CONTAINER = "whispertype-modelfetch"
LEGACY_MODEL_FETCH_CONTAINER = "whisperboard-modelfetch"
SERVER_BASE_COMMAND = ["python", "run_server.py"]
# Model names we will interpolate into a python -c inside the container.
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._/-]+$")
IMAGE_CPU = "ghcr.io/collabora/whisperlive-cpu:latest"
IMAGE_GPU = "ghcr.io/collabora/whisperlive-gpu:latest"

# The port WhisperLive listens on inside the container. The host-side port
# comes from the configured server address, so changing the address to :9091
# remaps the publish rather than breaking the connection.
CONTAINER_PORT = 9090

DOCKER_DESKTOP_PATHS = (
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    r"C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe",
)
DOCKER_CLI_FALLBACKS = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
)

# Docker Desktop starts a VM; on a cold boot with a spinning disk this is not
# quick. Two minutes is long enough to succeed and short enough that a genuine
# failure is still reported while the user is at the machine.
DOCKER_DAEMON_TIMEOUT_S = 150
# After `docker run` the container still has to bind the port. This covers the
# bind only -- the model load happens after the socket is accepting, and is
# reported separately by the WebSocket client via SERVER_READY.
PORT_BIND_TIMEOUT_S = 90
PROBE_TIMEOUT_S = 0.6

# How often to ask the container how far a model download has got. Each check
# is a `docker exec`, so this is deliberately not once a second -- but a model
# download is minutes long, and a progress figure that only moves every couple
# of seconds is still the difference between "working" and "hung".
DOWNLOAD_POLL_S = 2.0

# HTTP line ending, spelled by code point to keep it unambiguous.
CRLF = chr(13) + chr(10)

# Suppresses the console window each `docker` call would otherwise flash on a
# windowed (console=False) build.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# "a1b2c3d4: Downloading [====>    ]  50.5MB/500MB" -- Docker's non-TTY pull
# output. Sizes carry a unit that changes per layer, so both are normalised to
# bytes before they are summed.
_PULL_LINE = re.compile(
    r"^([0-9a-f]+):\s+(Downloading|Extracting)\s+\[[^\]]*\]\s+"
    r"([\d.]+)\s*([kKMGT]?B)/([\d.]+)\s*([kKMGT]?B)"
)
_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def _to_bytes(value: str, unit: str) -> float:
    try:
        return float(value) * _UNITS.get(unit.upper(), 1)
    except ValueError:
        return 0.0


def parse_address(address: str):
    """Split a ws:// address into (host, port). Falls back to localhost:9090."""
    try:
        parsed = urlparse(address)
        host = parsed.hostname or "localhost"
        port = parsed.port or CONTAINER_PORT
        return host, int(port)
    except (ValueError, AttributeError):
        return "localhost", CONTAINER_PORT


def is_local_address(address: str) -> bool:
    """True if the address names this machine.

    Starting a container is only ever the right answer for a server that is
    supposed to be here. Pointed at someone else's box, WhisperType has no
    business launching anything.
    """
    host, _ = parse_address(address)
    return host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")


def running_processes() -> set:
    """Lower-cased names of the processes running right now.

    `tasklist` rather than psutil, which is not a dependency of this project,
    and rather than EnumProcesses, which would need a ctypes dance to get the
    same strings. One call every twenty seconds is not worth either.
    """
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if completed.returncode != 0:
        return set()
    names = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        # "name.exe","1234","Console","1","12,345 K"
        names.add(line.split('","', 1)[0].lstrip('"').lower())
    return names


def parse_app_list(raw: str) -> list:
    """Split the user's comma/newline separated app list into process names."""
    parts = []
    for chunk in str(raw or "").replace("\n", ",").split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        # Accept "Game" as readily as "Game.exe" -- nobody thinks of their
        # games by their file extension.
        if not name.endswith(".exe"):
            name += ".exe"
        parts.append(name)
    return parts


def detect_nvidia_gpu() -> bool:
    """True if this machine has an NVIDIA GPU the GPU image could use.

    Asked once, on first run, so the image choice matches the hardware instead
    of defaulting everyone to the CPU build. `nvidia-smi` ships with the
    driver, so its presence and a successful device listing is a good proxy;
    whether Docker itself can pass the GPU through is only knowable by trying,
    which `_run_container` does -- falling back to the CPU image if it cannot.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        completed = subprocess.run(
            [exe, "-L"], capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0 and "GPU" in completed.stdout


def is_port_open(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True if something accepts a connection there right now.

    A minimal HTTP request is sent rather than connecting and hanging up
    immediately. WhisperLive's websockets server logs a full traceback for a
    connection that closes before sending a request line -- 27 lines of it,
    measured -- and a silent health check that fills the server's log is not a
    health check worth having. A request line halves that, and the "426
    Upgrade Required" that comes back is the better signal anyway: it proves a
    WebSocket server is there, not merely that the port is bound.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            try:
                request = CRLF.join(["GET / HTTP/1.1", "Host: " + host, "", ""])
                sock.sendall(request.encode("ascii", "ignore"))
                # The reply is what makes this a readiness check rather than a
                # liveness one. A freshly started container binds the port
                # before its Python process is serving, so a bare connect
                # succeeds against a server that will refuse the next
                # WebSocket handshake -- which is exactly the race a caller
                # hits right after the container is recreated.
                sock.settimeout(timeout)
                sock.recv(64)
            except socket.timeout:
                # Accepted, but nothing is answering yet.
                return False
            except OSError:
                # Closed on us without a reply: something is there and
                # handling connections, which is the question asked.
                return True
            return True
    except OSError:
        return False


def _format_bytes(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.0f} MB"


class ServerManager(QObject):
    """Starts and reports on the local WhisperLive container.

    All Docker work happens on a single worker thread -- `ensure_running()` and
    `stop_server()` return immediately and report through `state_changed`.
    Only one operation runs at a time; a request arriving while the worker is
    busy is dropped rather than queued, because both operations are idempotent
    and a queue would only let a stale one land after the user changed course.
    """

    # (state, detail). `detail` is written to be shown to the user verbatim.
    state_changed = Signal(str, str)
    # Progress of the model the server is currently fetching, ready to show as
    # it is; empty string when there is nothing being downloaded.
    model_progress = Signal(str)

    def __init__(self, settings: dict):
        super().__init__()
        self.logger = logging.getLogger("whispertype.server")
        self.settings = settings
        self.state = STATE_DISABLED
        self.detail = ""
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pull_process = None
        # The model-download watcher runs on its own thread rather than the
        # shared worker, so watching progress can never block a start or stop.
        self._watch_thread = None
        self._watch_stop = threading.Event()
        # Name of the app we have stood down for, if any, and a flag set when
        # the user starts the server by hand anyway -- an explicit instruction
        # outranks the heuristic, and without it the watcher would simply undo
        # the user's click on the next poll.
        self._paused_for = None
        self._yield_override = False
        self._app_thread = None
        self._app_stop = threading.Event()
        # Both pre-rename lookups are asked of Docker once per run and then
        # remembered: neither answer changes while we are the one changing it.
        self._legacy_retired = False
        self._cache_volume = None

    # -- configuration -----------------------------------------------------

    def update_settings(self, settings: dict):
        self.settings = settings

    @property
    def address(self) -> str:
        return self.settings.get("server_address", "ws://localhost:9090")

    @property
    def image(self) -> str:
        # An explicit image in config.json wins, so an unusual build or a
        # pinned tag can be used without a code change.
        configured = (self.settings.get("server_docker_image") or "").strip()
        if configured:
            return configured
        return IMAGE_GPU if self.settings.get("server_use_gpu", False) else IMAGE_CPU

    def can_manage_server(self) -> bool:
        """Whether starting a server here would even make sense.

        Separate from `manages_server` so that turning auto-start off disables
        the automatic behaviour without also removing the ability to start the
        server by hand from the tray -- which is exactly what someone who
        turned auto-start off is most likely to want.
        """
        return is_local_address(self.address)

    def manages_server(self) -> bool:
        return bool(self.settings.get("auto_start_server", True)) and self.can_manage_server()

    # -- public API --------------------------------------------------------

    def ensure_running(self, force: bool = False):
        """Start whatever is needed to get the server listening. Non-blocking.

        `force` is the user asking directly (the tray's Start Server), which
        overrides the auto-start setting but never the remote-address rule.
        """
        if not self.can_manage_server():
            self._emit(STATE_DISABLED, f"Using remote server {self.address}")
            return
        if not force and not self.manages_server():
            self._emit(STATE_DISABLED, "Automatic server startup is off")
            return
        if force:
            # The user asked directly; stop standing down until whatever we
            # stood down for has gone away.
            self._yield_override = True
            self._paused_for = None
        elif self._paused_for:
            self._emit(STATE_PAUSED,
                       f"GPU released while {self._paused_for} is running")
            return
        self._run_async(self._ensure_running_blocking, "server-start")

    def probe(self):
        """Check whether the server is listening, without starting anything.

        Used for the idle poll: in on-demand mode nothing holds a socket open,
        so this is the only thing keeping the tray icon honest between
        captures.
        """
        if not is_local_address(self.address):
            return
        self._run_async(self._probe_blocking, "server-probe")

    def start_download_watch(self):
        """Begin reporting how far the server has got fetching its model.

        WhisperLive downloads the model inside the container on first use of
        each one and says nothing about it over the WebSocket -- so a switch to
        a large model looks identical to a hang for as long as ten minutes.
        The bytes are visible from outside, though: huggingface_hub writes to a
        sparse `.incomplete` blob preallocated to the finished size, so the
        file's apparent size is the total and its allocated blocks are what has
        actually arrived.
        """
        if not self.can_manage_server():
            return  # a remote server's filesystem is not ours to inspect
        with self._lock:
            if self._watch_thread and self._watch_thread.is_alive():
                return
            self._watch_stop.clear()
            self._watch_thread = threading.Thread(
                target=self._watch_download, name="model-download-watch", daemon=True)
            self._watch_thread.start()

    def stop_download_watch(self):
        self._watch_stop.set()

    # -- standing down for other GPU work ---------------------------------

    def yield_apps(self) -> list:
        return parse_app_list(self.settings.get("vram_yield_apps", ""))

    def start_app_watch(self):
        """Watch for apps we should free the GPU for."""
        with self._lock:
            if self._app_thread and self._app_thread.is_alive():
                return
            self._app_stop.clear()
            self._app_thread = threading.Thread(
                target=self._watch_apps, name="vram-yield-watch", daemon=True)
            self._app_thread.start()

    def stop_app_watch(self):
        self._app_stop.set()

    def _find_yield_app(self):
        """The first configured app that is running, or None."""
        wanted = self.yield_apps()
        if not wanted:
            return None
        running = running_processes()
        for name in wanted:
            if name in running:
                return name
        return None

    def _watch_apps(self):
        while not self._app_stop.is_set():
            try:
                self._apply_yield_state()
            except Exception:
                self.logger.exception("VRAM yield check failed.")
            self._app_stop.wait(APP_POLL_S)

    def _apply_yield_state(self):
        found = self._find_yield_app()

        if found is None:
            # Nothing to defer to. A manual override only lasts as long as the
            # app that provoked it, so it is cleared here rather than left to
            # suppress the next game too.
            self._yield_override = False
            if self._paused_for:
                released, self._paused_for = self._paused_for, None
                self.logger.info("%s exited; bringing the server back.", released)
                if self.manages_server():
                    self._run_async(self._ensure_running_blocking, "server-resume")
            return

        if self._yield_override or not self.can_manage_server():
            return
        if self._paused_for == found:
            return  # already stood down for this one

        self._paused_for = found
        self.logger.info("%s is running; stopping the server to free the GPU.", found)
        self._run_async(lambda: self._pause_blocking(found), "server-pause")

    def _pause_blocking(self, app: str):
        self._docker("stop", CONTAINER_NAME, timeout=60)
        self._emit(STATE_PAUSED, f"GPU released while {app} is running")

    def _download_bytes(self, container: str = CONTAINER_NAME):
        """(downloaded, total) across in-progress model downloads, or None."""
        # %s is the apparent size -- preallocated to the finished length -- and
        # %b counts the 512-byte blocks actually committed to disk.
        # The -exec terminator is quoted: passed through `sh -c` unquoted, the
        # shell eats the bare `;` as a command separator and find fails with
        # "missing argument to -exec".
        script = ('find /root/.cache/huggingface -name "*.incomplete" '
                  '-exec stat -c "%s %b" {} ";"')
        ok, out, _ = self._docker("exec", container, "sh", "-c", script, timeout=15)
        if not ok or not out:
            return None
        done = total = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                total += int(parts[0])
                done += int(parts[1]) * 512
            except ValueError:
                continue
        if total <= 0:
            return None
        # Allocated blocks can nudge past the apparent size on the final write.
        return min(done, total), total

    def _watch_download(self):
        last = None
        while not self._watch_stop.is_set():
            sample = self._download_bytes()
            if sample is None:
                # Either nothing is downloading (the model was already cached
                # and this is a plain load) or the file has just been renamed
                # into place. Either way there is no progress to report.
                self.model_progress.emit("")
                last = None
            else:
                done, total = sample
                now = time.monotonic()
                text = f"{_format_bytes(done)} of {_format_bytes(total)} ({done / total:.0%})"
                if last and now > last[0] and done > last[1]:
                    rate = (done - last[1]) / (now - last[0])
                    remaining = (total - done) / rate if rate > 0 else 0
                    if remaining >= 90:
                        text += f" — about {remaining / 60:.0f} min left"
                    elif remaining > 0:
                        text += f" — about {remaining:.0f}s left"
                last = (now, done)
                self.model_progress.emit(text)
            self._watch_stop.wait(DOWNLOAD_POLL_S)
        self.model_progress.emit("")

    def stop_server(self):
        """Stop the container WhisperType started. Non-blocking."""
        self._run_async(self._stop_blocking, "server-stop")

    def shutdown(self):
        """Abandon any in-flight Docker work; called on app exit."""
        self._stop_event.set()
        self._watch_stop.set()
        self._app_stop.set()
        process = self._pull_process
        if process and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
            if thread.is_alive():
                self.logger.warning("Server manager thread did not exit within timeout.")

    def is_busy(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    # -- worker plumbing ---------------------------------------------------

    def _run_async(self, target, name):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.logger.debug("Server manager busy; dropping %s request.", name)
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._guard(target), name=name, daemon=True)
            self._thread.start()

    def _guard(self, target):
        def wrapper():
            try:
                target()
            except Exception:
                self.logger.exception("Server manager task failed.")
                self._emit(STATE_FAILED, "Server startup failed — see the log")
        return wrapper

    def _emit(self, state: str, detail: str = ""):
        # Repeats are dropped: the pull emits on every progress line, and the
        # tray would otherwise redraw its icon dozens of times a second.
        if state == self.state and detail == self.detail:
            return
        self.state = state
        self.detail = detail
        self.logger.info("Server state: %s (%s)", state, detail)
        self.state_changed.emit(state, detail)

    def _cancelled(self) -> bool:
        return self._stop_event.is_set()

    # -- docker ------------------------------------------------------------

    def _docker_exe(self):
        found = shutil.which("docker")
        if found:
            return found
        for candidate in DOCKER_CLI_FALLBACKS:
            if os.path.exists(candidate):
                return candidate
        return None

    def _docker(self, *args, timeout=30):
        """Run a docker command. Returns (ok, stdout, stderr)."""
        exe = self._docker_exe()
        if not exe:
            return False, "", "docker-not-found"
        try:
            completed = subprocess.run(
                [exe, *args],
                capture_output=True, text=True, timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return False, "", "timeout"
        except OSError as e:
            return False, "", str(e)
        return completed.returncode == 0, completed.stdout.strip(), completed.stderr.strip()

    def _daemon_ready(self) -> bool:
        ok, _, _ = self._docker("version", "--format", "{{.Server.Version}}", timeout=15)
        return ok

    # -- the actual sequence ----------------------------------------------

    def _container_is_running(self):
        """Ask Docker, not the network. None if there is no such container."""
        running = self._container_field("{{.State.Running}}")
        if running is None:
            return None
        return running == "true"

    def _probe_blocking(self):
        if self._paused_for:
            return  # stopped on purpose; not a fault to report
        host, port = parse_address(self.address)

        # For a container we own, Docker is the quieter authority: the idle
        # poll runs for as long as the app does, and every network probe costs
        # the server a logged traceback. Falls back to the network probe when
        # there is no such container -- including a server someone started by
        # hand, which is exactly the case Docker cannot answer for.
        alive = None
        if self.manages_server() or self.can_manage_server():
            alive = self._container_is_running()
        if alive is None:
            alive = is_port_open(host, port)

        if alive:
            self._emit(STATE_RUNNING, f"Listening on {host}:{port}")
        elif self.state in (STATE_RUNNING, STATE_DISABLED):
            # Only downgrade from a settled state. Mid-startup the port is
            # legitimately closed, and saying "not running" over the top of
            # "Downloading image 40%" would be worse than saying nothing.
            self._emit(STATE_FAILED, "Server is not running")

    def _ensure_running_blocking(self):
        host, port = parse_address(self.address)

        self._emit(STATE_CHECKING, "Checking for a running server")

        # A listening port is only proof enough when the server is not ours to
        # configure. When it is, the container still has to be checked against
        # what the settings now ask for -- a model change alters the command it
        # must run, and short-circuiting here on "something answered" is how a
        # configuration change silently never takes effect.
        # A pre-rename container is still ours, even though it no longer
        # answers to our name -- and it is the thing holding the port. Retiring
        # it here, before the check below, is what stops "something is
        # listening" being read as somebody else's server for the rest of the
        # run: the settings would then never reach the container, and Stop
        # Server would have nothing to stop. A no-op once there is nothing
        # left under the old name, and while the daemon is still coming up.
        if self._docker_exe():
            self._retire_legacy_objects()

        ours = self._docker_exe() and self._container_field("{{.State.Running}}") is not None
        if is_port_open(host, port) and not ours:
            # Worded identically to the probe's and _await_port's success, so
            # the idle poll does not keep rewriting a settled tooltip with a
            # synonym of what it already says.
            self._emit(STATE_RUNNING, f"Listening on {host}:{port}")
            return

        if not self._docker_exe():
            self._emit(STATE_FAILED,
                       "Docker is not installed — start the WhisperLive server yourself, "
                       "or turn off automatic startup in Settings")
            return

        if not self._ensure_daemon():
            return
        if self._cancelled():
            return
        if not self._ensure_container(port):
            return
        if self._cancelled():
            return
        self._await_port(host, port)

    def _ensure_daemon(self) -> bool:
        if self._daemon_ready():
            return True

        if not self.settings.get("start_docker_desktop", True):
            self._emit(STATE_FAILED, "Docker Desktop is not running — start it, then retry")
            return False

        desktop = next((p for p in DOCKER_DESKTOP_PATHS if os.path.exists(p)), None)
        if not desktop:
            self._emit(STATE_FAILED, "Docker Desktop was not found — start the Docker daemon yourself")
            return False

        self._emit(STATE_STARTING_DOCKER, "Starting Docker Desktop")
        try:
            subprocess.Popen([desktop], creationflags=_NO_WINDOW | subprocess.DETACHED_PROCESS)
        except OSError as e:
            self._emit(STATE_FAILED, f"Could not launch Docker Desktop: {e}")
            return False

        deadline = time.monotonic() + DOCKER_DAEMON_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._cancelled():
                return False
            # Reported as elapsed rather than remaining: a countdown implies
            # the wait is bounded by something meaningful, and it is not --
            # the daemon comes up when the VM does.
            waited = int(DOCKER_DAEMON_TIMEOUT_S - (deadline - time.monotonic()))
            self._emit(STATE_STARTING_DOCKER, f"Waiting for the Docker daemon ({waited}s)")
            if self._daemon_ready():
                return True
            self._stop_event.wait(2.0)

        self._emit(STATE_FAILED, "Docker Desktop did not finish starting")
        return False

    def _retire_legacy_objects(self):
        """Remove the pre-rename containers, once, before we make our own.

        The server container was named "whisperboard-server" until the rename
        to WhisperType. It publishes the host port and is created with
        --restart unless-stopped, so left alone it is still listening when the
        new container is created -- and the new one then fails with the port
        already allocated, on a machine where dictation had been working.

        A container is safe to remove because it holds nothing: the weights
        live in the cache volume, which is adopted rather than deleted. The
        throwaway fetch container goes too, in case a crashed run left one.
        """
        if self._legacy_retired:
            return
        if not self._daemon_ready():
            # A failed inspect with the daemon down means "cannot tell", not
            # "not there". Remembering that as done would leave the old
            # container in place for the rest of the run.
            return
        self._legacy_retired = True
        for name in (LEGACY_CONTAINER_NAME, LEGACY_MODEL_FETCH_CONTAINER):
            ok, _, _ = self._docker("inspect", "-f", "{{.Id}}", name, timeout=20)
            if not ok:
                continue
            self.logger.info("Removing the pre-rename container %s.", name)
            self._docker("rm", "-f", name, timeout=60)

    def _model_cache_volume(self) -> str:
        """The named volume the downloaded weights are cached in.

        A fresh machine gets "whispertype-models". One that already cached
        gigabytes under the pre-rename "whisperboard-models" keeps using it:
        Docker cannot rename a volume, and the alternatives are copying the
        cache or re-downloading every model the user already has. Adopting it
        costs nothing and is invisible.
        """
        if self._cache_volume is None:
            if not self._daemon_ready():
                # Undecided rather than wrong: answering "the new one" because
                # Docker was unreachable would mount a second, empty cache
                # beside the full one and re-download every model.
                return MODEL_CACHE_VOLUME
            ok, _, _ = self._docker(
                "volume", "inspect", LEGACY_MODEL_CACHE_VOLUME, timeout=20)
            self._cache_volume = LEGACY_MODEL_CACHE_VOLUME if ok else MODEL_CACHE_VOLUME
            if ok:
                self.logger.info("Using the pre-rename model cache volume %s.",
                                 LEGACY_MODEL_CACHE_VOLUME)
        return self._cache_volume

    def _container_field(self, template: str):
        ok, out, _ = self._docker("inspect", "-f", template, CONTAINER_NAME, timeout=20)
        return out if ok else None

    def _ensure_container(self, host_port: int) -> bool:
        # Before our own container is looked for: a leftover under the old
        # name still holds the port, and would fail the run below.
        self._retire_legacy_objects()

        # Resolved before anything is torn down, so a model that still has to
        # be downloaded is fetched while the old container is up and its
        # progress is visible.
        model_path = None
        if self.settings.get("share_one_model", True):
            model_path = self._resolve_model_path(self.settings.get("model", ""))
        desired_command = self._server_command(model_path)

        running = self._container_field("{{.State.Running}}")
        if running is not None:
            # A container by our name exists. Reuse it only if it still matches
            # what the settings ask for -- an image or port change here means
            # the old one would silently serve the wrong thing.
            image = self._container_field("{{.Config.Image}}") or ""
            ports = self._container_field(
                "{{range $p, $conf := .NetworkSettings.Ports}}"
                "{{if $conf}}{{(index $conf 0).HostPort}} {{end}}{{end}}") or ""
            command = self._container_field('{{join .Config.Cmd " "}}') or ""
            # The command carries the model, so a model change shows up here as
            # a mismatch and the container is rebuilt around the new one.
            matches = (image in self._acceptable_images()
                       and str(host_port) in ports.split()
                       and command.split() == desired_command)
            if matches and running == "true":
                return True
            if matches:
                self._emit(STATE_STARTING, "Starting the WhisperLive container")
                ok, _, err = self._docker("start", CONTAINER_NAME, timeout=60)
                if ok:
                    return True
                self.logger.warning("docker start failed: %s", err)
            self._emit(STATE_STARTING, "Recreating the WhisperLive container")
            self._docker("rm", "-f", CONTAINER_NAME, timeout=60)

        if not self._ensure_image():
            return False
        if self._cancelled():
            return False
        return self._run_container(host_port, command=desired_command)

    def _server_command(self, model_path=None) -> list:
        """The command the container should run.

        Stated in full because Docker replaces the image's CMD outright when
        arguments follow the image name rather than appending to it.
        """
        command = SERVER_BASE_COMMAND + ["--port", str(CONTAINER_PORT)]
        if model_path:
            command += ["-fw", model_path]
        return command

    def _resolve_model_path(self, model: str):
        """Local path of `model` inside the cache volume, downloading it first.

        Runs faster-whisper's own downloader in a throwaway container against
        the shared cache volume, so the model-name to repository mapping is
        the server's rather than a guess of ours. Returns None if it cannot be
        resolved, which leaves the server in its ordinary per-connection mode
        instead of failing to start.
        """
        if not model or not _SAFE_MODEL.match(model):
            self.logger.warning("Model name %r is not one we will pass to the container.", model)
            return None

        self._docker("rm", "-f", MODEL_FETCH_CONTAINER, timeout=60)
        snippet = ("from faster_whisper.utils import download_model; "
                   "print(download_model('%s'))" % model)
        # Checking rather than downloading: with the model already cached --
        # every launch but the first -- this finishes in seconds having
        # fetched nothing. The state only turns into a download once there
        # are bytes arriving to show for it, below.
        self._emit(STATE_CHECKING, f"Checking the {model} model")
        ok, _, err = self._docker(
            "run", "-d", "--name", MODEL_FETCH_CONTAINER,
            "-v", f"{self._model_cache_volume()}:{MODEL_CACHE_PATH}",
            self.image, "python", "-c", snippet, timeout=120)
        if not ok:
            self.logger.warning("Could not start the model fetch container: %s", err)
            return None

        try:
            # Report download progress off the same sparse-file trick used for
            # a load, but against the fetch container.
            while not self._cancelled():
                running = self._docker(
                    "inspect", "-f", "{{.State.Running}}", MODEL_FETCH_CONTAINER,
                    timeout=20)[1]
                sample = self._download_bytes(MODEL_FETCH_CONTAINER)
                if sample:
                    done, total = sample
                    self._emit(STATE_FETCHING_MODEL,
                               f"Downloading {model} — {_format_bytes(done)} of "
                               f"{_format_bytes(total)} ({done / total:.0%})")
                if running != "true":
                    break
                self._stop_event.wait(DOWNLOAD_POLL_S)

            ok, out, _ = self._docker("logs", MODEL_FETCH_CONTAINER, timeout=30)
            path = None
            for line in (out or "").splitlines():
                line = line.strip()
                if line.startswith("/"):
                    path = line
            if not path:
                self.logger.warning("Model fetch produced no path for %r.", model)
            return path
        finally:
            self._docker("rm", "-f", MODEL_FETCH_CONTAINER, timeout=60)

    def _acceptable_images(self) -> set:
        """Images a existing container may be running and still be reused.

        Includes the CPU build whenever the GPU one is configured: a container
        that already fell back once is still the right container, and treating
        it as a mismatch would tear it down and repeat the failed GPU attempt
        on every single launch.
        """
        images = {self.image}
        if self.settings.get("server_use_gpu", False):
            images.add(IMAGE_CPU)
        return images

    def _ensure_image(self) -> bool:
        return self._ensure_image_named(self.image)

    def _ensure_image_named(self, image: str) -> bool:
        ok, _, _ = self._docker("image", "inspect", image, timeout=30)
        if ok:
            return True
        return self._pull_image(image)

    def _pull_image(self, image: str) -> bool:
        """Pull the server image, reporting download progress as a percentage.

        The image is multi-gigabyte, so on a first run this is where nearly all
        of the wait lives. Showing a percentage is the difference between an
        app that looks hung and one that is visibly working.
        """
        exe = self._docker_exe()
        self._emit(STATE_PULLING, f"Downloading the server image ({image})")
        try:
            process = subprocess.Popen(
                [exe, "pull", image],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=_NO_WINDOW,
            )
        except OSError as e:
            self._emit(STATE_FAILED, f"Could not pull the server image: {e}")
            return False

        self._pull_process = process
        layers = {}
        try:
            for line in process.stdout:
                if self._cancelled():
                    process.kill()
                    return False
                match = _PULL_LINE.match(line.strip())
                if not match:
                    continue
                layer, phase, cur, cur_unit, total, total_unit = match.groups()
                if phase != "Downloading":
                    continue
                layers[layer] = (_to_bytes(cur, cur_unit), _to_bytes(total, total_unit))
                done = sum(c for c, _ in layers.values())
                size = sum(t for _, t in layers.values())
                if size > 0:
                    self._emit(STATE_PULLING,
                               f"Downloading the server image — {done / size:.0%} "
                               f"of {size / 1e9:.1f} GB")
        finally:
            process.stdout.close()
            code = process.wait()
            self._pull_process = None

        if code != 0:
            self._emit(STATE_FAILED, "Downloading the server image failed")
            return False
        return True

    def _run_container(self, host_port: int, use_gpu=None, image=None, command=None) -> bool:
        use_gpu = self.settings.get("server_use_gpu", False) if use_gpu is None else use_gpu
        image = image or self.image
        command = command or self._server_command()
        self._emit(STATE_STARTING, "Starting the WhisperLive container"
                   + (" (GPU)" if use_gpu else ""))
        args = ["run", "-d", "--name", CONTAINER_NAME,
                # So a machine that reboots into Docker Desktop brings the
                # server back without WhisperType having to ask.
                "--restart", "unless-stopped",
                "-v", f"{self._model_cache_volume()}:{MODEL_CACHE_PATH}",
                "-p", f"{host_port}:{CONTAINER_PORT}"]
        if use_gpu:
            args += ["--gpus", "all"]
        args.append(image)
        args += command

        ok, _, err = self._docker(*args, timeout=120)
        if ok:
            return True

        lowered = err.lower()
        if "already allocated" in lowered or "address already in use" in lowered:
            self._emit(STATE_FAILED, f"Port {host_port} is already in use by another program")
            return False
        if use_gpu and ("gpu" in lowered or "nvidia" in lowered or "device" in lowered):
            # The card is there but Docker cannot pass it through -- usually a
            # missing container toolkit. Dictation on the CPU build is far
            # better than no dictation, so fall back rather than stopping, and
            # say so, because the difference is very audible in latency.
            self.logger.warning("GPU container refused (%s); falling back to the CPU image.", err)
            self._docker("rm", "-f", CONTAINER_NAME, timeout=60)
            self._emit(STATE_STARTING, "GPU unavailable to Docker — falling back to the CPU image")
            if not self._ensure_image_named(IMAGE_CPU):
                return False
            return self._run_container(host_port, use_gpu=False, image=IMAGE_CPU,
                                       command=command)
        if "no matching manifest" in lowered or "not found" in lowered:
            self._emit(STATE_FAILED, f"The image {image} could not be found")
            return False
        self._emit(STATE_FAILED,
                   f"Docker could not start the server: "
                   f"{err.splitlines()[0] if err else 'unknown error'}")
        return False

    def _await_port(self, host: str, port: int) -> bool:
        deadline = time.monotonic() + PORT_BIND_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._cancelled():
                return False
            if is_port_open(host, port):
                self._emit(STATE_RUNNING, f"Listening on {host}:{port}")
                return True
            waited = int(PORT_BIND_TIMEOUT_S - (deadline - time.monotonic()))
            self._emit(STATE_STARTING, f"Waiting for the server to accept connections ({waited}s)")
            self._stop_event.wait(1.0)

        self._emit(STATE_FAILED, "The container started but nothing is listening on the port")
        return False

    def _stop_blocking(self):
        self._emit(STATE_CHECKING, "Stopping the WhisperLive container")
        ok, _, err = self._docker("stop", CONTAINER_NAME, timeout=60)
        if ok:
            self._emit(STATE_DISABLED, "Server stopped")
        else:
            self._emit(STATE_FAILED, f"Could not stop the server: {err or 'unknown error'}")
