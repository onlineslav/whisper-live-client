"""Work out where a dictated transcript is going to land.

WhisperType types into "whatever had focus when the hotkey fired", and until
the paste actually happens there is nothing on screen that says where that is.
The Capture Box opens next to the mouse, which is usually nowhere near the
caret, so a capture that is about to land in the wrong window looks exactly
like one that is about to land in the right one. This module finds the target
so it can be drawn on -- see target_overlay.py.

Windows has no single API that answers "where is the text caret". Three of
them answer part of it, and each covers a different set of applications:

  * GetGUIThreadInfo() reports a caret rectangle per GUI *thread*. It is
    cheap -- a struct copy, no cross-process call, never blocks -- and it
    keeps answering for a thread that has just lost the foreground, which is
    exactly the state the target is in while the Capture Box is up. It only
    knows about carets made with the classic CreateCaret() API, so it covers
    Notepad, the Win32 edit control, Explorer's rename box and most native
    apps, and knows nothing whatsoever about Chromium, Electron or UWP.

  * UI Automation's TextPattern reports the selection, and a collapsed
    selection is the caret. It covers anything with an accessibility
    implementation -- browsers, Electron, Office, UWP, WPF -- which is most
    of what anyone actually dictates into. It is a cross-process COM call
    into the target, so it takes as long as the target takes to answer, and
    an app that is busy or wedged can make it take a very long time.

  * The focused window's own rectangle always exists, and says which app at
    the very least. It is what is left when neither of the above knows
    anything.

They are tried in that order and the most precise answer wins.

Two things shape how the UIA half is used. First, GetFocusedElement() returns
whatever has focus *now*, so it has to run before the Capture Box takes the
foreground -- a moment later and the answer is our own read-only text area.
That means it runs synchronously on the GUI thread, once, at the top of a
capture. Second, because it can block, it is timed: one acquisition that
overruns UIA_BUDGET disables UI Automation for the rest of the session and
the ladder falls back to the two APIs that cannot stall. Degrading the marker
is a much better failure than freezing the app behind a wedged browser.

Everything after acquisition is refresh(), which is pure user32 and free: the
caret does not move while the user is dictating into a window they are not
typing in, so the job is to notice the window being dragged or closed, not to
re-derive the position.

ctypes note, the same one win_focus.py opens with: every function has explicit
argtypes and restype. The default is a 32-bit C int, which truncates handles
and pointers on a 64-bit build.
"""

import ctypes
import logging
import time
from collections import namedtuple
from ctypes import wintypes

logger = logging.getLogger("whispertype.target")

# How long one UI Automation acquisition may take before UIA is abandoned for
# the rest of the session. Generous, because it is not a latency budget -- the
# call is on the path to showing the Capture Box, so a slow one is already
# felt -- but a ceiling on how long a wedged target can hold the GUI thread.
UIA_BUDGET = 0.6

# Nothing useful is left of a caret this small; some apps report a zero-width
# or zero-height rectangle for a control that has focus but no insertion point.
MIN_CARET_PX = 2

# A "caret" taller than this is not a caret. Chromium reports the bounding box
# of the whole focused element for some collapsed selections, which for a
# <body> with a contenteditable is the entire page -- drawing a caret bar down
# the full height of the window is worse than drawing none. In physical
# pixels, so it has to clear a large type size on a display at 200%.
MAX_CARET_HEIGHT_PX = 220

# How much of its window a focused element has to fill before it is treated as
# being the window rather than a control inside it. Several applications --
# Obsidian and most Electron shells among them -- report the whole window as
# the focused element whenever the focus is not on a specific control, and an
# outline drawn tightly around that is a confident-looking answer to a
# question that was not actually answered. Demoted to the window rung
# instead, which says the same thing but says it honestly. Only applied when
# there is no caret: with one, the field is context around a known point and
# its size does not matter.
FIELD_IS_WINDOW_RATIO = 0.92


# Rectangles are (x, y, width, height) in *screen* pixels, or None.
#
#   caret   the insertion point itself, at character width and line height
#   field   the control the text is going into
#   window  the top-level window that control is in
#   label   what to call it, for the tray log and the overlay's fallback
#   source  which rung of the ladder answered: "caret", "field", "window" or
#           "none". The overlay draws a different thing for each, because the
#           difference between "here, exactly" and "somewhere in this window"
#           is the whole point of showing anything.
#   style   (font family, size in points) at the caret, or None. Only the
#           ghost text uses it, and only to look like it belongs where it is
#           standing -- see ghost_text.py.
Target = namedtuple("Target", "caret field window label source style")
# So the five-field construction sites elsewhere keep working.
Target.__new__.__defaults__ = (None,)

