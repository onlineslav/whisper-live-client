import sys
import json
import os
import logging
import time
from datetime import datetime
from pynput.keyboard import Controller, Key
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QPen, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QMessageBox
from PySide6.QtCore import Qt, QRectF
from settings_window import SettingsWindow, DEFAULT_SETTINGS, CONFIG_FILE
from hotkey_listener import HotkeyListener
from websocket_client import WebSocketClient
from audio_capture import AudioCapture
from capture_box import CaptureBox

HISTORY_FILE = "transcription_history.log"

class WhisperBoardApp:
    def __init__(self):
        self.logger = logging.getLogger("whisperboard.app")
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.is_capturing = False
        self.connection_status = "Disconnected"
        self.keyboard = Controller()

        # Load settings
        self.load_settings()

        # UI Windows
        self.settings_window = None
        self.capture_box = CaptureBox()

        # Core Components
        self._init_tray_icon()
        self._init_hotkey_listener()
        self._init_websocket_client()
        self.audio_capture = AudioCapture()
        
        # Connect signals
        self.audio_capture.audio_chunk_ready.connect(self.websocket_client.send_audio)
        self.capture_box.confirmed.connect(self.on_capture_confirmed)
        self.capture_box.cancelled.connect(self.on_capture_cancelled)

    def _init_tray_icon(self):
        self.state_icons = self._build_state_icons()
        self.tray_icon = QSystemTrayIcon(self.app)
        self._set_tray_icon_state("connecting")
        self.menu = QMenu()
        self._create_menu()
        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.show()

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
            self._set_tray_icon_state("error")

    def _init_hotkey_listener(self):
        try:
            hotkey_str = self.settings["capture_hotkey"]
            
            # Validate the hotkey to ensure it's not just modifiers
            keys = set(part.strip().lower() for part in hotkey_str.split('+'))
            modifier_keys = {'ctrl', 'alt', 'shift', 'cmd'}
            if not keys or keys.issubset(modifier_keys):
                raise ValueError("Hotkey must include at least one non-modifier key (e.g., 'a', 'f1').")

            self.hotkey_listener = HotkeyListener(hotkey_str)
            self.hotkey_listener.hotkey_activated.connect(self.on_hotkey_activated)
            self.hotkey_listener.run()
        except Exception as e:
            self.logger.exception("Failed to initialize hotkey listener.")
            QMessageBox.critical(None, "Hotkey Error", f"Failed to register hotkey '{self.settings['capture_hotkey']}'. Please change it in the settings.\n\nError: {e}")

    def _init_websocket_client(self):
        self.websocket_client = WebSocketClient(
            self.settings["server_address"],
            self.settings.get("model", "tiny.en"),
        )
        self.websocket_client.connection_status_changed.connect(self.on_connection_status_changed)
        self.websocket_client.message_received.connect(self.on_message_received)
        self.websocket_client.connect()

    def load_settings(self):
        try:
            with open(CONFIG_FILE, "r") as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = DEFAULT_SETTINGS

    def on_connection_status_changed(self, status):
        self.connection_status = status
        self.status_action.setText(f"Status: {status}")
        self._sync_icon_state()
        self.logger.info("Connection status changed: %s", status)

    def on_message_received(self, message_str):
        if not self.is_capturing:
            return
        
        try:
            message = json.loads(message_str)
            if "segments" in message and message["segments"]:
                full_text = "".join(seg["text"] for seg in message["segments"])
                self.capture_box.set_text(full_text.strip())
        except json.JSONDecodeError:
            self.logger.warning("Received non-JSON message: %s", message_str)
    
    def on_hotkey_activated(self):
        if not self.is_capturing:
            self.is_capturing = True
            self.capture_box.set_text("Listening...")
            self.capture_box.show_at_cursor()
            self.audio_capture.start_streaming()
            self.websocket_client.reset_eos()
            self._sync_icon_state()
            self.logger.debug("Capture started via hotkey.")
        else:
            self.on_capture_confirmed(self.capture_box.text_label.text())

    def on_capture_confirmed(self, text):
        if not self.is_capturing:
            return
        self.is_capturing = False
        # Hide the capture UI to return focus to the previous app
        self.capture_box.hide()
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._sync_icon_state()
        self.logger.debug("Capture confirmed with text length %d", len(text))

        # Save to history log
        self.write_to_history(text, status="CONFIRMED")

        # Paste text at cursor
        QApplication.clipboard().setText(text)
        QApplication.processEvents()
        time.sleep(0.18)  # let focus return to the target app
        focused = QGuiApplication.focusWindow()
        self.logger.debug("Focused window before paste: %s", focused)
        with self.keyboard.pressed(Key.ctrl):
            self.keyboard.press('v')
            self.keyboard.release('v')
        time.sleep(0.02)
        self.logger.debug("Paste triggered via Ctrl+V.")

    def on_capture_cancelled(self):
        if not self.is_capturing:
            return
        self.is_capturing = False
        self.audio_capture.stop_streaming()
        self.websocket_client.send_eos()
        self._sync_icon_state()
        self.write_to_history(self.capture_box.text_label.text(), status="CANCELLED")
        self.logger.debug("Capture cancelled.")

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
        if self.settings_window is None:
            self.settings_window = SettingsWindow()
            self.settings_window.save_button.clicked.connect(lambda: None)
        self.settings_window.show()
        self.settings_window.activateWindow()

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
