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
from ctypes import wintypes

_SW_RESTORE = 9

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []

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


def focus_window(hwnd) -> bool:
    """Bring `hwnd` to the foreground. Returns True if it ended up foreground.

    Works even when our process no longer owns the foreground (e.g. just
    after our own overlay was hidden), via the AttachThreadInput workaround.
    """
    if not _AVAILABLE or not hwnd or not _user32.IsWindow(hwnd):
        return False

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
    finally:
        for thread in attached:
            _user32.AttachThreadInput(current, thread, False)

    return _user32.GetForegroundWindow() == hwnd