EMPTY = Target(None, None, None, "", "none", None)


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _user32.GetGUIThreadInfo.restype = wintypes.BOOL
    _user32.GetGUIThreadInfo.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]

    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    _user32.ClientToScreen.restype = wintypes.BOOL
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]

    _AVAILABLE = True
except (OSError, AttributeError):
    _AVAILABLE = False


def is_available() -> bool:
    return _AVAILABLE


def _rect_tuple(rect) -> tuple:
    """(x, y, w, h) from a Win32 RECT, which is left/top/right/bottom."""
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _sane_caret(rect) -> bool:
    """True if `rect` looks like an insertion point rather than a whole page."""
    if rect is None:
        return False
    _, _, width, height = rect
    return (width >= 0 and height >= MIN_CARET_PX
            and height <= MAX_CARET_HEIGHT_PX)


def _fills(inner, outer) -> bool:
    """True if `inner` is within a hair of being `outer` in both dimensions.

    See FIELD_IS_WINDOW_RATIO. Both dimensions have to qualify: a text area
    is routinely as wide as the window it is in and almost never as tall,
    which is exactly the case that must not be demoted.
    """
    if inner is None or outer is None or outer[2] <= 0 or outer[3] <= 0:
        return False
    return (inner[2] / outer[2] >= FIELD_IS_WINDOW_RATIO
            and inner[3] / outer[3] >= FIELD_IS_WINDOW_RATIO)


def window_title(hwnd) -> str:
    if not _AVAILABLE or not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def window_rect(hwnd):
    if not _AVAILABLE or not hwnd:
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return _rect_tuple(rect)


def _thread_of(hwnd) -> int:
    return _user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0


def _gui_thread_info(hwnd):
    """GetGUIThreadInfo for the thread owning `hwnd`, or None."""
    if not _AVAILABLE or not hwnd:
        return None
    thread = _thread_of(hwnd)
    if not thread:
        return None
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if not _user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        return None
    return info


def _caret_from_gui_thread(info):
    """The classic caret rectangle, in screen coordinates, or None.

    rcCaret is in the *client* coordinates of hwndCaret, so it has to be
    mapped out before it means anything on screen. A thread with no caret
    reports hwndCaret == 0 and a zeroed rectangle.
    """
    if info is None or not info.hwndCaret:
        return None
    rect = info.rcCaret
    if rect.right - rect.left < 0 or rect.bottom - rect.top < MIN_CARET_PX:
        return None
    origin = wintypes.POINT(rect.left, rect.top)
    if not _user32.ClientToScreen(info.hwndCaret, ctypes.byref(origin)):
        return None
    caret = (origin.x, origin.y,
             rect.right - rect.left, rect.bottom - rect.top)
    return caret if _sane_caret(caret) else None


# ---------------------------------------------------------------------------
# UI Automation, through raw COM.
#
# comtypes would make this shorter, but it is a dependency the project does
# not otherwise have and this is four interfaces' worth of one call each. The
# vtable slot numbers below are the method's position in the interface as
# declared in UIAutomationClient.idl, plus the three IUnknown slots that come
# first. They are fixed for the life of an interface -- COM interfaces are
# append-only by definition -- so they are safe to hard-code, but they are the
# one thing here that fails by crashing rather than by returning an error, so
# each is named after the method it calls.
# ---------------------------------------------------------------------------

_PVOID = ctypes.c_void_p

CLSID_CUIAutomation = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
IID_IUIAutomation = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"

# UIA_TextPatternId, from UIAutomationClient.h.
_UIA_TEXT_PATTERN = 10014
# TextUnit_Character, for ExpandToEnclosingUnit below.
_TEXT_UNIT_CHARACTER = 0

