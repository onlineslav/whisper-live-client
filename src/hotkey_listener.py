import logging
import threading
from pynput import keyboard
from PySide6.QtCore import QObject, Signal

class HotkeyListener(QObject):
    """
    Listens for a global hotkey press in a separate thread and emits a signal.
    """
    hotkey_activated = Signal()

    def __init__(self, hotkey_str):
        super().__init__()
        self.hotkey_str = hotkey_str
        self.logger = logging.getLogger("whisperboard.hotkey")
        self.listener_thread = None
        # Store listener so canonical uses same instance
        self._listener = None
        parsed = keyboard.HotKey.parse(self.hotkey_str)
        self.logger.info("Parsed hotkey '%s' as %s", self.hotkey_str, parsed)
        self.hotkey = keyboard.HotKey(parsed, self.on_activate)

    def on_activate(self):
        self.logger.debug("Hotkey %s activated.", self.hotkey_str)
        # This callback is executed in the listener thread,
        # so we emit a signal to communicate with the main GUI thread.
        self.hotkey_activated.emit()

    def run(self):
        """Starts the keyboard listener in a separate thread."""
        if self.listener_thread is None or not self.listener_thread.is_alive():
            self.listener_thread = threading.Thread(target=self._run_listener, daemon=True)
            self.listener_thread.start()
            self.logger.info("Hotkey listener started for '%s'.", self.hotkey_str)

    def _run_listener(self):
        with keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        ) as listener:
            self._listener = listener
            listener.join()

    def _on_press(self, key):
        if self._listener:
            self.hotkey.press(self._listener.canonical(key))

    def _on_release(self, key):
        if self._listener:
            self.hotkey.release(self._listener.canonical(key))



if __name__ == '__main__':
    # Example usage for testing
    from PySide6.QtWidgets import QApplication
    import sys

    class TestApp(QObject):
        def __init__(self):
            super().__init__()
            self.app = QApplication(sys.argv)
            
            # Hotkey to listen for, e.g., Ctrl+`
            self.hotkey = "ctrl+`"
            self.listener = HotkeyListener(self.hotkey)
            self.listener.hotkey_activated.connect(self.handle_activation)
            self.listener.run()
            
            print(f"Test application running. Press {self.hotkey} to trigger the signal.")
            
        def handle_activation(self):
            print("Signal received in the main thread! Application will quit now.")
            self.app.quit()
        
        def exec(self):
            sys.exit(self.app.exec())

    test_app = TestApp()
    test_app.exec()
