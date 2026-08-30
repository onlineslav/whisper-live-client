r"""Brings the WhisperLive server up, and reports honestly on how far it got.

WhisperBoard launches itself at login; the server it talks to does not. The
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
STATE_STARTING = "starting"          # docker run / docker start
STATE_RUNNING = "running"            # something is listening on the port
STATE_FAILED = "failed"              # gave up; detail says why

CONTAINER_NAME = "whisperboard-server"

# Whisper weights are downloaded on first use of each model and cached here
# inside the container. Held in a named volume because the alternative is
# re-downloading gigabytes every time the container is recreated -- and the
# container *is* recreated whenever the image or port changes, or the GPU
# image falls back to the CPU one. Switching models in Settings costs one
# download instead of one per container lifetime.
MODEL_CACHE_VOLUME = "whisperboard-models"
MODEL_CACHE_PATH = "/root/.cache/huggingface"
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
    supposed to be here. Pointed at someone else's box, WhisperBoard has no
    business launching anything.
    """
    host, _ = parse_address(address)
    return host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")


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
            except OSError:
                # It accepted the connection, which is the question asked.
                pass
            return True
    except OSError:
        return False


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

    def __init__(self, settings: dict):
        super().__init__()
        self.logger = logging.getLogger("whisperboard.server")
        self.settings = settings
        self.state = STATE_DISABLED
        self.detail = ""
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pull_process = None

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

    def stop_server(self):
        """Stop the container WhisperBoard started. Non-blocking."""
        self._run_async(self._stop_blocking, "server-stop")

    def shutdown(self):
        """Abandon any in-flight Docker work; called on app exit."""
        self._stop_event.set()
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
        if is_port_open(host, port):
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

    def _container_field(self, template: str):
        ok, out, _ = self._docker("inspect", "-f", template, CONTAINER_NAME, timeout=20)
        return out if ok else None

    def _ensure_container(self, host_port: int) -> bool:
        running = self._container_field("{{.State.Running}}")
        if running is not None:
            # A container by our name exists. Reuse it only if it still matches
            # what the settings ask for -- an image or port change here means
            # the old one would silently serve the wrong thing.
            image = self._container_field("{{.Config.Image}}") or ""
            ports = self._container_field(
                "{{range $p, $conf := .NetworkSettings.Ports}}"
                "{{if $conf}}{{(index $conf 0).HostPort}} {{end}}{{end}}") or ""
            matches = image in self._acceptable_images() and str(host_port) in ports.split()
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
        return self._run_container(host_port)

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

    def _run_container(self, host_port: int, use_gpu=None, image=None) -> bool:
        use_gpu = self.settings.get("server_use_gpu", False) if use_gpu is None else use_gpu
        image = image or self.image
        self._emit(STATE_STARTING, "Starting the WhisperLive container"
                   + (" (GPU)" if use_gpu else ""))
        args = ["run", "-d", "--name", CONTAINER_NAME,
                # So a machine that reboots into Docker Desktop brings the
                # server back without WhisperBoard having to ask.
                "--restart", "unless-stopped",
                "-v", f"{MODEL_CACHE_VOLUME}:{MODEL_CACHE_PATH}",
                "-p", f"{host_port}:{CONTAINER_PORT}"]
        if use_gpu:
            args += ["--gpus", "all"]
        args.append(image)

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
            return self._run_container(host_port, use_gpu=False, image=IMAGE_CPU)
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