# IUnknown
_RELEASE = 2
# IUIAutomation
_GET_FOCUSED_ELEMENT = 8
# IUIAutomationElement
_ELEM_GET_CURRENT_PATTERN = 16
_ELEM_GET_CURRENT_NAME = 23
_ELEM_GET_CURRENT_BOUNDING_RECTANGLE = 43
# IUIAutomationTextPattern
_TEXT_GET_SELECTION = 5
# IUIAutomationTextRangeArray
_RANGES_GET_LENGTH = 3
_RANGES_GET_ELEMENT = 4
# IUIAutomationTextRange
_RANGE_CLONE = 3
_RANGE_EXPAND_TO_ENCLOSING_UNIT = 6
_RANGE_GET_ATTRIBUTE_VALUE = 9
_RANGE_GET_BOUNDING_RECTANGLES = 10

# Text attributes at the caret, for drawing ghost text that looks like it
# belongs in the document rather than pasted on top of it. Both are routinely
# unsupported -- an application that answers "mixed" or "not supported" hands
# back a reserved sentinel object rather than a value -- so both are strictly
# best effort and the caller has a fallback for each.
_UIA_FONT_NAME_ATTR = 40005
_UIA_FONT_SIZE_ATTR = 40006

# The VARIANT tags worth reading. Anything else (including the VT_UNKNOWN
# sentinels above) is ignored.
_VT_I4 = 3
_VT_R4 = 4
_VT_R8 = 5
_VT_BSTR = 8


class _VARIANT(ctypes.Structure):
    """Just enough VARIANT to read a string or a number out of one.

    The real thing is a large union; only the members below are ever touched,
    and the padding keeps the struct the size the callee expects to write into
    (24 bytes on x64, 16 on x86).
    """

    class _Value(ctypes.Union):
        _fields_ = [
            ("lVal", ctypes.c_long),
            ("fltVal", ctypes.c_float),
            ("dblVal", ctypes.c_double),
            ("bstrVal", ctypes.c_void_p),
            ("_pad", ctypes.c_byte * 16),
        ]

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("value", _Value),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


