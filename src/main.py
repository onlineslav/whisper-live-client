import copy
import ctypes
import sys
import json
import os
import logging
import time
from datetime import datetime
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QMessageBox
from PySide6.QtCore import QTimer
from branding import wordmark_icon
from settings_window import SettingsWindow, DEFAULT_SETTINGS, APP_DATA_DIR, CONFIG_FILE
from hotkey_listener import HotkeyListener
from websocket_client import WebSocketClient, VAD_PARAMETERS
from audio_capture import AudioCapture
from capture_box import CaptureBox
import win_input
import payload_log
import server_manager
import caret_target
import target_overlay
from target_overlay import TargetOverlay
from live_type import LiveTyper
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

# How often a running capture checks which window is in front. Polled rather
# than hooked: it only runs while a capture is up, and a user switching
# windows does not notice a sixth of a second.
FOREGROUND_POLL_MS = 150

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

# One colour per thing the user can actually do something about. "starting"
# is distinct from "connecting" because they fail differently: an amber icon
# means WhisperType is bringing the server up and the wait is expected, while
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

# How often the target marker re-checks the window it is drawn on. Only ever
# the cheap half of caret_target -- no cross-process call -- so this is not
# about cost but about how quickly the marker should follow a window being
# dragged, which is the one thing it has to keep up with.
TARGET_POLL_MS = 200

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

    Two copies of WhisperType are actively broken, not merely wasteful: both
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
        _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\WhisperType-SingleInstance")
        ERROR_ALREADY_EXISTS = 183
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except (OSError, AttributeError):
        # Not Windows, or the API is unavailable: allow the app to run rather
        # than blocking startup over a guard.
        return True

