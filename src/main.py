import copy
import ctypes
import sys
import json
import os
import logging
import time
from datetime import datetime
from PySide6.QtGui import (
    QIcon, QAction, QPixmap, QPainter, QColor, QFont, QPainterPath, QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QMessageBox
from PySide6.QtCore import Qt, QTimer
from settings_window import SettingsWindow, DEFAULT_SETTINGS, APP_DATA_DIR, CONFIG_FILE
from hotkey_listener import HotkeyListener
from websocket_client import WebSocketClient, VAD_PARAMETERS
from audio_capture import AudioCapture
from capture_box import CaptureBox
import win_input
import payload_log
import server_manager
from server_manager import ServerManager
from transcript import Transcript
from final_pass import FinalPass, BYTES_PER_SECOND, MAX_REPLAY_SECONDS, TIMEOUT_MS as FINAL_PASS_TIMEOUT_MS
from win_focus import (
    get_foreground_window, focus_window, is_own_window, is_window,
    get_window_title,
)

HISTORY_FILE = os.path.join(APP_DATA_DIR, "transcription_history.log")

# Milliseconds between confirming and reading the final transcript. Short: the
# server's last segments usually arrive before the box finishes hiding, and
# every millisecond here is felt as lag between pressing Enter and seeing text.
PASTE_START_DELAY_MS = 60

# Milliseconds to wait after the Capture Box hides before restoring focus to
# the target window. Windows needs a moment to finish tearing down the overlay
# and hand the foreground on.
PASTE_FOCUS_DELAY_MS = 120

# Milliseconds between releasing held modifiers and injecting Ctrl+V. The
# target processes the keyups in order, but a chord arriving in the same
# instant as its own activation is dropped by some apps (Chromium especially).
PASTE_KEY_DELAY_MS = 40

# How long to wait for the final pass before pasting the streamed text
# regardless. FinalPass budgets its own round trip and always reports back, so
# this only covers it failing to report at all -- a thread that never returns
# would otherwise swallow the capture silently, which is the one outcome the
# user cannot recover from.
FINAL_PASS_WATCHDOG_MS = FINAL_PASS_TIMEOUT_MS + 2500

# Ceiling on the audio kept for the final pass. Past MAX_REPLAY_SECONDS the
# replay is refused anyway; this just stops a capture left running all
# afternoon from growing the buffer with it. 16 kHz float32 mono, so a minute
# is under 4 MB.
MAX_CAPTURE_AUDIO_BYTES = BYTES_PER_SECOND * (MAX_REPLAY_SECONDS + 10)

# Tray icon wordmark. "W" reads cleanly at the ~16px Windows renders the tray
# at; "WL" is legible from about 24px up and turns to mush below it.
TRAY_LETTER = "W"

# Windows asks for the tray icon at a handful of sizes depending on DPI and
# taskbar settings. Rendering each one rather than downscaling a single large
# bitmap keeps the letterform crisp — downscaled type blurs badly.
TRAY_ICON_SIZES = (16, 20, 24, 32, 48, 64)

# One colour per thing the user can actually do something about. "starting"
# is distinct from "connecting" because they fail differently: an amber icon
# means WhisperBoard is bringing the server up and the wait is expected, while
# grey means nothing is running and nothing is being done about it.
TRAY_STATE_COLORS = {
    "ready": "#2ecc71",
    "connecting": "#3498db",
    "starting": "#f39c12",
    "recording": "#e74c3c",
    "offline": "#7f8c8d",
    "error": "#c0392b",
}

# How often the tray re-reads its own status while something is in progress,
# so the elapsed counter in the tooltip actually moves.
STATUS_TICK_MS = 1000

# How often to check that the server is still there while no socket is open.
# In on-demand mode nothing else would notice it going away, and a tray icon
# that claims "ready" for a server that died an hour ago is worse than none.
# A minute rather than seconds: this only drives an at-a-glance indicator, and
# the state is re-checked on the hotkey anyway -- which is the moment it
# actually has to be right.
IDLE_PROBE_MS = 60000

# Held for the lifetime of the process; releasing it would let a second
# instance start. Module-level so it is never garbage collected.
_instance_mutex = None


def acquire_single_instance() -> bool:
    """Claim the one-instance-per-session lock. False if already running.

    Two copies of WhisperBoard are actively broken, not merely wasteful: both
    listen for the same global hotkey, so both open a Capture Box stacked on
    the same spot, both stream the microphone to the server, and both fight
    over the foreground. The paste then lands in the *other* instance's box --
    a read-only text area that silently absorbs it -- so dictation appears to
    do nothing at all while the transcript is logged as confirmed.

    A named mutex is used rather than a lock file because Windows releases it
    when the process dies however it dies, leaving nothing to clean up after a
    crash. The "Local\\" prefix scopes it to the logon session, so a second
    user on the same machine still gets their own instance.
    """
    global _instance_mutex
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\WhisperBoard-SingleInstance")
        ERROR_ALREADY_EXISTS = 183
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except (OSError, AttributeError):
        # Not Windows, or the API is unavailable: allow the app to run rather
        # than blocking startup over a guard.
        return True

class WhisperBoardApp:
    def __init__(self):
        self.logger = logging.getLogger("whisperboard.app")
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = 0.0
        # The capture's text so far. The server only ever sends the tail
        # of it -- see transcript.py -- so it is assembled here.
        self._transcript = Transcript()
        # The capture's audio, kept so it can be re-transcribed in one piece
        # once the user confirms -- see final_pass.py. The streamed text is a
        # preview; this is what actually gets pasted when the replay works.
        self._capture_audio = bytearray()
        self._final_pass = None
        # True between confirming and pasting, while a final pass is in
        # flight. Guards against pasting twice when the pass and its watchdog
        # both come back.
        self._paste_pending = False
        # Which capture that pending paste belongs to. A watchdog outlives the
        # pass it was set for, so without this one left over from a capture
        # that already pasted would cut the next capture's pass short.
        self._paste_token = 0
        self.connection_status = "Disconnected"
        self.connection_detail = ""
        # What the tray is currently saying, and since when -- the elapsed
        # time in the tooltip is measured from the last change of label, not
        # from process start, so "Loading model (40s)" means this load.
        self._status_label = ""
        self._status_since = time.monotonic()
        # Latches so a server that is down does not produce a tray balloon on
        # every 15-second probe.
        self._server_failure_notified = False
        # How far the server has got downloading a model it did not already
        # have; empty when nothing is being fetched.
        self._model_progress = ""
        # Win32 handle of the window that had focus before the Capture Box
        # appeared, so paste can be routed back to it (e.g. Notepad).
        self._prev_foreground_hwnd = None

        # Load settings
        self.load_settings()
        self._apply_launch_on_startup(self.settings.get("launch_on_startup", False))

        # UI Windows
        self.settings_window = None
        self.capture_box = CaptureBox()
        self.capture_box.set_font_size(self.settings["capture_font_size"])
        self.capture_box.set_font_family(
            self.settings.get("capture_font_family", DEFAULT_SETTINGS["capture_font_family"]))
        self.capture_box.set_line_spacing(
            self.settings.get("capture_line_spacing", DEFAULT_SETTINGS["capture_line_spacing"]))
        self.capture_box.set_grow_to_fit(
            self.settings.get("capture_grow_to_fit", DEFAULT_SETTINGS["capture_grow_to_fit"]))
        self.capture_box.set_meter_style(self.settings["capture_meter_style"])
        self._apply_capture_appearance()

        # Core Components
        self.audio_capture = AudioCapture()
        self.server_manager = ServerManager(self.settings)
        self.server_manager.state_changed.connect(self.on_server_state_changed)
        self.server_manager.model_progress.connect(self.on_model_progress)
        self._init_tray_icon()
        self._init_hotkey_listener()
        self._init_websocket_client()
        self._init_status_timers()
        self._start_server_if_needed()
        
        # Connect signals
        self.capture_box.confirmed.connect(self.on_capture_confirmed)
        self.capture_box.cancelled.connect(self.on_capture_cancelled)
        self.capture_box.meter_style_changed.connect(self.on_meter_style_changed)
        # Straight from the audio thread, so the meter moves even while the
        # speech gate is holding chunks back from the server.
        self.audio_capture.level_changed.connect(self.capture_box.set_level_db)
        # Kept alongside the stream rather than instead of it: the server gets
        # the audio live so the box can show something immediately, and the
        # copy here is what the final pass replays.
        self.audio_capture.audio_chunk_ready.connect(self._buffer_capture_audio)
        self.app.aboutToQuit.connect(self._shutdown)

    def _init_tray_icon(self):
        self.state_icons = self._build_state_icons()
        self.tray_icon = QSystemTrayIcon(self.app)
        self._set_tray_icon_state("connecting")
        self.menu = QMenu()
        self._create_menu()
        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()
        self._sync_icon_state()

    def _create_menu(self):
        self.status_action = QAction("Status: Disconnected")
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)
        # The second, disabled line is where the diagnosis goes -- "Docker
        # Desktop is not running", "Port 9090 is already in use". Without it
        # the menu can only say something is wrong, not what.
        self.status_detail_action = QAction("")
        self.status_detail_action.setEnabled(False)
        self.menu.addAction(self.status_detail_action)
        self.menu.addSeparator()
        self.start_server_action = QAction("Start Server")
        self.start_server_action.triggered.connect(self.on_start_server)
        self.menu.addAction(self.start_server_action)
        self.stop_server_action = QAction("Stop Server")
        self.stop_server_action.triggered.connect(self.on_stop_server)
        self.menu.addAction(self.stop_server_action)
        self.reconnect_action = QAction("Reconnect")
        self.reconnect_action.triggered.connect(self.on_reconnect)
        self.menu.addAction(self.reconnect_action)
        self.menu.addSeparator()
        self.settings_action = QAction("Settings")
        self.settings_action.triggered.connect(self.open_settings)
        self.menu.addAction(self.settings_action)
        self.history_action = QAction("View Transcription History")
        self.history_action.triggered.connect(self.open_history)
        self.menu.addAction(self.history_action)
        self.menu.addSeparator()
        self.exit_action = QAction("Exit")
        self.exit_action.triggered.connect(self.app.quit)
        self.menu.addAction(self.exit_action)

    def _build_state_icons(self):
        """Builds the tray icons: a bold wordmark on a state-coloured disc."""

        def render(size: int, base_color: str) -> QPixmap:
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)

            inset = max(1, round(size * 0.03))
            diameter = size - 2 * inset
            painter.setBrush(QColor(base_color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(inset, inset, diameter, diameter)

            # The wordmark is filled as a vector path rather than drawn as text.
            # Qt's Windows font engine applies ClearType subpixel antialiasing
            # whatever the style strategy or paint device, which bakes coloured
            # RGB fringes into the glyph edges — visible once the icon is
            # composited over the taskbar. Path filling uses the plain
            # antialiasing rasteriser, so the edges stay neutral.
            font = QFont("Segoe UI")
            font.setBold(True)
            font.setPixelSize(100)  # reference size; scaled to fit below
            path = QPainterPath()
            path.addText(0, 0, font, TRAY_LETTER)
            bounds = path.boundingRect()
            if bounds.isEmpty():
                painter.end()
                return pixmap

            # Scale to fit the disc, then centre on the glyph's own ink rather
            # than the font's line box — capitals sit high in the line box and
            # would look bottom-heavy inside a circle.
            scale = min(diameter * 0.74 / bounds.width(),
                        diameter * 0.56 / bounds.height())
            transform = QTransform()
            transform.translate(size / 2, size / 2)
            transform.scale(scale, scale)
            transform.translate(-bounds.center().x(), -bounds.center().y())

            painter.fillPath(transform.map(path), QColor("#ffffff"))
            painter.end()
            return pixmap

        def make_icon(base_color: str) -> QIcon:
            icon = QIcon()
            for size in TRAY_ICON_SIZES:
                icon.addPixmap(render(size, base_color))
            return icon

        return {state: make_icon(color) for state, color in TRAY_STATE_COLORS.items()}

    def _set_tray_icon_state(self, state: str):
        icon = self.state_icons.get(state, self.state_icons.get("error"))
        if icon:
            self.tray_icon.setIcon(icon)

    def _status_summary(self):
        """The one place that decides what state the app is really in.

        Two independent facts have to be reconciled: whether the server exists
        (the ServerManager) and whether we have a live, model-loaded session on
        it (the WebSocketClient). The socket wins whenever it has something to
        say, because a working connection is proof the server is up; otherwise
        the server manager explains why there is no connection to have.

        Returns (icon_state, label, detail).
        """
        if self.is_capturing and self.connection_status == "Ready":
            return "recording", "Recording", "Listening \u2014 press the hotkey again to paste"

        server_state = self.server_manager.state
        server_detail = self.server_manager.detail

        if self.connection_status == "Ready":
            return "ready", "Ready", self.connection_detail or "Connected, model loaded"
        if self.connection_status == "Waiting":
            return "connecting", "Server busy", self.connection_detail
        if self.connection_status == "Loading":
            if self._model_progress:
                # A first use of a large model is a multi-minute download, not
                # a load. Amber and a different word, because the wait is a
                # different order of magnitude and the user should not read it
                # as the app being stuck.
                return "starting", "Downloading model", self._model_progress
            return "connecting", "Loading model", self.connection_detail
        if self.connection_status == "Connecting":
            return "connecting", "Connecting", self.connection_detail

        # No usable connection. Whatever the server manager is doing is the
        # more informative answer -- it is the reason there is no connection.
        if server_state == server_manager.STATE_PULLING:
            return "starting", "Downloading server", server_detail
        if server_state == server_manager.STATE_STARTING_DOCKER:
            return "starting", "Starting Docker", server_detail
        if server_state in (server_manager.STATE_STARTING, server_manager.STATE_CHECKING):
            return "starting", "Starting server", server_detail
        if server_state == server_manager.STATE_PAUSED:
            # Grey, not red: this is a thing we chose to do, not a fault.
            return "offline", "GPU released", server_detail
        if server_state == server_manager.STATE_FAILED:
            return "error", "Server unavailable", server_detail
        if server_state == server_manager.STATE_RUNNING:
            if self.settings.get("connect_on_demand", False):
                return "ready", "Ready (on-demand)", "Server is running; connects when you dictate"
            # "Reconnecting", not "Connecting": the manager last saw a live
            # server, so the detail here is why the most recent attempt failed
            # rather than a first dial in progress. Calling both "Connecting"
            # put the label at odds with its own detail line.
            return "connecting", "Reconnecting", self.connection_detail or server_detail

        # Nothing is running, and nothing is starting it.
        if self.connection_status == "Error":
            return "error", "Connection error", self.connection_detail
        fallback = self.connection_detail or server_detail
        if self.settings.get("connect_on_demand", False):
            return "offline", "Not connected", fallback or "Connects when you dictate"
        return "offline", "Disconnected", fallback or "No connection to the server"

    def _sync_icon_state(self):
        icon_state, label, detail = self._status_summary()
        if label != self._status_label:
            self._status_label = label
            self._status_since = time.monotonic()

        self._set_tray_icon_state(icon_state)
        self.status_action.setText(f"Status: {label}")
        self.status_detail_action.setText(f"   {detail}" if detail else "")
        self.status_detail_action.setVisible(bool(detail))
        self.tray_icon.setToolTip(self._tooltip_text(label, detail, icon_state))
        self._sync_menu_actions()
        if self._capture_waiting_for_connection:
            self._update_capture_status_text(label, detail)

    def _tooltip_text(self, label: str, detail: str, icon_state: str) -> str:
        text = f"WhisperBoard \u2014 {label}"
        # Elapsed time only while something is in flight, and only once it has
        # gone on long enough to be worth watching. On a settled state, a
        # running counter would be noise.
        if icon_state in ("starting", "connecting"):
            elapsed = int(time.monotonic() - self._status_since)
            if elapsed >= 3:
                text += f" ({elapsed}s)"
        if detail:
            text += "\n" + detail
        return text

    def _sync_menu_actions(self):
        # Shown whenever a local server *could* be managed, not only when
        # auto-start is on: someone who turned auto-start off still needs a
        # way to start the server on the occasions they want it.
        can_manage = self.server_manager.can_manage_server()
        running = self.server_manager.state == server_manager.STATE_RUNNING
        busy = self.server_manager.is_busy()
        self.start_server_action.setVisible(can_manage)
        self.stop_server_action.setVisible(can_manage)
        self.start_server_action.setEnabled(can_manage and not running and not busy)
        self.stop_server_action.setEnabled(can_manage and running and not busy)
        self.reconnect_action.setEnabled(self.connection_status != "Ready")

    def _update_capture_status_text(self, label: str, detail: str):
        """Say what the box is waiting for, instead of a permanent 'Connecting...'.

        A capture opened against a server that is not running used to sit on
        "Connecting..." indefinitely, with no way to tell a slow model load
        from a Docker daemon that is never going to come up.
        """
        if detail:
            self.capture_box.set_text(f"{label}\u2026\n{detail}")
        else:
            self.capture_box.set_text(f"{label}\u2026")

    def _init_status_timers(self):
        # Drives the elapsed counter in the tooltip.
        self._status_timer = QTimer()
        self._status_timer.setInterval(STATUS_TICK_MS)
        self._status_timer.timeout.connect(self._on_status_tick)
        self._status_timer.start()

        # Keeps the icon honest while no socket is open.
        self._probe_timer = QTimer()
        self._probe_timer.setInterval(IDLE_PROBE_MS)
        self._probe_timer.timeout.connect(self._on_idle_probe)
        self._probe_timer.start()

    def _on_status_tick(self):
        icon_state, _, _ = self._status_summary()
        # Only the in-flight states have a moving tooltip; refreshing a settled
        # one would rewrite the same string once a second forever.
        if icon_state in ("starting", "connecting"):
            self._sync_icon_state()

    def _on_idle_probe(self):
        if self.connection_status in ("Ready", "Loading", "Waiting"):
            return  # the live socket is the proof; no need to poll
        if self.server_manager.is_busy():
            return
        self.server_manager.probe()

    def _start_server_if_needed(self):
        """Bring the server up at launch, so it is warm by the first hotkey.

        This runs even in on-demand mode: on-demand is about when to open a
        socket, not about whether a server should exist. Leaving the container
        until the first capture would put a cold Docker start in front of the
        words the user is already speaking.
        """
        # Started regardless of auto-start: standing down for a game is worth
        # doing even for a server the user starts by hand.
        self.server_manager.start_app_watch()
        if self.server_manager.manages_server():
            self.server_manager.ensure_running()
        else:
            self.server_manager.probe()

    def on_server_state_changed(self, state: str, detail: str):
        if state == server_manager.STATE_RUNNING:
            self._maybe_autoconnect()
        elif state == server_manager.STATE_FAILED and not self._server_failure_notified:
            # Once per failure, not once per retry: the probe re-reports the
            # same dead server every 15 seconds, and a balloon each time would
            # be its own problem.
            self._server_failure_notified = True
            self._notify("WhisperLive server unavailable",
                         f"{detail}.\n\nDictation will not work until the server is running.")
        if state != server_manager.STATE_FAILED:
            self._server_failure_notified = False
        self._sync_icon_state()

    def _maybe_autoconnect(self):
        """Open the socket, unless the user asked us to wait for a capture."""
        if self.settings.get("connect_on_demand", False):
            return
        self.websocket_client.connect()

    def on_tray_activated(self, reason):
        # Left or double click: report status where it cannot be missed. The
        # context menu already carries it, but nobody right-clicks a tray icon
        # to find out whether their dictation is going to work.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            _, label, detail = self._status_summary()
            self._notify(f"WhisperBoard \u2014 {label}", detail or "")

    def on_start_server(self):
        self._server_failure_notified = False
        # Forced: clicking Start Server is a clearer instruction than the
        # auto-start checkbox, whichever way that is set.
        self.server_manager.ensure_running(force=True)
        self._sync_icon_state()

    def on_stop_server(self):
        self.server_manager.stop_server()
        self._sync_icon_state()

    def on_reconnect(self):
        self._server_failure_notified = False
        if (self.server_manager.can_manage_server()
                and self.server_manager.state != server_manager.STATE_RUNNING):
            self.server_manager.ensure_running(force=True)
        self.websocket_client.connect()
        self._sync_icon_state()

    def _init_hotkey_listener(self):
        self._start_hotkey_listener(self.settings["capture_hotkey"])

    def _start_hotkey_listener(self, hotkey_str):
        try:
            # Validate the hotkey to ensure it's not just modifiers
            keys = set(part.strip('<>').strip().lower() for part in hotkey_str.split('+'))
            modifier_keys = {'ctrl', 'alt', 'shift', 'cmd'}
            if not keys or keys.issubset(modifier_keys):
                raise ValueError("Hotkey must include at least one non-modifier key (e.g., 'a', 'f1').")

            if hasattr(self, "hotkey_listener") and self.hotkey_listener:
                try:
                    self.hotkey_listener.stop()
                except Exception:
                    self.logger.exception("Failed to stop previous hotkey listener.")

            self.hotkey_listener = HotkeyListener(hotkey_str)
            self.hotkey_listener.hotkey_activated.connect(self.on_hotkey_activated)
            self.hotkey_listener.run()
        except Exception as e:
            self.logger.exception("Failed to initialize hotkey listener.")
            QMessageBox.critical(None, "Hotkey Error", f"Failed to register hotkey '{hotkey_str}'. Please change it in the settings.\n\nError: {e}")

    def _init_websocket_client(self):
        previous_client = getattr(self, "websocket_client", None)
        if previous_client:
            try:
                self.audio_capture.audio_chunk_ready.disconnect(previous_client.send_audio)
            except Exception:
                pass
            try:
                # Before the shutdown, not after: the retiring client emits a
                # final "Disconnected" as its thread unwinds, and that arrives
                # on the GUI thread late enough to overwrite the new client's
                # status with the old one's last word.
                previous_client.connection_status_changed.disconnect(
                    self.on_connection_status_changed)
                previous_client.message_received.disconnect(self.on_message_received)
            except Exception:
                pass
            try:
                previous_client.disconnect()
            except Exception:
                self.logger.exception("Failed to disconnect previous WebSocket client.")

        self.websocket_client = WebSocketClient(
            self.settings["server_address"],
            self.settings.get("model", "distil-small.en"),
            sample_rate=self.audio_capture.rate,
            channels=self.audio_capture.channels,
            audio_format=self.audio_capture.audio_format,
            reconnect_after_capture=self.settings.get("reconnect_after_capture", True),
        )
        self.websocket_client.connection_status_changed.connect(self.on_connection_status_changed)
        self.websocket_client.message_received.connect(self.on_message_received)
        self.audio_capture.audio_chunk_ready.connect(self.websocket_client.send_audio)
        self.connection_status = "Disconnected"
        self.connection_detail = ""
        if not self.settings.get("connect_on_demand", False):
            # Only worth dialling if there is something to dial. When we are
            # starting the server ourselves, the connect happens on the
            # manager's "running" state instead of against a closed port.
            if (not self.server_manager.manages_server()
                    or self.server_manager.state == server_manager.STATE_RUNNING):
                self.websocket_client.connect()

    def load_settings(self):
        try:
            with open(CONFIG_FILE, "r") as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        # Kept before the defaults are backfilled, so first-run detection can
        # tell "the user has no opinion yet" from "the user chose this".
        self._settings_from_disk = set(self.settings)
        # Backfill settings added since this config file was written, so a
        # config from an older build does not KeyError on a new setting.
        for key, value in DEFAULT_SETTINGS.items():
            self.settings.setdefault(key, value)
        self._resolve_gpu_default()
        self.settings["capture_hotkey"] = self._normalize_hotkey_string(
            self.settings.get("capture_hotkey", DEFAULT_SETTINGS["capture_hotkey"])
        )

    def _resolve_gpu_default(self):
        """Pick the server image from the hardware, once, on first run.

        A flat default is wrong on one machine or the other: the CPU image on
        an NVIDIA box leaves the GPU idle and dictation noticeably slower,
        while the GPU image on a machine without one refuses to start. Asked
        once and written to the config, so the Settings checkbox afterwards
        shows -- and can override -- a real answer rather than a guess.
        """
        if "server_use_gpu" in self._settings_from_disk:
            return
        detected = server_manager.detect_nvidia_gpu()
        self.settings["server_use_gpu"] = detected
        self.logger.info("First run: %s NVIDIA GPU detected; using the %s server image.",
                         "an" if detected else "no", "GPU" if detected else "CPU")
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.settings, f, indent=4)
        except OSError:
            # Not fatal -- it is simply re-detected next launch.
            self.logger.exception("Could not persist the detected GPU setting.")

    def _normalize_hotkey_string(self, hotkey: str) -> str:
        """Ensure modifiers are wrapped for pynput parsing."""
        if not hotkey:
            return ""
        parts = []
        for token in hotkey.split('+'):
            t = token.strip().lower()
            if not t:
                continue
            if t in ("<ctrl>", "ctrl", "control"):
                parts.append("<ctrl>")
            elif t in ("<alt>", "alt"):
                parts.append("<alt>")
            elif t in ("<shift>", "shift"):
                parts.append("<shift>")
            elif t in ("<cmd>", "cmd", "win", "windows", "meta", "super"):
                parts.append("<cmd>")
            else:
                # Named keys keep their brackets: pynput parses "<f9>" as
                # Key.f9 but rejects a bare "f9". Stripping them unconditionally
                # meant any hotkey with a function, space, or enter key saved
                # correctly and then failed to load on the next launch.
                t = t.strip("<>")
                parts.append(t if len(t) == 1 else f"<{t}>")
        return "+".join(parts)

    def _startup_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f"\"{sys.executable}\""
        return f"\"{sys.executable}\" \"{os.path.abspath(__file__)}\""

    def _apply_launch_on_startup(self, enabled: bool):
        """Create or remove the Run key entry for autostart on Windows."""
        try:
            import winreg
            run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, "WhisperBoard", 0, winreg.REG_SZ, self._startup_command())
                else:
                    try:
                        winreg.DeleteValue(key, "WhisperBoard")
                    except FileNotFoundError:
                        pass
        except Exception:
            self.logger.exception("Failed to update launch on startup setting.")

    def on_connection_status_changed(self, status, detail=""):
        self.connection_status = status
        self.connection_detail = detail

        # "Ready", not "Loading": the socket being open only means the server
        # accepted us, and audio streamed before SERVER_READY is audio spoken
        # into a model that is not in memory yet.
        if status == "Ready" and self.is_capturing and self._capture_waiting_for_connection:
            self._capture_waiting_for_connection = False
            self._begin_streaming()

        # Only a model load can be waiting on a download; anything else means
        # there is nothing to watch and the poller should not be running.
        if status == "Loading":
            self.server_manager.start_download_watch()
        else:
            self.server_manager.stop_download_watch()
            self._model_progress = ""

        if status in ("Ready", "Loading", "Waiting"):
            # A live session is the strongest possible evidence the server is
            # up, and it beats whatever the manager last concluded from a probe.
            self._server_failure_notified = False
        elif status in ("Disconnected", "Error"):
            # A refused connection means the manager's last verdict may be out
            # of date -- a container can die between probes. Re-check now so
            # the tray explains the failure rather than showing a hopeful
            # "Reconnecting" over a server that is gone.
            if self.server_manager.manages_server() and not self.server_manager.is_busy():
                self.server_manager.probe()

        self._sync_icon_state()
        self.logger.info("Connection status changed: %s (%s)", status, detail)

    def on_model_progress(self, detail: str):
        if detail == self._model_progress:
            return
        self._model_progress = detail
        self._sync_icon_state()

    def on_message_received(self, message_str):
        now = time.time()
        if not self.is_capturing and now > self._post_capture_grace_until:
            self.logger.debug("Dropping message outside capture window.")
            return

        try:
            if isinstance(message_str, (bytes, bytearray)):
                try:
                    message_str = message_str.decode("utf-8", errors="ignore")
                except Exception:
                    self.logger.warning("Failed to decode binary message of len %d", len(message_str))
                    return
            message = json.loads(message_str)
            self._transcript.update(message)
            full_text = self._transcript.text()

            # An empty transcript leaves the box on its "Listening..."
            # placeholder rather than blanking it, and leaves toPlainText()
            # empty so confirming without speaking pastes nothing.
            if full_text:
                self.capture_box.set_text(full_text)
                self.logger.debug("Updated capture text (%d chars).", len(full_text))
            else:
                self.logger.debug("Message received but no text found: %s", message)
        except json.JSONDecodeError:
            self.logger.warning("Received non-JSON message: %s", message_str)
    
    def on_hotkey_activated(self):
        # Ignore hotkey while settings window is open to prevent accidental activation during configuration
        if self.settings_window and self.settings_window.isVisible():
            self.logger.debug("Hotkey pressed while settings window open; ignoring.")
            return
        if self._paste_pending:
            # Dictating again before the last capture pasted. Settle it now,
            # with the streamed text, rather than let it land in the middle of
            # this one -- the box is about to be cleared, so waiting any
            # longer would lose it outright.
            self.logger.debug("New capture while a paste was pending; settling it first.")
            self._on_final_pass_timeout()
        if not self.is_capturing:
            self.is_capturing = True
            self._capture_waiting_for_connection = False
            payload_log.log_event("capture_start")
            # Record the target window now, before the Capture Box steals focus.
            self._record_paste_target()
            self.capture_box.show_at_cursor()
            if self.connection_status == "Ready":
                self._begin_streaming()
            else:
                # Not ready yet: connect-on-demand, a connection still warming
                # up, or a server that is not running at all. The box now
                # reports which of those it is, refreshed as the state moves;
                # on_connection_status_changed() starts streaming once ready.
                self._capture_waiting_for_connection = True
                if (self.server_manager.manages_server()
                        and self.server_manager.state != server_manager.STATE_RUNNING
                        and not self.server_manager.is_busy()):
                    # Dictating is as clear a request for a server as there is.
                    self.server_manager.ensure_running()
                self.websocket_client.connect()
            self._sync_icon_state()
            self.logger.debug("Capture started via hotkey.")
        else:
            if self._capture_waiting_for_connection:
                self.on_capture_cancelled()
            else:
                self.on_capture_confirmed(self.capture_box.text_area.toPlainText())

    def _record_paste_target(self):
        """Remember the window the transcript should be pasted into.

        Anything belonging to WhisperBoard itself is refused: a leftover
        Capture Box or the Settings window can hold the foreground when the
        hotkey fires, and routing the paste there throws the text away. In that
        case the previous target is kept, which is nearly always the window the
        user is actually working in.
        """
        hwnd = get_foreground_window()
        if hwnd and is_own_window(hwnd):
            self.logger.debug(
                "Foreground window 0x%X is ours; keeping previous paste target.",
                hwnd)
            return
        self._prev_foreground_hwnd = hwnd
        if hwnd:
            self.logger.debug("Paste target: 0x%X '%s'", hwnd, get_window_title(hwnd))

    def _begin_streaming(self):
        """Start audio streaming for a capture on a connected session."""
        # Empty text => the box shows its "Listening..." placeholder, while
        # toPlainText() stays "" so confirming without speaking pastes nothing.
        self.capture_box.set_text("")
        # Drop the last capture's words before any of this one arrive.
        self._transcript.reset()
        self._capture_audio.clear()
        payload_log.log_event("streaming_start")
        self.websocket_client.reset_eos()
        self.audio_capture.start_streaming()

    def on_capture_confirmed(self, text):
        if not self.is_capturing:
            return
        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = time.time() + 8.0
        payload_log.log_event("capture_confirmed")
        # The box stays up with Confirm spinning until the paste is actually
        # on its way, which is not the same moment the button was clicked:
        # re-transcribing the capture takes up to a second or so, and with the
        # box already gone that is a second of nothing happening after a
        # click -- which reads as a hang rather than as work. Set before the
        # work below rather than after, so a slow shutdown of the audio thread
        # is inside the spinner too; nothing is repainted in between, so on
        # the quick path it costs a flag and no frame.
        self.capture_box.set_busy(True)
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._schedule_on_demand_disconnect()
        self._sync_icon_state()
        self.logger.debug("Capture confirmed.")
        if self._start_final_pass():
            # _dismiss_capture_box() takes the box down when the pass reports
            # back, or when the watchdog gives up on it.
            return
        self._dismiss_capture_box()
        QTimer.singleShot(PASTE_START_DELAY_MS, self._do_paste)

    def _dismiss_capture_box(self):
        """Take the box down, on the way to pasting what it was holding.

        processEvents() flushes the hide and the focus change that follows it,
        so the OS can start handing the foreground back to the app being
        dictated into while the paste is still being set up.
        """
        self.capture_box.set_busy(False)
        self.capture_box.hide()
        QApplication.processEvents()

    def _buffer_capture_audio(self, chunk: bytes):
        """Keep the capture's audio for the final pass."""
        if not self.is_capturing:
            return
        if len(self._capture_audio) >= MAX_CAPTURE_AUDIO_BYTES:
            return
        self._capture_audio.extend(chunk)

    def _start_final_pass(self) -> bool:
        """Re-transcribe the capture in one piece. True if the paste now waits.

        False means paste the streamed text as before -- the setting is off,
        there is no server to ask, or the clip is too short or too long to
        replay (final_pass.py explains both bounds). None of those are errors;
        the streamed text is what would have been pasted anyway.
        """
        if not self.settings.get("final_pass", True):
            return False
        if self.connection_status != "Ready":
            self.logger.debug("No final pass: connection is %s.", self.connection_status)
            return False
        if (self.server_manager.manages_server()
                and not self.settings.get("share_one_model", True)):
            # Without a shared model the server loads a fresh copy of it for
            # every connection, so the replay would spend the whole budget
            # waiting for a load and cost the VRAM of a second copy to do it.
            # Only checked for a server we start ourselves -- it is the only
            # one this setting actually describes.
            self.logger.debug("No final pass: the server loads a model per connection.")
            return False
        audio = bytes(self._capture_audio)
        if not FinalPass.is_replayable(audio):
            self.logger.debug("No final pass: %.1fs of audio is outside the replayable range.",
                              len(audio) / BYTES_PER_SECOND)
            return False

        if self._final_pass is None:
            self._final_pass = FinalPass(
                self.settings["server_address"], self.settings["model"], VAD_PARAMETERS)
            self._final_pass.finished.connect(self._on_final_pass_finished)
        else:
            # Server address and model can have changed in Settings since the
            # last capture. (A pass still in flight from that capture is
            # disowned by run() below, so it cannot come back and overwrite
            # this one.)
            self._final_pass.server_address = self.settings["server_address"]
            self._final_pass.model = self.settings["model"]

        self._paste_pending = True
        self._paste_token += 1
        token = self._paste_token
        self._final_pass.run(audio)
        QTimer.singleShot(FINAL_PASS_WATCHDOG_MS,
                          lambda: self._on_final_pass_timeout(token))
        self.logger.debug("Final pass started over %.1fs of audio.",
                          len(audio) / BYTES_PER_SECOND)
        return True

    def _on_final_pass_finished(self, text: str):
        if not self._paste_pending:
            return
        self._paste_pending = False
        # Read before the box goes: without a re-transcription the streamed
        # text in the box is what gets pasted, and hiding it first would mean
        # reading it back out of a window that is on its way out.
        text = text or self.capture_box.text_area.toPlainText()
        self._dismiss_capture_box()
        QTimer.singleShot(PASTE_START_DELAY_MS, lambda: self._do_paste(text))

    def _on_final_pass_timeout(self, token=None):
        if not self._paste_pending:
            return
        if token is not None and token != self._paste_token:
            return
        self._paste_pending = False
        self.logger.warning("Final pass did not report back; pasting the streamed text.")
        if self._final_pass:
            self._final_pass.abandon()
        text = self.capture_box.text_area.toPlainText()
        self._dismiss_capture_box()
        QTimer.singleShot(PASTE_START_DELAY_MS, lambda: self._do_paste(text))

    def on_capture_cancelled(self):
        if not self.is_capturing:
            return
        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = time.time() + 8.0
        payload_log.log_event("capture_cancelled")
        self._paste_pending = False
        if self._final_pass:
            self._final_pass.abandon()
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._schedule_on_demand_disconnect()
        self._sync_icon_state()
        self.write_to_history(self.capture_box.text_area.toPlainText(), status="CANCELLED")
        self.logger.debug("Capture cancelled.")

    def _schedule_on_demand_disconnect(self):
        if not self.settings.get("connect_on_demand", False):
            return
        # Delay disconnect slightly so the server can deliver final transcripts after EOS.
        QTimer.singleShot(1200, self.websocket_client.disconnect)

    def write_to_history(self, text, status="CONFIRMED"):
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] {status}: {text}\n")
        except Exception as e:
            self.logger.exception("Failed to write to history file.")

    def open_history(self):
        try:
            if os.path.exists(HISTORY_FILE):
                os.startfile(HISTORY_FILE)
            else:
                pass
        except Exception as e:
            self.logger.exception("Failed to open history file.")
            QMessageBox.critical(None, "Error", f"Could not open history file.\n\nError: {e}")

    def open_settings(self):
        try:
            if hasattr(self, "hotkey_listener") and self.hotkey_listener:
                self.hotkey_listener.stop()
        except Exception:
            self.logger.exception("Error stopping hotkey listener before opening settings.")

        if self.settings_window is None:
            self.settings_window = SettingsWindow()
            self.settings_window.settings_saved.connect(self.on_settings_saved)
            self.settings_window.window_closed.connect(self.on_settings_closed)
            self.settings_window.preview_requested.connect(self.on_meter_preview_requested)
            self.audio_capture.level_changed.connect(
                self.settings_window.set_preview_level_db)
        else:
            self.settings_window.load_settings()
            try:
                self.settings_window.window_closed.disconnect()
            except Exception:
                pass
            self.settings_window.window_closed.connect(self.on_settings_closed)
        self.settings_window.show()
        self.settings_window.activateWindow()

    def on_settings_closed(self):
        """Resume hotkey listener when settings window closes."""
        self._start_hotkey_listener(self.settings["capture_hotkey"])

    def on_meter_preview_requested(self, active: bool):
        """Open or release the mic for the Settings window's meter preview.

        Safe to run only because the hotkey listener is stopped for as long as
        the Settings window is open, so a capture cannot start underneath the
        preview and find the device already streaming in meter-only mode.
        """
        if self.is_capturing:
            return
        if active:
            self.audio_capture.start_streaming(meter_only=True)
        else:
            self.audio_capture.stop_streaming()

    def on_settings_saved(self, updated_settings):
        """Apply updated settings from the Settings window."""
        self.settings = updated_settings
        self.settings["capture_hotkey"] = self._normalize_hotkey_string(self.settings.get("capture_hotkey", ""))
        self._apply_launch_on_startup(self.settings.get("launch_on_startup", False))
        self._start_hotkey_listener(self.settings["capture_hotkey"])
        self.capture_box.set_font_size(
            self.settings.get("capture_font_size", DEFAULT_SETTINGS["capture_font_size"]))
        self.capture_box.set_font_family(
            self.settings.get("capture_font_family", DEFAULT_SETTINGS["capture_font_family"]))
        self.capture_box.set_line_spacing(
            self.settings.get("capture_line_spacing", DEFAULT_SETTINGS["capture_line_spacing"]))
        self.capture_box.set_grow_to_fit(
            self.settings.get("capture_grow_to_fit", DEFAULT_SETTINGS["capture_grow_to_fit"]))
        self.capture_box.set_meter_style(
            self.settings.get("capture_meter_style", DEFAULT_SETTINGS["capture_meter_style"]))
        self._apply_capture_appearance()
        self.server_manager.update_settings(self.settings)
        self._init_websocket_client()
        # A server address or image change makes the previous verdict stale,
        # and turning auto-start on is a request to start it now, not next
        # launch.
        self._server_failure_notified = False
        self._start_server_if_needed()
        self._sync_icon_state()

    def _apply_capture_appearance(self):
        """Push the Capture Box's look from settings onto the box."""
        self.capture_box.set_surface(
            self.settings.get("capture_bg_color", DEFAULT_SETTINGS["capture_bg_color"]),
            self.settings.get("capture_opacity", DEFAULT_SETTINGS["capture_opacity"]),
            self.settings.get("capture_panel_frost", DEFAULT_SETTINGS["capture_panel_frost"]))
        self.capture_box.set_field(
            self.settings.get("capture_field_color", DEFAULT_SETTINGS["capture_field_color"]),
            self.settings.get("capture_field_opacity", DEFAULT_SETTINGS["capture_field_opacity"]),
            self.settings.get("capture_field_frost", DEFAULT_SETTINGS["capture_field_frost"]))
        self.capture_box.set_frost(
            self.settings.get("capture_blur_radius",
                              DEFAULT_SETTINGS["capture_blur_radius"]),
            self.settings.get("capture_frost_saturation",
                              DEFAULT_SETTINGS["capture_frost_saturation"]),
            self.settings.get("capture_frost_brightness",
                              DEFAULT_SETTINGS["capture_frost_brightness"]),
            self.settings.get("capture_frost_levelling",
                              DEFAULT_SETTINGS["capture_frost_levelling"]))

    def on_meter_style_changed(self, style: str):
        """Persist a style picked by clicking the meter itself.

        Written straight to the config file rather than through the Settings
        window: the window is usually closed while a capture is running, and a
        style tried out mid-capture is worth nothing if it is forgotten by the
        next one.
        """
        self.settings["capture_meter_style"] = style
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.settings, f, indent=4)
        except Exception:
            self.logger.exception("Failed to save meter style.")

    def _do_paste(self, text=None):
        """Put the transcript on the clipboard and paste it into the target.

        Split across timer hops rather than run straight through: each stage
        gives Windows and the target application a chance to process the one
        before it, and returning to the event loop in between keeps the app
        responsive while the foreground changes hands.

        `text` is the final pass's re-transcription when there is one. Without
        it the streamed text in the box is used, which is the same text a
        moment less polished -- see final_pass.py for what the difference is.
        """
        # Read the final transcription now -- the box may have updated between
        # confirm and here -- and log it before anything else can fail.
        if text is None:
            text = self.capture_box.text_area.toPlainText()
        text = text.strip()
        self.write_to_history(text, status="CONFIRMED")
        if not text:
            self.logger.debug("Nothing transcribed; skipping paste.")
            return

        # Dictation happens a phrase at a time, and the next paste lands right
        # where this one left the caret, so without this the last word of one
        # capture and the first of the next run together. Trailing rather than
        # leading: a leading space would be wrong at the start of a line, and
        # would put the caret one character further from where the user is
        # about to keep typing. The history keeps the clean text -- the space
        # is a paste-time affordance, not part of what was said.
        text += " "

        # The clipboard is written first and left alone from here on. Whatever
        # happens to the keystroke afterwards, the transcript is recoverable
        # with a manual Ctrl+V.
        if not win_input.set_clipboard_text(text):
            self.logger.warning("Clipboard write failed; falling back to typing.")
            self._paste_by_typing(text)
            return

        QTimer.singleShot(PASTE_FOCUS_DELAY_MS, lambda: self._restore_focus_and_paste(text))

    def _restore_focus_and_paste(self, text):
        hwnd = self._prev_foreground_hwnd
        if not hwnd or not is_window(hwnd):
            self._paste_unavailable(
                "the window you were in is gone", text)
            return
        if is_own_window(hwnd):
            # Should be unreachable now that _record_paste_target filters our
            # own windows, but pasting into our read-only box loses the text
            # silently, so it is worth refusing twice.
            self._paste_unavailable("the target window belongs to WhisperBoard", text)
            return

        if not focus_window(hwnd):
            self._paste_unavailable(
                f"could not focus '{get_window_title(hwnd)}'", text)
            return

        # Clear anything the user is still holding. Confirming with the hotkey
        # means Ctrl is usually down; a held Shift or Alt would turn the
        # injected Ctrl+V into a different shortcut entirely.
        released = win_input.release_modifiers()
        if released:
            self.logger.debug("Released held modifiers before paste: %s",
                              ", ".join(sorted(set(released))))

        QTimer.singleShot(PASTE_KEY_DELAY_MS, lambda: self._send_paste(text, hwnd))

    def _send_paste(self, text, hwnd):
        # The foreground can change again in the gap above -- a notification
        # toast, or the user clicking elsewhere -- so it is rechecked rather
        # than assumed.
        foreground = get_foreground_window()
        if foreground != hwnd:
            self.logger.debug("Foreground drifted to 0x%X before paste; retrying focus.",
                              foreground or 0)
            if not focus_window(hwnd):
                self._paste_unavailable("focus moved to another window", text)
                return

        if not win_input.send_ctrl_v():
            self._paste_unavailable(
                "Windows blocked the keystroke, which usually means the target "
                "app is running as administrator", text)
            return

        self.logger.info("Pasted %d chars into 0x%X '%s'.",
                         len(text), hwnd, get_window_title(hwnd))

    def _paste_by_typing(self, text):
        """Last resort when the clipboard is unusable: type the text out."""
        hwnd = self._prev_foreground_hwnd
        if hwnd and is_window(hwnd) and not is_own_window(hwnd) and focus_window(hwnd):
            win_input.release_modifiers()
            if win_input.type_text(text):
                self.logger.info("Typed %d chars into 0x%X.", len(text), hwnd)
                return
        self.logger.error("Could not deliver the transcript by typing either.")
        self._notify("Transcript could not be pasted",
                     "It is in your transcription history.")

    def _paste_unavailable(self, reason, text):
        """Report a paste that could not be delivered, without losing the text.

        The transcript is already on the clipboard by the time this is called,
        so the recovery is one keystroke -- but only if the user is told, which
        is what made the previous silent failures so confusing.
        """
        self.logger.warning("Paste not delivered: %s.", reason)
        preview = text if len(text) <= 60 else text[:57] + "..."
        self._notify("Transcript copied to clipboard",
                     f"Press Ctrl+V to paste it — {reason}.\n{preview}")

    def _notify(self, title, message):
        try:
            self.tray_icon.showMessage(title, message, self.tray_icon.icon(), 6000)
        except Exception:
            self.logger.exception("Failed to show tray notification.")

    def _shutdown(self):
        self._paste_pending = False
        if getattr(self, "_final_pass", None):
            self._final_pass.abandon()
        self.audio_capture.shutdown()
        if getattr(self, "websocket_client", None):
            self.websocket_client.disconnect()
        if getattr(self, "server_manager", None):
            self.server_manager.stop_app_watch()
            # Abandons any in-flight Docker work. The container itself is left
            # running: it was started with a restart policy so the next launch
            # finds it warm, and stopping it here would make every quit cost
            # the next session a cold model load. "Stop Server" in the tray
            # menu is the deliberate way to shut it down.
            self.server_manager.shutdown()

    def run(self):
        sys.exit(self.app.exec())

def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.INFO)

    if not acquire_single_instance():
        logging.getLogger("whisperboard.app").error(
            "Another WhisperBoard instance is already running; exiting.")
        # A QApplication is needed for the message box, and it must be created
        # before any widget. This one is discarded with the process.
        QApplication(sys.argv)
        QMessageBox.warning(
            None, "WhisperBoard",
            "WhisperBoard is already running.\n\n"
            "Look for the tray icon near the clock. Running two copies breaks "
            "dictation: both open a capture box on the same hotkey and the "
            "paste lands in the wrong one.")
        sys.exit(0)

    app = WhisperBoardApp()
    app.run()

if __name__ == "__main__":
    main()
