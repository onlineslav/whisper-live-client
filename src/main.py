import copy
import sys
import json
import os
import logging
import time
from datetime import datetime
from pynput.keyboard import Controller, Key
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QMessageBox
from PySide6.QtCore import Qt, QRectF, QTimer
from settings_window import SettingsWindow, DEFAULT_SETTINGS, APP_DATA_DIR, CONFIG_FILE
from hotkey_listener import HotkeyListener
from websocket_client import WebSocketClient
from audio_capture import AudioCapture
from capture_box import CaptureBox
from win_focus import get_foreground_window, focus_window

HISTORY_FILE = os.path.join(APP_DATA_DIR, "transcription_history.log")

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

class WhisperBoardApp:
    def __init__(self):
        self.logger = logging.getLogger("whisperboard.app")
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = 0.0
        self.connection_status = "Disconnected"
        self.keyboard = Controller()
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
        """Builds simple, high-contrast tray icons for each state."""
        def make_icon(base_color: str, overlay):
            size = 64
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QColor(base_color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(4, 4, size - 8, size - 8)
            overlay(painter, size)
            painter.end()
            return QIcon(pixmap)

        def draw_ready(painter: QPainter, size: int):
            pen = QPen(QColor("#ffffff"), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(int(size * 0.30), int(size * 0.55), int(size * 0.45), int(size * 0.72))
            painter.drawLine(int(size * 0.45), int(size * 0.72), int(size * 0.72), int(size * 0.38))

        def draw_connecting(painter: QPainter, size: int):
            pen = QPen(QColor("#ffffff"), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            rect = QRectF(size * 0.20, size * 0.20, size * 0.60, size * 0.60)
            painter.drawArc(rect, 30 * 16, 210 * 16)
            painter.drawArc(rect, -150 * 16, 210 * 16)

        def draw_recording(painter: QPainter, size: int):
            painter.setBrush(QColor("#ffffff"))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(int(size * 0.34), int(size * 0.34), int(size * 0.32), int(size * 0.32))

        def draw_error(painter: QPainter, size: int):
            pen = QPen(QColor("#ffffff"), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(int(size * 0.50), int(size * 0.28), int(size * 0.50), int(size * 0.60))
            painter.drawEllipse(int(size * 0.46), int(size * 0.65), int(size * 0.08), int(size * 0.08))

        return {
            "ready": make_icon("#2ecc71", draw_ready),
            "connecting": make_icon("#3498db", draw_connecting),
            "recording": make_icon("#e74c3c", draw_recording),
            "error": make_icon("#7f8c8d", draw_error),
        }

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
            # Record the target window now, before the Capture Box steals focus.
            self._prev_foreground_hwnd = get_foreground_window()
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

    def _begin_streaming(self):
        """Start audio streaming for a capture on a connected session."""
        # Empty text => the box shows its "Listening..." placeholder, while
        # toPlainText() stays "" so confirming without speaking pastes nothing.
        self.capture_box.set_text("")
        self.websocket_client.reset_eos()
        self.audio_capture.start_streaming()

    def on_capture_confirmed(self, text):
        if not self.is_capturing:
            return
        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = time.time() + 8.0
        # Hide the capture UI to return focus to the previous app.
        self.capture_box.hide()
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._schedule_on_demand_disconnect()
        self._sync_icon_state()
        self.logger.debug("Capture confirmed.")
        # processEvents() flushes the hide/focus-change events so the OS can
        # restore focus to the target window before the paste fires. _do_paste
        # reads the final box text, capturing any last-moment transcript.
        QApplication.processEvents()
        QTimer.singleShot(220, self._do_paste)

    def on_capture_cancelled(self):
        if not self.is_capturing:
            return
        self.is_capturing = False
        self._capture_waiting_for_connection = False
        self._post_capture_grace_until = time.time() + 8.0
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
        # Read the final transcription now (the box may have updated since
        # confirm), log it, and put it on the clipboard.
        text = self.capture_box.text_area.toPlainText().strip()
        self.write_to_history(text, status="CONFIRMED")
        if not text:
            self.logger.debug("Nothing transcribed; skipping paste.")
            return
        QApplication.clipboard().setText(text)
        QApplication.processEvents()

        # Restore focus to the window that was active before the Capture Box
        # opened, then simulate Ctrl+V so the text lands at its caret.
        hwnd = self._prev_foreground_hwnd
        if hwnd:
            if focus_window(hwnd):
                # Let Windows settle the foreground/focus change before keys.
                QApplication.processEvents()
            else:
                self.logger.warning(
                    "Could not restore focus to target window 0x%X; "
                    "paste may land in the wrong place.", hwnd)
        else:
            self.logger.warning("No target window recorded; cannot route paste.")

        with self.keyboard.pressed(Key.ctrl):
            self.keyboard.press('v')
            self.keyboard.release('v')
        self.logger.debug("Pasted %d chars via Ctrl+V.", len(text))

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
    app = WhisperBoardApp()
    app.run()

if __name__ == "__main__":
    main()
