"""Windows clipboard and synthetic-keystroke helpers for the paste step.

Pasting the transcript into "whatever app was active" is the one part of
WhisperType that has to work through the OS rather than through Qt, and each
of the pieces below exists because the obvious approach fails in practice:

  * The clipboard is written as a plain CF_UNICODETEXT block rather than via
    QClipboard. Qt registers a *delayed-rendering* data object: the bytes are
    only produced when the pasting app asks for them, which makes the paste
    depend on our event loop being responsive at that instant and on Qt still
    owning the clipboard. A flat HGLOBAL is owned by the system, so the text
    survives regardless of what this process is doing.

  * OpenClipboard() routinely fails with ERROR_ACCESS_DENIED because clipboard
    managers, Office and browsers hold it open for a few milliseconds at a
    time. Every clipboard call retries.

  * Keystrokes go through SendInput directly instead of pynput so the return
    value can be checked. SendInput reports how many events it actually
    injected, and injection into a higher-integrity (elevated) window is
    blocked by UIPI and silently returns 0 -- the difference between "pasted"
    and "did nothing" is only visible here.

  * Modifiers the user is physically holding are released first. The capture
    hotkey is a chord (Ctrl+backtick by default), so on the hotkey-to-confirm
    path Ctrl and friends are often still down; a stray Shift or Alt turns the
    injected Ctrl+V into Ctrl+Shift+V or Ctrl+Alt+V, which most apps treat as
    something else entirely or ignore.
"""

import ctypes
import logging
import time
from ctypes import wintypes

logger = logging.getLogger("whispertype.input")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_V = 0x56

# Left/right halves are probed separately: GetAsyncKeyState(VK_CONTROL) reports
# the merged state, but a keyup has to name the side that is actually down.
MODIFIER_KEYS = {
    "ctrl": (0xA2, 0xA3),    # VK_LCONTROL, VK_RCONTROL
    "shift": (0xA0, 0xA1),   # VK_LSHIFT, VK_RSHIFT
    "alt": (0xA4, 0xA5),     # VK_LMENU, VK_RMENU
    "win": (0x5B, 0x5C),     # VK_LWIN, VK_RWIN
}
# Right Alt/Ctrl and the Win keys are "extended" keys; their injected events
# need the flag or the target sees the left-hand key instead.
EXTENDED_VKS = {0xA3, 0xA5, 0x5B, 0x5C}

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # All three members are declared even though only ki is used: the union's
    # size is what SendInput validates cbSize against, and MOUSEINPUT is the
    # largest of them.
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardData.argtypes = [wintypes.UINT]

    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    _user32.SendInput.restype = wintypes.UINT
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.MapVirtualKeyW.restype = wintypes.UINT
    _user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    _AVAILABLE = True
except (OSError, AttributeError):
    _AVAILABLE = False


def is_available() -> bool:
    return _AVAILABLE


def _open_clipboard(attempts: int = 12, delay: float = 0.02) -> bool:
    """OpenClipboard with retries; another process holding it is routine."""
    for _ in range(attempts):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def set_clipboard_text(text: str) -> bool:
    """Put `text` on the clipboard as real CF_UNICODETEXT bytes."""
    if not _AVAILABLE:
        return False
    if not _open_clipboard():
        logger.warning("Could not open the clipboard (held by another app).")
        return False

    handle = None
    try:
        _user32.EmptyClipboard()
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)  # includes the terminating NUL
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            logger.warning("GlobalAlloc failed for %d bytes.", size)
            return False
        target = _kernel32.GlobalLock(handle)
        if not target:
            logger.warning("GlobalLock failed.")
            return False
        try:
            ctypes.memmove(target, buffer, size)
        finally:
            _kernel32.GlobalUnlock(handle)

        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            logger.warning("SetClipboardData failed (error %d).",
                           ctypes.get_last_error())
            return False
        # Ownership passed to the system; freeing it now would corrupt it.
        handle = None
        return True
    finally:
        if handle:
            _kernel32.GlobalFree(handle)
        _user32.CloseClipboard()


def get_clipboard_text():
    """Read CF_UNICODETEXT back, or None if it is absent/unreadable."""
    if not _AVAILABLE or not _open_clipboard():
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _key_event(vk: int, up: bool) -> _INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = _INPUT(type=INPUT_KEYBOARD)
    event.u.ki = _KEYBDINPUT(
        wVk=vk,
        # Scan code as well as virtual key: apps that read raw scan codes
        # (games, remote-desktop clients, some terminals) ignore vk-only input.
        wScan=_user32.MapVirtualKeyW(vk, 0),
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return event


def _send(events) -> bool:
    """Inject `events` as one atomic batch. False if Windows refused."""
    if not events:
        return True
    array = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    if sent != len(events):
        logger.warning(
            "SendInput injected %d of %d events (error %d). The target window "
            "is most likely running elevated, which blocks input from this "
            "process.", sent, len(events), ctypes.get_last_error())
        return False
    return True


def held_modifiers():
    """Names of modifier keys the user is physically holding right now."""
    if not _AVAILABLE:
        return []
    held = []
    for name, vks in MODIFIER_KEYS.items():
        if any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
            held.append(name)
    return held


def release_modifiers():
    """Release any held Ctrl/Shift/Alt/Win. Returns what was released.

    Injecting a keyup for a key the user is still holding is safe: the physical
    release that follows is just a second keyup, which apps ignore.
    """
    if not _AVAILABLE:
        return []

    events = []
    released = []
    for name, vks in MODIFIER_KEYS.items():
        for vk in vks:
            if _user32.GetAsyncKeyState(vk) & 0x8000:
                events.append(_key_event(vk, up=True))
                released.append(name)

    if not events:
        return []

    # Releasing a Windows key that went down with nothing pressed in between
    # pops the Start menu. Tapping Ctrl first makes the shell treat it as a
    # consumed chord, which is the standard way to suppress that.
    if "win" in released:
        events.insert(0, _key_event(VK_CONTROL, up=False))
        events.insert(1, _key_event(VK_CONTROL, up=True))

    _send(events)
    return released


def send_ctrl_v() -> bool:
    """Inject Ctrl+V into whatever window currently has focus."""
    if not _AVAILABLE:
        return False
    return _send([
        _key_event(VK_CONTROL, up=False),
        _key_event(VK_V, up=False),
        _key_event(VK_V, up=True),
        _key_event(VK_CONTROL, up=True),
    ])


def type_text(text: str) -> bool:
    """Type `text` as literal Unicode input, bypassing the clipboard.

    The fallback for targets that ignore Ctrl+V. KEYEVENTF_UNICODE carries the
    character itself rather than a key, so it is layout-independent.
    """
    if not _AVAILABLE or not text:
        return False

    events = []
    for char in text:
        point = ord(char)
        if point <= 0xFFFF:
            codes = [point]
        else:
            # Characters outside the BMP go as their UTF-16 surrogate pair.
            codes = [0xD800 + ((point - 0x10000) >> 10),
                     0xDC00 + ((point - 0x10000) & 0x3FF)]
        for code in codes:
            for up in (False, True):
                event = _INPUT(type=INPUT_KEYBOARD)
                event.u.ki = _KEYBDINPUT(
                    wVk=0, wScan=code,
                    dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                    time=0, dwExtraInfo=0)
                events.append(event)

    # Long transcripts are chunked: a very large SendInput batch can overrun
    # the target's input queue and lose the tail.
    for start in range(0, len(events), 200):
        if not _send(events[start:start + 200]):
            return False
        time.sleep(0.001)
    return True
