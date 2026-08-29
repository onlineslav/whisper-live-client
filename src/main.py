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
from websocket_client import WebSocketClient
from audio_capture import AudioCapture
from capture_box import CaptureBox
import win_input
import payload_log
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

# Tray icon wordmark. "W" reads cleanly at the ~16px Windows renders the tray
# at; "WL" is legible from about 24px up and turns to mush below it.
TRAY_LETTER = "W"

# Windows asks for the tray icon at a handful of sizes depending on DPI and
# taskbar settings. Rendering each one rather than downscaling a single large
# bitmap keeps the letterform crisp — downscaled type blurs badly.
TRAY_ICON_SIZES = (16, 20, 24, 32, 48, 64)

TRAY_STATE_COLORS = {
    "ready": "#2ecc71",
    "connecting": "#3498db",
    "recording": "#e74c3c",
    "error": "#7f8c8d",
}

# Whisper is known to emit these strings on silence/noise. Drop any segment
# whose normalized text matches. WhisperLive issue #185 tracks the upstream bug.
HALLUCINATION_PHRASES = {
    "", ".", "you", "thank you", "thanks for watching",
    "thank you for watching", "thanks", "okay", "ok", "bye",
    "the end", "subscribe", "please subscribe",
    "thanks for watching the video", "thank you very much",
}