try:
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    _oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)

    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoInitializeEx.argtypes = [_PVOID, wintypes.DWORD]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), _PVOID, wintypes.DWORD,
        ctypes.POINTER(_GUID), ctypes.POINTER(_PVOID)]
    _ole32.CLSIDFromString.restype = ctypes.c_long
    _ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]

    _oleaut32.SysFreeString.restype = None
    _oleaut32.SysFreeString.argtypes = [_PVOID]
    _oleaut32.VariantClear.restype = ctypes.c_long
    _oleaut32.VariantClear.argtypes = [_PVOID]
    _oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
    _oleaut32.SafeArrayGetLBound.argtypes = [
        _PVOID, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    _oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
    _oleaut32.SafeArrayGetUBound.argtypes = [
        _PVOID, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    _oleaut32.SafeArrayAccessData.restype = ctypes.c_long
    _oleaut32.SafeArrayAccessData.argtypes = [_PVOID, ctypes.POINTER(_PVOID)]
    _oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long
    _oleaut32.SafeArrayUnaccessData.argtypes = [_PVOID]
    _oleaut32.SafeArrayDestroy.restype = ctypes.c_long
    _oleaut32.SafeArrayDestroy.argtypes = [_PVOID]

    _COM_AVAILABLE = True
except (OSError, AttributeError):
    _COM_AVAILABLE = False


def _guid(text: str) -> _GUID:
    out = _GUID()
    _ole32.CLSIDFromString(text, ctypes.byref(out))
    return out


def _out(value):
    """An out-parameter for _call().

    ctypes.pointer() rather than the usual byref(): byref returns an opaque
    CArgObject whose type cannot be used to declare a prototype, and _call
    below has to build one per signature. pointer() returns a real LP_x whose
    type is exactly the argtype wanted. It allocates where byref does not,
    which costs nothing at the handful of calls a second this module makes.
    """
    return ctypes.pointer(value)


def _call(this, slot: int, *args) -> int:
    """Invoke vtable slot `slot` on COM pointer `this`. Returns the HRESULT.

    restype is a plain long rather than ctypes.HRESULT so a failure comes back
    as a number to test. HRESULT makes ctypes raise, and half these calls fail
    as a matter of course -- an element with no TextPattern is the normal
    case, not an exception.
    """
    vtable = ctypes.cast(this, ctypes.POINTER(_PVOID))[0]
    slot_address = ctypes.cast(vtable + slot * ctypes.sizeof(_PVOID),
                               ctypes.POINTER(_PVOID))[0]
    # WINFUNCTYPE caches by signature, so the repeat cost of rebuilding the
    # prototype here is a dictionary lookup.
    prototype = ctypes.WINFUNCTYPE(
        ctypes.c_long, _PVOID, *(type(arg) for arg in args))
    return prototype(slot_address)(this, *args)


def _release(this):
    if this:
        _call(this, _RELEASE)


def _bstr(pointer) -> str:
    """Read and free a BSTR out-parameter."""
    if not pointer:
        return ""
    try:
        return ctypes.c_wchar_p(pointer).value or ""
    finally:
        _oleaut32.SysFreeString(pointer)


def _rects_from_safearray(array):
    """The (x, y, w, h) tuples in a SAFEARRAY of doubles, four per rectangle.

    GetBoundingRectangles hands back one flat array of left/top/width/height
    quadruples -- one per line the range spans. The array is destroyed here:
    it is an out-parameter the caller owns.
    """
    if not array:
        return []
    lower = ctypes.c_long()
    upper = ctypes.c_long()
    data = _PVOID()
    rects = []
    try:
        if _oleaut32.SafeArrayGetLBound(array, 1, ctypes.byref(lower)) < 0:
            return []
        if _oleaut32.SafeArrayGetUBound(array, 1, ctypes.byref(upper)) < 0:
            return []
        count = upper.value - lower.value + 1
        if count < 4:
            return []
        if _oleaut32.SafeArrayAccessData(array, ctypes.byref(data)) < 0:
            return []
        try:
            values = ctypes.cast(
                data, ctypes.POINTER(ctypes.c_double * count)).contents
            for start in range(0, count - 3, 4):
                left, top, width, height = values[start:start + 4]
                rects.append((int(round(left)), int(round(top)),
                              int(round(width)), int(round(height))))
        finally:
            _oleaut32.SafeArrayUnaccessData(array)
    finally:
        _oleaut32.SafeArrayDestroy(array)
    return rects


class _UIAutomation:
    """The bits of UI Automation this module needs, and nothing else.

    Created lazily on first use and disabled permanently the first time it
    misbehaves -- see UIA_BUDGET. `available` is the whole of its public
    state: once it goes False the ladder simply stops asking.
    """

    def __init__(self):
        self._client = None
        self.available = _COM_AVAILABLE

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        if not self.available:
            return False
        # COINIT_APARTMENTTHREADED. Qt has already initialised the GUI thread
        # as an STA for drag and drop, so this returns S_FALSE ("already
        # initialised, same mode") rather than doing anything -- which is
        # exactly what is wanted. RPC_E_CHANGED_MODE would mean somebody set
        # up an MTA here instead, and is equally fine to proceed on: the
        # apartment exists either way.
        _ole32.CoInitializeEx(None, 0x2)
        clsid = _guid(CLSID_CUIAutomation)
        iid = _guid(IID_IUIAutomation)
        client = _PVOID()
        # CLSCTX_INPROC_SERVER. The UIA client library runs in our process and
        # does the cross-process work itself.
        result = _ole32.CoCreateInstance(
            _out(clsid), None, 1, _out(iid), _out(client))
        if result < 0 or not client.value:
            logger.debug("UI Automation is unavailable (0x%08X); "
                         "falling back to the caret and window rectangles.",
                         result & 0xFFFFFFFF)
            self.available = False
            return False
        self._client = client
        return True

    def disable(self, reason: str):
        logger.warning("Disabling UI Automation for this session: %s.", reason)
        self.available = False
        self.shutdown()

    def shutdown(self):
        if self._client:
            _release(self._client)
            self._client = None

    def focused(self):
        """(caret, field, name, style) for the focused element; any may be None.

        Must be called while the target still *has* focus -- once the Capture
        Box is up, the focused element is our own text area.
        """
        if not self._ensure_client():
            return None, None, "", None

        element = _PVOID()
        if _call(self._client, _GET_FOCUSED_ELEMENT,
                 _out(element)) < 0 or not element.value:
            return None, None, "", None
        try:
            caret, style = self._caret(element)
            return caret, self._bounds(element), self._name(element), style
        finally:
            _release(element)

    def _name(self, element) -> str:
        name = _PVOID()
        if _call(element, _ELEM_GET_CURRENT_NAME, _out(name)) < 0:
            return ""
        return _bstr(name.value)

    def _bounds(self, element):
        rect = wintypes.RECT()
        if _call(element, _ELEM_GET_CURRENT_BOUNDING_RECTANGLE,
                 _out(rect)) < 0:
            return None
        bounds = _rect_tuple(rect)
        return bounds if bounds[2] > 0 and bounds[3] > 0 else None

    def _caret(self, element):
        """(insertion point, text style) inside `element`, from its TextPattern.

        The selection of a text control with nothing selected is a collapsed
        range, and a collapsed range's bounding rectangle is -- reasonably
        enough -- empty. So the range is cloned and stretched over the one
        character it sits on, which gives a rectangle at the right place with
        the line's height and a character's width. The clone matters: the
        range handed back by GetSelection is live, and expanding it in place
        would select that character in the user's document.
        """
        pattern = _PVOID()
        if _call(element, _ELEM_GET_CURRENT_PATTERN, ctypes.c_int(_UIA_TEXT_PATTERN),
                 _out(pattern)) < 0 or not pattern.value:
            return None, None
        try:
            ranges = _PVOID()
            if _call(pattern, _TEXT_GET_SELECTION,
                     _out(ranges)) < 0 or not ranges.value:
                return None, None
            try:
                length = ctypes.c_int()
                if _call(ranges, _RANGES_GET_LENGTH,
                         _out(length)) < 0 or length.value < 1:
                    return None, None
                selection = _PVOID()
                if _call(ranges, _RANGES_GET_ELEMENT, ctypes.c_int(0),
                         _out(selection)) < 0 or not selection.value:
                    return None, None
                try:
                    return self._range_rect(selection)
                finally:
                    _release(selection)
            finally:
                _release(ranges)
        finally:
            _release(pattern)

    def _range_rect(self, selection):
        """(rect, style) for the character the collapsed selection sits on."""
        clone = _PVOID()
        if _call(selection, _RANGE_CLONE,
                 _out(clone)) < 0 or not clone.value:
            return None, None
        try:
            _call(clone, _RANGE_EXPAND_TO_ENCLOSING_UNIT,
                  ctypes.c_int(_TEXT_UNIT_CHARACTER))
            # Read before the rectangles: both come from the same expanded
            # range, and the style is what makes ghost text drawn at that
            # rectangle look like it belongs there.
            style = self._range_style(clone)
            array = _PVOID()
            if _call(clone, _RANGE_GET_BOUNDING_RECTANGLES,
                     _out(array)) < 0:
                return None, style
            rects = _rects_from_safearray(array.value)
            if not rects:
                return None, style
            # The first line of the range. A range expanded over one character
            # is one line by construction, but a caret sitting on a wrap point
            # can report the end of one line and the start of the next.
            caret = rects[0]
            return (caret if _sane_caret(caret) else None), style
        finally:
            _release(clone)

    def _range_style(self, text_range):
        """(font family, size in points) for `text_range`, or None.

        Both halves are optional and either can come back missing: an
        application that has no opinion, or whose range spans more than one
        font, returns a reserved sentinel rather than a value. None means
        "draw it however you like", which is what ghost_text.py falls back to.
        """
        family = self._attribute(text_range, _UIA_FONT_NAME_ATTR)
        size = self._attribute(text_range, _UIA_FONT_SIZE_ATTR)
        if not family and not size:
            return None
        return (family or None, size or None)

    def _attribute(self, text_range, attribute_id: int):
        """One text attribute as a str or float, or None if it is not a value."""
        variant = _VARIANT()
        if _call(text_range, _RANGE_GET_ATTRIBUTE_VALUE,
                 ctypes.c_int(attribute_id), _out(variant)) < 0:
            return None
        try:
            tag = variant.vt
            if tag == _VT_BSTR:
                # Read, but do not free: VariantClear below owns the string.
                return ctypes.c_wchar_p(variant.value.bstrVal).value or None
            if tag == _VT_R8:
                return float(variant.value.dblVal)
            if tag == _VT_R4:
                return float(variant.value.fltVal)
            if tag == _VT_I4:
                return float(variant.value.lVal)
            # VT_UNKNOWN (the "not supported" / "mixed" sentinels), VT_EMPTY,
            # or anything else this does not need.
            return None
        finally:
            _oleaut32.VariantClear(_out(variant))


_uia = _UIAutomation()


def locate(hwnd, want_style: bool = False) -> Target:
    """Find where text sent to `hwnd` right now would land.

    Call this while `hwnd` still has the foreground -- at the top of a
    capture, before the Capture Box appears. Everything afterwards goes
    through refresh(), which cannot ask the target anything but also cannot
    stall.

    `want_style` asks for the font at the caret as well, which only UI
    Automation can answer. Without it the cheap rung short-circuits the whole
    COM path whenever it finds a caret, so the font comes back None -- fine
    for the marker, which does not draw text, and not fine for ghost text,
    which has to be set in the document's own face. It is a parameter rather
    than always-on because it turns a free lookup into a cross-process call.
    """
    if not _AVAILABLE or not hwnd or not _user32.IsWindow(hwnd):
        return EMPTY

    window = window_rect(hwnd)
    title = window_title(hwnd)

    # The cheap rung first, so a Win32 target never pays for a COM call.
    info = _gui_thread_info(hwnd)
    classic_caret = _caret_from_gui_thread(info)
    caret = classic_caret
    field = None
    name = ""
    style = None

    if (classic_caret is None or want_style) and _uia.available:
        started = time.monotonic()
        try:
            uia_caret, field, name, style = _uia.focused()
        except OSError:
            logger.exception("UI Automation raised while locating the caret.")
            uia_caret, field, name, style = None, None, "", None
        elapsed = time.monotonic() - started
        if elapsed > UIA_BUDGET:
            _uia.disable(f"an acquisition took {elapsed:.1f}s")
        # When both rungs answer, the caller's purpose decides which wins.
        # The classic caret is the real one the application blinks, so it is
        # the better *position*; UI Automation's is the character cell, whose
        # height is the line box -- which is the leading ghost text has to
        # advance by, and which a bare caret is often shorter than.
        if uia_caret is not None and (classic_caret is None or want_style):
            caret = uia_caret

    if field is None and info is not None and info.hwndFocus:
        # The focused control's own window rectangle. Only a real answer for
        # a control that *is* a window -- which is the Win32 case, and the
        # Win32 case is the one where UIA did not get a look in.
        field = window_rect(info.hwndFocus)

    # Last, so it catches a whole-window answer from either source above.
    if caret is None and _fills(field, window):
        logger.debug("The focused element fills its window; reporting the "
                     "window instead, which is all that was really found.")
        field = None

    label = " — ".join(part for part in (title, name) if part)

    if caret is not None:
        source = "caret"
    elif field is not None:
        source = "field"
    elif window is not None:
        source = "window"
    else:
        source = "none"

    target = Target(caret, field, window, label, source, style)
    logger.debug("Target for 0x%X: %s via %s (caret=%s field=%s style=%s)",
                 hwnd, label or "<untitled>", source, caret, field, style)
    return target


def refresh(target: Target, hwnd) -> Target:
    """Bring `target` up to date without asking the application anything.

    Three things can change while a capture is running, and none of them need
    a cross-process call to notice:

      * The window can be closed. Then there is no target and the overlay has
        to say so rather than keep pointing at a hole in the desktop.
      * The window can be moved or resized. The caret has not moved relative
        to it, so everything is carried along by the same delta -- which is
        what makes the marker stay put when the target window is dragged.
      * A classic caret can be re-reported, which is free to check and is
        occasionally better than what was cached (a Win32 app that scrolled
        under an autocomplete, say).

    What is deliberately *not* done is re-running UI Automation. Its answer
    would be about our own Capture Box by now, and it is the one call here
    that can block.
    """
    if not _AVAILABLE or not hwnd or not _user32.IsWindow(hwnd):
        return target._replace(source="lost")

    moved = window_rect(hwnd)
    if moved is None:
        return target._replace(source="lost")

    caret, field, window = target.caret, target.field, target.window
    if window is not None and moved[:2] != window[:2]:
        dx = moved[0] - window[0]
        dy = moved[1] - window[1]
        caret = _shifted(caret, dx, dy)
        field = _shifted(field, dx, dy)
    window = moved

    fresh = _caret_from_gui_thread(_gui_thread_info(hwnd))
    if fresh is not None:
        caret = fresh

    source = target.source
    if source == "lost":
        # The window came back -- a minimised target restored, most likely.
        source = "caret" if caret else ("field" if field else "window")
    return Target(caret, field, window, target.label, source, target.style)


def _shifted(rect, dx: int, dy: int):
    if rect is None:
        return None
    return (rect[0] + dx, rect[1] + dy, rect[2], rect[3])


def shutdown():
    """Release the UI Automation client. Safe to call more than once."""
    _uia.shutdown()
