import ctypes
import logging
import sys
import threading
from pynput import keyboard
from PySide6.QtCore import QObject, Signal

# Virtual-key codes for the two keys a running capture claims for itself.
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

# The modifiers that hand the key back to the application underneath. Enter on
# its own confirms; Shift+Enter is a newline the user meant to type, and
# Ctrl+Enter and friends are the target's own shortcuts. Probed with
# GetAsyncKeyState rather than tracked, because the hook thread has no state
# of its own and this costs a fraction of a microsecond.
MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, LWin, RWin

# WH_KEYBOARD_LL message ids for a key going down. The two "sys" variants are
# the same press with Alt held, which is not a press this claims -- but the
# release still has to be matched, so both ids are recognised.
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104


class HotkeyListener(QObject):
    """
    Listens for a global hotkey press in a separate thread and emits a signal.

    It also claims Enter and Escape from the desktop for the length of a
    capture that cannot take the keyboard for itself. A capture typing into
    the target application has to leave the keyboard where it is -- see
    CaptureBox.set_passive -- and a box without the keyboard never sees Enter,
    which is the key the whole interaction is documented to end with.

    Claiming the keys here rather than taking the focus back is deliberate.
    Focus is what live_type.py's one safety check depends on: it confirms its
    text really arrived by watching the target's caret move, and a window that
    is not in front does not show a caret at all. Text goes in by posted
    message and lands whether the target is in front or not -- measured -- so
    focus buys nothing there and costs the check. Two keys are cheaper.
    """
    hotkey_activated = Signal()
    # Enter and Escape, while claimed. Emitted from the hook thread, so both
    # are delivered to the GUI thread by Qt's queued connection -- the same
    # arrangement hotkey_activated has always used.
    confirm_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, hotkey_str):
        super().__init__()
        self.hotkey_str = hotkey_str
        self.logger = logging.getLogger("whispertype.hotkey")
        self.listener_thread = None
        # Store listener so canonical uses same instance
        self._listener = None
        # Whether Enter and Escape currently belong to the capture. A plain
        # bool: written from the GUI thread and read from the hook thread, and
        # a bool assignment needs no lock to be seen whole.
        self._claiming = False
        # The key codes swallowed on the way down, so their release is
        # swallowed too even if the claim is dropped in between. A key-up with
        # no key-down is the kind of thing that leaves a key stuck down as far
        # as the receiving application is concerned.
        self._swallowed = set()
        parsed = keyboard.HotKey.parse(self.hotkey_str)
        self.logger.info("Parsed hotkey '%s' as %s", self.hotkey_str, parsed)
        self.hotkey = keyboard.HotKey(parsed, self.on_activate)

    def on_activate(self):
        self.logger.debug("Hotkey %s activated.", self.hotkey_str)
        # This callback is executed in the listener thread,
        # so we emit a signal to communicate with the main GUI thread.
        self.hotkey_activated.emit()

    def claim_capture_keys(self, claimed: bool):
        """Take Enter and Escape from the desktop, or give them back.

        Only ever on while a capture is up that cannot take the keyboard
        itself, and off at every other moment -- these are two keys the rest
        of the desktop very much wants.
        """
        claimed = bool(claimed)
        if claimed == self._claiming:
            return
        self._claiming = claimed
        # _swallowed is deliberately not cleared here. Confirming releases the
        # claim on the way down, while the user still has the key held, so the
        # release that follows arrives with the claim already gone -- and a
        # key-up whose key-down was swallowed has to be swallowed too, or the
        # document gets a bare Enter out of nowhere.
        self.logger.debug("Capture keys %s.",
                          "claimed" if claimed else "released")

    def run(self):
        """Starts the keyboard listener in a separate thread."""
        if self.listener_thread is None or not self.listener_thread.is_alive():
            self.listener_thread = threading.Thread(target=self._run_listener, daemon=True)
            self.listener_thread.start()
            self.logger.info("Hotkey listener started for '%s'.", self.hotkey_str)

    def _run_listener(self):
        # The filter is a Windows-only hook point; elsewhere the listener runs
        # exactly as it always did, without the claim.
        options = ({"win32_event_filter": self._event_filter}
                   if sys.platform == "win32" else {})
        with keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            **options,
        ) as listener:
            self._listener = listener
            listener.join()

    def _event_filter(self, msg, data):
        """Swallow Enter and Escape while a passive capture owns them.

        Runs inside the low-level keyboard hook, on the listener's thread and
        synchronously with the keystroke, so it does the least it can: read
        two integers, ask Windows for the modifier state, emit a signal.
        suppress_event() raises, which is how pynput's hook returns non-zero
        and stops the key reaching the application underneath.
        """
        if self._listener is None:
            return True
        vk = data.vkCode
        if vk not in (VK_RETURN, VK_ESCAPE):
            return True

        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if not down:
            # A release is answered by what happened to its press, not by the
            # claim: the claim is dropped the moment Enter confirms, which is
            # while the key is still held.
            if vk not in self._swallowed:
                return True
            self._swallowed.discard(vk)
            self._listener.suppress_event()

        if not self._claiming:
            return True
        # Injected events are claimed too. A remapper -- AutoHotkey, a
        # programmable keyboard's driver -- delivers a real Enter as an
        # injected one, and refusing those would leave the box unanswerable on
        # exactly the desktops most likely to have one. Nothing this app
        # injects is Enter or Escape, so there is no loop to close.
        if self._modifier_held():
            # Shift+Enter and the like belong to the document.
            return True
        self._swallowed.add(vk)
        if vk == VK_RETURN:
            self.confirm_requested.emit()
        else:
            self.cancel_requested.emit()
        self._listener.suppress_event()

    @staticmethod
    def _modifier_held() -> bool:
        state = ctypes.windll.user32.GetAsyncKeyState
        return any(state(vk) & 0x8000 for vk in MODIFIER_VKS)

    def _on_press(self, key):
        if self._listener:
            self.hotkey.press(self._listener.canonical(key))

    def _on_release(self, key):
        if self._listener:
            self.hotkey.release(self._listener.canonical(key))

    def stop(self):
        """Stop listening and wait for the listener thread to finish."""
        self._claiming = False
        self._swallowed.clear()
        if self._listener:
            self._listener.stop()
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=1)
        self._listener = None



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
