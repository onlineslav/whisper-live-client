"""Windows focus helpers.

Putting text into "whatever app was active" means simulating Ctrl+V (or
typing) into that app — which only works if the app has the foreground when
the keystroke fires. Restoring the foreground reliably is the hard part:

  * SetForegroundWindow() is denied by Windows unless the caller currently
    owns the foreground. Once our own overlay is hidden we no longer do, so
    a bare call silently fails. The fix is the long-standing AttachThreadInput
    workaround — temporarily linking our input queue to the source/target
    threads makes the foreground change permitted.

  * ctypes' default return/argument type is a 32-bit C int, which truncates
    64-bit window handles. Every function below has explicit argtypes/restype.
"""

import ctypes
import time
from ctypes import wintypes

_SW_RESTORE = 9

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []

    _user32.SetActiveWindow.restype = wintypes.HWND
    _user32.SetActiveWindow.argtypes = [wintypes.HWND]

    _user32.SetFocus.restype = wintypes.HWND
    _user32.SetFocus.argtypes = [wintypes.HWND]

    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.GetCurrentProcessId.argtypes = []

    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]

    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]

    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]

    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]

    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]

    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.AttachThreadInput.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.BOOL
    ]

    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentThreadId.argtypes = []

    _AVAILABLE = True
except (OSError, AttributeError):
    _AVAILABLE = False


def get_foreground_window():
    """Return the handle of the current foreground window, or None."""
    if not _AVAILABLE:
        return None
    return _user32.GetForegroundWindow() or None


def is_window(hwnd) -> bool:
    """True if `hwnd` still refers to a live window."""
    if not _AVAILABLE or not hwnd:
        return False
    return bool(_user32.IsWindow(hwnd))


def get_window_title(hwnd) -> str:
    """Title text of `hwnd`, or "" if it has none."""
    if not _AVAILABLE or not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def is_own_window(hwnd) -> bool:
    """True if `hwnd` belongs to this process.

    Pasting into one of our own windows is always a mistake: the Capture Box
    is a read-only text area, so a Ctrl+V aimed at it disappears without any
    visible sign that anything went wrong.
    """
    if not _AVAILABLE or not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _try_focus(hwnd) -> None:
    """One attempt at making `hwnd` the foreground window."""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)

    current = _kernel32.GetCurrentThreadId()
    foreground = _user32.GetForegroundWindow()
    fg_thread = (
        _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    )
    target_thread = _user32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    try:
        for thread in {fg_thread, target_thread}:
            if thread and thread != current:
                if _user32.AttachThreadInput(current, thread, True):
                    attached.append(thread)
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        # While the input queues are attached, activation and keyboard focus
        # can be set directly. SetForegroundWindow alone leaves some apps
        # foreground-but-unfocused, which swallows the first keystroke.
        _user32.SetActiveWindow(hwnd)
        _user32.SetFocus(hwnd)
    finally:
        for thread in attached:
            _user32.AttachThreadInput(current, thread, False)


def focus_window(hwnd, attempts: int = 3, settle: float = 0.03) -> bool:
    """Bring `hwnd` to the foreground. Returns True if it ended up foreground.

    Works even when our process no longer owns the foreground (e.g. just
    after our own overlay was hidden), via the AttachThreadInput workaround.

    The foreground change is not always immediate -- the shell can be busy, and
    a window that is still animating its own activation may bounce back -- so
    the result is polled and the attempt is repeated rather than trusted once.
    """
    if not _AVAILABLE or not hwnd or not _user32.IsWindow(hwnd):
        return False

    for attempt in range(attempts):
        if _user32.GetForegroundWindow() == hwnd:
            return True
        _try_focus(hwnd)
        # Poll rather than sleeping a flat interval: the common case settles
        # within a few milliseconds and should not pay the full wait.
        deadline = time.monotonic() + settle * (attempt + 1)
        while time.monotonic() < deadline:
            if _user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.005)

    return _user32.GetForegroundWindow() == hwnd