class WhisperTypeApp:
    def __init__(self):
        self.logger = logging.getLogger("whispertype.app")
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        # The title-bar / Alt-Tab / taskbar icon for every window the app
        # opens (Settings, dialogs), so they match the tray's wordmark. The
        # tray sets its own status-coloured variant on top of this.
        self.app.setWindowIcon(wordmark_icon())

        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = 0.0
        # The capture's text so far. The server only ever sends the tail
        # of it -- see transcript.py -- so it is assembled here.
        self._transcript = Transcript()
        # Text from earlier sessions of this same capture. A connection that
        # drops mid-capture is replaced, and the replacement's timestamps
        # start again at zero, so what the old one said is set aside here
        # rather than folded into a Transcript that would alias the two.
        self._banked_text = ""
        # True from a mid-capture drop until the replacement is ready.
        self._capture_interrupted = False
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
        # The same for the on-demand disconnect: only the newest one scheduled
        # may fire, or one left over from the previous capture hangs up on the
        # server while it is still sending this one's last words.
        self._disconnect_token = 0
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
        # Where in that window the text is going to land, and the marker drawn
        # on it. Resolved once per capture while the target still has focus --
        # see _record_paste_target.
        self._paste_target = None
        # True while this capture is previewing its words at the caret, which
        # is also what decides whether the Capture Box runs slimmed down.
        self._ghosting = False

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
        self.target_overlay = TargetOverlay()
        self.live_typer = LiveTyper()
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
        # Which window is in front, for as long as a capture is up. Decides
        # whether the box is faded (the user is elsewhere) and whether Enter
        # and Escape belong to the capture -- see _check_capture_foreground.
        self._foreground_timer = QTimer()
        self._foreground_timer.setInterval(FOREGROUND_POLL_MS)
        self._foreground_timer.timeout.connect(self._check_capture_foreground)
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
        """The tray icons: the app wordmark on a disc, one colour per state."""
        return {state: wordmark_icon(color)
                for state, color in TRAY_STATE_COLORS.items()}

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
            if self.connection_status == "Ready":
                # "Ready" from a session the last capture just ended -- it is
                # already closing, and this capture is waiting on its
                # replacement.
                label, detail = "Connecting", ""
            self._update_capture_status_text(label, detail)
        elif self.is_capturing and self._capture_interrupted:
            self._show_interruption()

    def _tooltip_text(self, label: str, detail: str, icon_state: str) -> str:
        text = f"WhisperType \u2014 {label}"
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
        # A slimmed box has no transcript area to say this in, and "waiting on
        # Docker" is exactly the message that must not be invisible. The box
        # goes back to full height for as long as it has status to report, and
        # _begin_streaming slims it again once there is a transcript instead.
        self.capture_box.set_compact(False)
        if detail:
            self.capture_box.set_text(f"{label}\u2026\n{detail}")
        else:
            self.capture_box.set_text(f"{label}\u2026")

    def _show_interruption(self):
        """Say the connection went, under the words that made it through.

        The text shown is never what gets pasted -- that is _capture_text() --
        so the status lines here cannot end up in the document.
        """
        status = self.connection_status
        if status == "Loading":
            headline = "Reconnecting \u2014 loading the model\u2026"
        elif status == "Waiting":
            headline = "Reconnecting \u2014 the server is busy\u2026"
        elif status == "Connecting":
            headline = "Connection lost \u2014 reconnecting\u2026"
        else:
            headline = "Connection lost \u2014 retrying\u2026"
        lines = [headline]
        # Connecting and Loading details only restate the headline.
        if self.connection_detail and status not in ("Connecting", "Loading"):
            lines.append(self.connection_detail)
        lines.append("Keep talking: it catches up once reconnected. "
                     "Confirm keeps what you have so far.")
        text = self._capture_text()
        status_block = "\n".join(lines)
        # A slimmed box has no room for this; see _update_capture_status_text.
        self.capture_box.set_compact(False)
        self.capture_box.set_text(f"{text}\n\n{status_block}" if text else status_block)

    def _capture_text(self) -> str:
        """Everything this capture has heard, across any reconnects."""
        return " ".join(part for part in (self._banked_text, self._transcript.text()) if part)

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

        # Keeps the target marker on the target. Runs only during a capture.
        self._target_timer = QTimer()
        self._target_timer.setInterval(TARGET_POLL_MS)
        self._target_timer.timeout.connect(self._on_target_tick)

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
            self._notify(f"WhisperType \u2014 {label}", detail or "")

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
            # Enter and Escape, for the captures the box cannot hear them in
            # itself. Reconnected here rather than once at startup because the
            # listener is rebuilt whenever the hotkey changes.
            self.hotkey_listener.confirm_requested.connect(self.on_capture_confirm_key)
            self.hotkey_listener.cancel_requested.connect(self.on_capture_cancel_key)
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
                # Drop the pre-rename entry whichever way this goes, so a box
                # that had autostart on under the old name does not keep
                # launching the old path alongside the new one.
                try:
                    winreg.DeleteValue(key, "WhisperBoard")
                except FileNotFoundError:
                    pass
                if enabled:
                    winreg.SetValueEx(key, "WhisperType", 0, winreg.REG_SZ, self._startup_command())
                else:
                    try:
                        winreg.DeleteValue(key, "WhisperType")
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
        elif self.is_capturing and not self._capture_waiting_for_connection:
            # Streaming, so any news about the connection is news about this
            # capture. Only a fresh session reports "Ready" -- SERVER_READY
            # comes once per connection -- so anything else means the one
            # being streamed into has gone.
            if status == "Ready":
                if self._capture_interrupted:
                    self._resume_streaming()
            elif not self._capture_interrupted:
                self._interrupt_capture()

        if self.is_capturing and status in ("Disconnected", "Error"):
            # The client redials on its own while it is running, but a thread
            # that stopped -- told to, or failed outright -- does not come
            # back by itself, and a capture is waiting on it either way.
            self.websocket_client.connect()

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
            if self._capture_interrupted:
                # Whatever still arrives from the session that dropped was
                # already banked with the rest of its text; folding it in
                # again would duplicate it.
                self.logger.debug("Dropping message from an interrupted session.")
                return
            self._transcript.update(message)
            full_text = self._capture_text()

            # An empty transcript leaves the box on its "Listening..."
            # placeholder rather than blanking it, and leaves toPlainText()
            # empty so confirming without speaking pastes nothing.
            if full_text:
                # The box is fed even while it is slimmed down and hiding its
                # transcript: confirming reads the text back out of it.
                self.capture_box.set_text(full_text)
                if self._ghosting:
                    self.live_typer.update(full_text)
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
            self._capture_interrupted = False
            # Cleared here and not only once streaming starts, so that the
            # text of a capture that never got a connection is empty rather
            # than the previous capture's.
            self._transcript.reset()
            self._banked_text = ""
            payload_log.log_event("capture_start")
            # Record the target window now, before the Capture Box steals focus.
            self._record_paste_target()
            # Live typing first: whether it started decides how much of the
            # box is needed, whether the box may take the keyboard, and what
            # the marker draws.
            self._ghosting = self._start_live_typing()
            self.capture_box.set_compact(self._ghosting)
            self.capture_box.set_passive(self._ghosting)
            # Before the box, so the flash is already running when the box
            # fades in over it rather than starting after it has settled.
            self._show_target_marker()
            self.capture_box.show_at_cursor(self._capture_anchor())
            # Enter and Escape are claimed from here on whenever the target is
            # in front -- a passive box never receives a keystroke, and either
            # kind of box can be left for another window and come back to.
            self._check_capture_foreground()
            self._foreground_timer.start()
            # The client's own answer, not the last status it reported: once
            # the previous capture sent END_OF_AUDIO its session is spent, but
            # "Ready" stands until the socket actually closes.
            if self.websocket_client.is_ready:
                self._begin_streaming()
            else:
                # Not ready yet: connect-on-demand, a connection still warming
                # up, or a server that is not running at all. The box now
                # reports which of those it is, refreshed as the state moves;
                # on_connection_status_changed() starts streaming once ready.
                self._capture_waiting_for_connection = True
                self._request_server()
                self.websocket_client.connect()
            self._sync_icon_state()
            self.logger.debug("Capture started via hotkey.")
        else:
            if self._capture_waiting_for_connection:
                self.on_capture_cancelled()
            else:
                self.on_capture_confirmed(self._capture_text())

    def _claim_capture_keys(self, claimed: bool):
        """Take Enter and Escape from the desktop for this capture, or return them."""
        listener = getattr(self, "hotkey_listener", None)
        if listener is None:
            return
        try:
            listener.claim_capture_keys(claimed)
        except Exception:
            self.logger.exception("Could not change the capture key claim.")

    def _check_capture_foreground(self):
        """Fade the box, and hand Enter/Escape back, by which window is in front.

        Three cases, by what the user is looking at:

        * The target -- where the text is going. Enter and Escape belong to
          the capture: a passive box cannot hear them, and a box the user has
          clicked back past cannot either. Enter here ends in a paste into
          this same window, so claiming it costs the document nothing.
        * The box itself (or any other window of ours). It hears the keys
          itself, so nothing is claimed.
        * Anything else -- the browser opened to check a fact. The capture
          keeps recording, but the keys are the user's, the box fades, and
          it says the hotkey is how to finish.
        """
        if not self.is_capturing:
            # Ended some way that did not stop the timer on its own.
            self._foreground_timer.stop()
            self._claim_capture_keys(False)
            return
        if not self.capture_box.isVisible():
            # Hidden for a moment by the frost fallback (refresh_backdrop),
            # not closed: that is what is_capturing is for.
            return
        hwnd = get_foreground_window()
        if not hwnd:
            # Mid-switch, or the desktop has the foreground for a moment.
            # Nothing to learn from it; the next tick will know.
            return
        at_target = bool(self._prev_foreground_hwnd) and hwnd == self._prev_foreground_hwnd
        away = not at_target and not is_own_window(hwnd)
        self._claim_capture_keys(at_target)
        self.capture_box.set_away(
            away, f"Still listening · {self._hotkey_label()} to paste")

    def _hotkey_label(self) -> str:
        """The capture hotkey the way a person would write it: "F13", "Ctrl+`"."""
        names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
        parts = []
        for token in self.settings.get("hotkey", "").split("+"):
            key = token.strip().strip("<>").lower()
            if not key:
                continue
            if key in names:
                parts.append(names[key])
            elif key[0] == "f" and key[1:].isdigit():
                parts.append(key.upper())
            else:
                parts.append(key.capitalize())
        return "+".join(parts) or "the hotkey"

    def on_capture_confirm_key(self):
        """Enter, pressed while the capture had claimed it from the desktop.

        That is, while a passive box was up or the target window was in front
        -- see _check_capture_foreground. The box has no keyboard of its own to
        hear it with, so it arrives here instead -- and does exactly what
        clicking Confirm does. The one
        exception is a box that is still reporting on the connection: there is
        no transcript in it yet, only a status message, and confirming would
        paste that. The hotkey answers the same case the same way.
        """
        if not self.is_capturing or not self.capture_box.isVisible():
            return
        if self._capture_waiting_for_connection:
            self.capture_box.on_cancel()
        else:
            self.capture_box.on_confirm()

    def on_capture_cancel_key(self):
        """Escape, pressed while the capture had claimed it from the desktop."""
        if not self.is_capturing or not self.capture_box.isVisible():
            return
        self.capture_box.on_cancel()

    def _record_paste_target(self):
        """Remember the window the transcript should be pasted into, and where.

        Anything belonging to WhisperType itself is refused: a leftover
        Capture Box or the Settings window can hold the foreground when the
        hotkey fires, and routing the paste there throws the text away. In that
        case the previous target is kept, which is nearly always the window the
        user is actually working in.

        The caret is located here rather than anywhere later because this is
        the last moment it can be: locating it asks the operating system what
        has focus, and a few milliseconds from now the answer is the Capture
        Box's own read-only text area. See caret_target.py.
        """
        hwnd = get_foreground_window()
        if hwnd and is_own_window(hwnd):
            self.logger.debug(
                "Foreground window 0x%X is ours; keeping previous paste target.",
                hwnd)
        else:
            self._prev_foreground_hwnd = hwnd
            if hwnd:
                self.logger.debug("Paste target: 0x%X '%s'", hwnd,
                                  get_window_title(hwnd))

        self._paste_target = None
        if not self._prev_foreground_hwnd:
            return
        if not (self.settings.get("capture_show_target",
                                  DEFAULT_SETTINGS["capture_show_target"])
                or self.settings.get("capture_follow_caret",
                                     DEFAULT_SETTINGS["capture_follow_caret"])
                or self.settings.get("capture_live_typing",
                                     DEFAULT_SETTINGS["capture_live_typing"])):
            # Nothing that needs the caret is wanted -- no marker, no
            # caret-following, no live typing -- so there is nothing to spend
            # a UI Automation round trip on.
            return
        try:
            # The font at the caret is only worth a COM call when something is
            # going to draw text in it.
            self._paste_target = caret_target.locate(
                self._prev_foreground_hwnd,
                want_style=self.settings.get(
                    "capture_live_typing", DEFAULT_SETTINGS["capture_live_typing"]))
        except Exception:
            # Never at the cost of the capture itself: a marker that cannot be
            # worked out is a missing marker, not a dictation that does not
            # start.
            self.logger.exception("Could not locate the paste target.")

    def _start_live_typing(self) -> bool:
        """Begin typing the transcript into the target, if it qualifies.

        True when it is running, which is also the signal to slim the Capture
        Box and stop it taking the keyboard: the words are going into the
        document itself, so the box has no transcript to show and the target
        must keep focus for the keystrokes to reach it.
        """
        if not self.settings.get("capture_live_typing",
                                 DEFAULT_SETTINGS["capture_live_typing"]):
            return False
        try:
            return self.live_typer.begin(self._paste_target,
                                         self._prev_foreground_hwnd)
        except Exception:
            self.logger.exception("Could not start live typing.")
            return False

    def _undo_live_typing(self):
        """Take back the preview text, wherever the capture is going next."""
        try:
            if self.live_typer.typed:
                self.live_typer.clear()
        except Exception:
            self.logger.exception("Could not remove the live-typed preview.")
        finally:
            self.live_typer.reset()

    def _show_target_marker(self):
        """Put the marker up for the capture that is starting."""
        if not self.settings.get("capture_show_target",
                                 DEFAULT_SETTINGS["capture_show_target"]):
            return
        if self._paste_target is None or self._paste_target.source == "none":
            return
        try:
            # The application's own caret is moving along with the typed text
            # while live typing runs, so a second marker sitting where the
            # caret started is worse than none. The field outline stays.
            self.target_overlay.set_caret_visible(not self._ghosting)
            self.target_overlay.show_target(self._paste_target)
            self._target_timer.start()
        except Exception:
            self.logger.exception("Could not show the target marker.")

    def _hide_target_marker(self):
        """Take the marker down. Safe whether or not it ever went up."""
        self._target_timer.stop()
        self._ghosting = False
        try:
            self.target_overlay.dismiss()
        except Exception:
            self.logger.exception("Could not dismiss the target marker.")

    def _on_target_tick(self):
        """Keep the marker on the target while the capture runs.

        Only the cheap half of caret_target -- see refresh() there. The window
        can be dragged, minimised or closed while the box is up, and the
        marker following it (or going grey when it cannot) is the difference
        between a mark that means something and a mark left on the desktop.
        """
        if self._paste_target is None or not self.target_overlay.isVisible():
            self._target_timer.stop()
            return
        try:
            self._paste_target = caret_target.refresh(
                self._paste_target, self._prev_foreground_hwnd)
            self.target_overlay.update_target(self._paste_target)
        except Exception:
            self.logger.exception("Could not refresh the target marker.")
            self._hide_target_marker()

    def _capture_anchor(self):
        """Where the Capture Box should open, or None for beside the mouse.

        Under the caret normally. Under the whole field while ghost text is
        showing, because the space just below the caret is where the ghost is
        about to write -- a box anchored there would cover the words it exists
        to stop duplicating.
        """
        if not self.settings.get("capture_follow_caret",
                                 DEFAULT_SETTINGS["capture_follow_caret"]):
            return None
        if self._paste_target is None:
            return None
        try:
            if self._ghosting and self._paste_target.field is not None:
                return target_overlay.anchor_below(self._paste_target.field)
            return target_overlay.anchor_below(self._paste_target.caret)
        except Exception:
            self.logger.exception("Could not derive a capture anchor.")
            return None

    def _begin_streaming(self):
        """Start audio streaming for a capture on a connected session."""
        # Empty text => the box shows its "Listening..." placeholder, while
        # toPlainText() stays "" so confirming without speaking pastes nothing.
        self.capture_box.set_text("")
        # Back to a strip, if this capture is typing: any status message that
        # widened the box has served its purpose once audio is flowing.
        self.capture_box.set_compact(self._ghosting)
        # Drop the last capture's words before any of this one arrive.
        self._transcript.reset()
        self._banked_text = ""
        self._capture_interrupted = False
        self._capture_audio.clear()
        payload_log.log_event("streaming_start")
        self.websocket_client.reset_eos()
        self.audio_capture.start_streaming()

    def _interrupt_capture(self):
        """The session this capture was streaming into has gone.

        The words it produced are banked, the box says what happened, and a
        replacement is dialled. The microphone keeps running: the client holds
        what is said meanwhile and sends it once the replacement is ready (see
        WebSocketClient.send_audio), so speaking through a reconnect loses
        nothing that the server would have heard.
        """
        self._capture_interrupted = True
        # The in-progress segment is included -- it is the server's best
        # reading of that audio, and the audio is not coming back.
        self._banked_text = self._capture_text()
        self._transcript.reset()
        payload_log.log_event("connection_lost", status=self.connection_status)
        self.logger.warning("Connection lost mid-capture (%s: %s); %d characters kept.",
                            self.connection_status, self.connection_detail,
                            len(self._banked_text))
        self._request_server()
        self.websocket_client.connect()
        self._show_interruption()

    def _resume_streaming(self):
        """A replacement session is ready; carry on with the same capture.

        Deliberately not _begin_streaming: that starts a capture over, and
        clearing the client's held audio (reset_eos) would throw away exactly
        what was said during the reconnect.
        """
        self._capture_interrupted = False
        # New session, new timeline: nothing of the old one may alias it.
        self._transcript.reset()
        payload_log.log_event("streaming_resumed")
        self.logger.info("Reconnected mid-capture; resuming.")
        self.capture_box.set_compact(self._ghosting)
        self.capture_box.set_text(self._capture_text())

    def _request_server(self):
        """Start the local server if it is ours to start and is not up.

        Dictating is as clear a request for a server as there is.
        """
        if (self.server_manager.manages_server()
                and self.server_manager.state != server_manager.STATE_RUNNING
                and not self.server_manager.is_busy()):
            self.server_manager.ensure_running()

    def on_capture_confirmed(self, text):
        if not self.is_capturing:
            return
        self._foreground_timer.stop()
        self._claim_capture_keys(False)
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

        The marker goes with it. It is deliberately kept up for the whole of
        the final pass -- the box spinning is exactly when someone wonders
        where the text is about to go -- and taken down here, one moment
        before it actually goes there.
        """
        self._hide_target_marker()
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
        text = text or self._capture_text()
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
        text = self._capture_text()
        self._dismiss_capture_box()
        QTimer.singleShot(PASTE_START_DELAY_MS, lambda: self._do_paste(text))

    def on_capture_cancelled(self):
        if not self.is_capturing:
            return
        self._foreground_timer.stop()
        self._claim_capture_keys(False)
        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = time.time() + 8.0
        payload_log.log_event("capture_cancelled")
        # Before the marker goes: the target still has the keyboard at this
        # point (a passive box never took it), which is what makes taking the
        # preview back possible at all.
        self._undo_live_typing()
        self._hide_target_marker()
        self._paste_pending = False
        if self._final_pass:
            self._final_pass.abandon()
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._schedule_on_demand_disconnect()
        self._sync_icon_state()
        self.write_to_history(self._capture_text(), status="CANCELLED")
        self.logger.debug("Capture cancelled.")

    def _schedule_on_demand_disconnect(self):
        if not self.settings.get("connect_on_demand", False):
            return
        # Delay disconnect slightly so the server can deliver final transcripts after EOS.
        self._disconnect_token += 1
        token = self._disconnect_token
        QTimer.singleShot(1200, lambda: self._on_demand_disconnect(token))

    def _on_demand_disconnect(self, token: int):
        """The delayed half of _schedule_on_demand_disconnect.

        Skipped when another capture has started in the meantime. The client
        redials the moment the server hangs up on END_OF_AUDIO, so a hotkey
        pressed within this delay finds a fresh "Ready" connection and starts
        streaming into it -- and disconnecting here would cut that capture off
        a second in, leaving the box on "Listening..." over a dead socket.
        That capture schedules its own disconnect when it ends.
        """
        if token != self._disconnect_token:
            return
        if self.is_capturing:
            self.logger.debug("On-demand disconnect skipped: a capture is running.")
            return
        self.websocket_client.disconnect()

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
        self.capture_box.set_text_color(
            self.settings.get("capture_text_color",
                              DEFAULT_SETTINGS["capture_text_color"]))
        self.capture_box.set_accent_color(
            self.settings.get("capture_accent_color",
                              DEFAULT_SETTINGS["capture_accent_color"]))
        # The marker takes the box's accent, so the thing pointing at the
        # target and the thing showing the transcript read as one tool rather
        # than as two overlays that happen to be up at once.
        self.target_overlay.set_accent_color(
            self.settings.get("capture_accent_color",
                              DEFAULT_SETTINGS["capture_accent_color"]))
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
            text = self._capture_text()
        text = text.strip()
        self.write_to_history(text, status="CONFIRMED")
        if not text:
            self.logger.debug("Nothing transcribed; skipping paste.")
            # There can still be a preview in the document even when the final
            # transcript came back empty, and leaving it there would be the
            # one outcome nobody could undo without knowing what to look for.
            self._undo_live_typing()
            return

        # Dictation happens a phrase at a time, and the next paste lands right
        # where this one left the caret, so without this the last word of one
        # capture and the first of the next run together. Trailing rather than
        # leading: a leading space would be wrong at the start of a line, and
        # would put the caret one character further from where the user is
        # about to keep typing. The history keeps the clean text -- the space
        # is a paste-time affordance, not part of what was said.
        text += " "

        # Before the clipboard: when live typing already put exactly this text
        # in the document, the whole delete-and-paste is a round trip back to
        # where the document already is -- and the one window in the capture
        # where the words exist nowhere but the clipboard. Keeping them is
        # both safer and cheaper. See live_type.finish_if_matches.
        if self.live_typer.finish_if_matches(text):
            self.logger.info(
                "The live-typed text is already the transcript; kept it in "
                "place rather than pasting over it.")
            return

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
            self._paste_unavailable("the target window belongs to WhisperType", text)
            return

        if not focus_window(hwnd):
            self._paste_unavailable(
                f"could not focus '{get_window_title(hwnd)}'", text)
            return

        # Focus is confirmed on the target, so the live-typed preview can be
        # taken back. It goes out before the finished text goes in: the final
        # pass re-reads the whole clip and usually punctuates it differently
        # from the running preview, so the two must not be concatenated.
        self._undo_live_typing()

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
            self._undo_live_typing()
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
        # A capture still open at quit must not read the disconnect below as
        # a dropped connection and dial a new one.
        self.is_capturing = False
        self._paste_pending = False
        self._foreground_timer.stop()
        self._claim_capture_keys(False)
        self._hide_target_marker()
        caret_target.shutdown()
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
        logging.getLogger("whispertype.app").error(
            "Another WhisperType instance is already running; exiting.")
        # A QApplication is needed for the message box, and it must be created
        # before any widget. This one is discarded with the process.
        QApplication(sys.argv)
        QMessageBox.warning(
            None, "WhisperType",
            "WhisperType is already running.\n\n"
            "Look for the tray icon near the clock. Running two copies breaks "
            "dictation: both open a capture box on the same hotkey and the "
            "paste lands in the wrong one.")
        sys.exit(0)

    app = WhisperTypeApp()
    app.run()

if __name__ == "__main__":
    main()