def _is_hallucination(text: str) -> bool:
    normalized = text.strip().lower().strip(".,!?\"' ")
    return normalized in HALLUCINATION_PHRASES


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
        self.connection_status = "Disconnected"
        # Win32 handle of the window that had focus before the Capture Box
        # appeared, so paste can be routed back to it (e.g. Notepad).
        self._prev_foreground_hwnd = None

        # Load settings
        self.load_settings()
        self._apply_launch_on_startup(self.settings.get("launch_on_startup", False))

        # UI Windows
        self.settings_window = None
        self.capture_box = CaptureBox()

        # Core Components
        self.audio_capture = AudioCapture()
        self._init_tray_icon()
        self._init_hotkey_listener()
        self._init_websocket_client()
        
        # Connect signals
        self.capture_box.confirmed.connect(self.on_capture_confirmed)
        self.capture_box.cancelled.connect(self.on_capture_cancelled)
        self.app.aboutToQuit.connect(self._shutdown)

    def _init_tray_icon(self):
        self.state_icons = self._build_state_icons()
        self.tray_icon = QSystemTrayIcon(self.app)
        self._set_tray_icon_state("connecting")
        self.menu = QMenu()
        self._create_menu()
        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.show()
        self._sync_icon_state()

    def _create_menu(self):
        self.status_action = QAction("Status: Disconnected")
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)
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
            self.tray_icon.setToolTip(f"WhisperBoard ({state.capitalize()})")

    def _sync_icon_state(self):
        if self.is_capturing:
            self._set_tray_icon_state("recording")
        elif self.connection_status == "Connected":
            self._set_tray_icon_state("ready")
        elif self.connection_status == "Connecting":
            self._set_tray_icon_state("connecting")
        else:
            if self.settings.get("connect_on_demand", False):
                self._set_tray_icon_state("ready")
            else:
                self._set_tray_icon_state("error")

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
                previous_client.disconnect()
            except Exception:
                self.logger.exception("Failed to disconnect previous WebSocket client.")

        self.websocket_client = WebSocketClient(
            self.settings["server_address"],
            self.settings.get("model", "distil-small.en"),
            sample_rate=self.audio_capture.rate,
            channels=self.audio_capture.channels,
            audio_format=self.audio_capture.audio_format,
        )
        self.websocket_client.connection_status_changed.connect(self.on_connection_status_changed)
        self.websocket_client.message_received.connect(self.on_message_received)
        self.audio_capture.audio_chunk_ready.connect(self.websocket_client.send_audio)
        if not self.settings.get("connect_on_demand", False):
            self.websocket_client.connect()

    def load_settings(self):
        try:
            with open(CONFIG_FILE, "r") as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        # Ensure new defaults exist
        if "connect_on_demand" not in self.settings:
            self.settings["connect_on_demand"] = DEFAULT_SETTINGS.get("connect_on_demand", False)
        self.settings["capture_hotkey"] = self._normalize_hotkey_string(
            self.settings.get("capture_hotkey", DEFAULT_SETTINGS["capture_hotkey"])
        )

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
                t = t.strip("<>")
                parts.append(t)
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

    def on_connection_status_changed(self, status):
        self.connection_status = status
        status_label = status
        if status == "Disconnected" and self.settings.get("connect_on_demand", False):
            status_label = "Disconnected (on-demand)"
        self.status_action.setText(f"Status: {status_label}")

        if status == "Connected" and self.is_capturing and self._capture_waiting_for_connection:
            self._capture_waiting_for_connection = False
            self._begin_streaming()

        self._sync_icon_state()
        self.logger.info("Connection status changed: %s", status)

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
            full_text = ""
            if isinstance(message, dict):
                if message.get("segments"):
                    kept = [
                        seg.get("text", "")
                        for seg in message["segments"]
                        if isinstance(seg, dict) and not _is_hallucination(seg.get("text", ""))
                    ]
                    full_text = "".join(kept)
                elif "text" in message:
                    text = str(message.get("text", ""))
                    full_text = "" if _is_hallucination(text) else text
                if not full_text and isinstance(message.get("segment"), dict):
                    seg_text = str(message["segment"].get("text", ""))
                    full_text = "" if _is_hallucination(seg_text) else seg_text

            if full_text:
                self.capture_box.set_text(full_text.strip())
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
        if not self.is_capturing:
            self.is_capturing = True
            self._capture_waiting_for_connection = False
            payload_log.log_event("capture_start")
            # Record the target window now, before the Capture Box steals focus.
            self._record_paste_target()
            self.capture_box.show_at_cursor()
            if self.connection_status == "Connected":
                self._begin_streaming()
            else:
                # Not connected yet — only happens before the initial connection
                # finishes warming up, or in connect-on-demand mode. Wait for it;
                # on_connection_status_changed() starts streaming once ready.
                self._capture_waiting_for_connection = True
                self.capture_box.set_text("Connecting...")
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
        # Hide the capture UI to return focus to the previous app.
        self.capture_box.hide()
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._schedule_on_demand_disconnect()
        self._sync_icon_state()
        self.logger.debug("Capture confirmed.")
        # processEvents() flushes the hide/focus-change events so the OS can
        # start handing the foreground back. _do_paste reads the final box text,
        # capturing any last-moment transcript, then paces the rest itself.
        QApplication.processEvents()
        QTimer.singleShot(PASTE_START_DELAY_MS, self._do_paste)

    def on_capture_cancelled(self):
        if not self.is_capturing:
            return
        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = time.time() + 8.0
        payload_log.log_event("capture_cancelled")
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

    def on_settings_saved(self, updated_settings):
        """Apply updated settings from the Settings window."""
        self.settings = updated_settings
        self.settings["capture_hotkey"] = self._normalize_hotkey_string(self.settings.get("capture_hotkey", ""))
        self._apply_launch_on_startup(self.settings.get("launch_on_startup", False))
        self._start_hotkey_listener(self.settings["capture_hotkey"])
        self._init_websocket_client()
        self._sync_icon_state()

    def _do_paste(self):
        """Put the transcript on the clipboard and paste it into the target.

        Split across timer hops rather than run straight through: each stage
        gives Windows and the target application a chance to process the one
        before it, and returning to the event loop in between keeps the app
        responsive while the foreground changes hands.
        """
        # Read the final transcription now -- the box may have updated between
        # confirm and here -- and log it before anything else can fail.
        text = self.capture_box.text_area.toPlainText().strip()
        self.write_to_history(text, status="CONFIRMED")
        if not text:
            self.logger.debug("Nothing transcribed; skipping paste.")
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
        self.audio_capture.shutdown()
        if getattr(self, "websocket_client", None):
            self.websocket_client.disconnect()

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
