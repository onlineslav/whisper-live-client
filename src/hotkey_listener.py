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
        self.hotkey = keyboard.HotKey(
            keyboard.HotKey.parse(self.hotkey_str),
            self.on_activate
        )

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
        with keyboard.Listener(on_press=self.for_canonical(self.hotkey.press), on_release=self.for_canonical(self.hotkey.release)) as listener:
            listener.join()
    
    def for_canonical(self, f):
        return lambda k: f(self.listener.canonical(k))
    
    # This method is needed for pynput to work correctly
    @property
    def listener(self):
        if hasattr(keyboard.Listener, 'canonical'):
             # This is a bit of a hack to get the listener instance
             # for the canonical method.
            return keyboard.Listener(on_press=None)
        return None



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
