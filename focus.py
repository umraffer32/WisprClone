"""Where a paste would land: foreground-window and focused-control checks
through Win32 and UI Automation."""

import ctypes
import ctypes.wintypes as wt
import logging

log = logging.getLogger("wisprclone")


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
                ("rcCaret", wt.RECT)]


def _gui_thread_info():
    """GetGUIThreadInfo for the foreground thread, or None if the call failed."""
    gti = _GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(gti)):
        return gti
    return None


def foreground_window():
    return ctypes.windll.user32.GetForegroundWindow()


def caret_visible():
    """True when the foreground window shows a system text caret - i.e. the
    paste almost certainly landed in a real text field. Apps that draw their
    own caret read as False, which errs toward showing the repaste offer."""
    gti = _gui_thread_info()
    return gti is not None and bool(gti.hwndCaret)


_TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
                     "ConsoleWindowClass"}             # conhost/cmd


def is_terminal():
    """Windows Terminal and conhost draw their own cursor and expose the
    buffer as a Document/Pane to UI Automation, so caret_visible() and
    focused_editable() both read False here even though a Ctrl+V into a
    console always lands at the input line - no read-only case to miss,
    unlike a webpage."""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(foreground_window(), buf, 256)
    return buf.value in _TERMINAL_CLASSES


_uia = None


def _get_uia():
    global _uia
    if _uia is None:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        _uia = comtypes.client.CreateObject(UIA.CUIAutomation,
                                            interface=UIA.IUIAutomation)
    return _uia


def focused_editable():
    """UI Automation check for apps that draw their own caret (Electron,
    Chromium): is the focused control an Edit field? Only Edit counts -
    Document would also match read-only web pages, and a wrong True here
    hides the repaste offer exactly when it's needed."""
    try:
        el = _get_uia().GetFocusedElement()
        ct = el.CurrentControlType
        log.debug("focused control type: %d", ct)
        return ct == 50004  # UIA_EditControlTypeId
    except Exception:
        log.debug("UIA focus check failed", exc_info=True)
        return False


def focused_text():
    """Full text of the focused control (TextPattern first, ValuePattern as
    the fallback), or None when it exposes neither - the caller's signal to
    fall back to the blind same-window heuristic. Probed 2026-08-25: every
    field Uriah dictates into (Claude desktop, Firefox chrome + web content,
    Gmail compose, Notepad) answers via TextPattern."""
    try:
        from comtypes.gen import UIAutomationClient as UIA
        el = _get_uia().GetFocusedElement()
        pat = el.GetCurrentPattern(10014)  # UIA_TextPatternId
        if pat:
            return pat.QueryInterface(
                UIA.IUIAutomationTextPattern).DocumentRange.GetText(-1)
        pat = el.GetCurrentPattern(10002)  # UIA_ValuePatternId
        if pat:
            return pat.QueryInterface(
                UIA.IUIAutomationValuePattern).CurrentValue
    except Exception:
        log.debug("UIA text read failed", exc_info=True)
    return None


# GUI_INMENUMODE | GUI_SYSTEMMENUMODE | GUI_POPUPMENUMODE (winuser.h)
_GUI_MENU_FLAGS = 0x0004 | 0x0008 | 0x0010
_MENU_CONTROL_TYPES = {50009, 50010, 50011}  # UIA Menu, MenuBar, MenuItem


def paste_blocked():
    """True while a popup menu owns input on the foreground thread - an
    injected Ctrl+V would go to the menu, not the text field, and a stray
    'v' can even activate a menu item. Native Win32 menus (Notepad,
    Explorer, Electron apps) run a modal loop that sets the menu-mode
    flags; Chromium and Firefox draw their own menu popups, which hold
    mouse capture (that's how they dismiss on an outside click) and report
    a menu-ish focused element via UI Automation."""
    gti = _gui_thread_info()
    if gti is not None and (gti.flags & _GUI_MENU_FLAGS or gti.hwndCapture):
        return True
    try:
        return _get_uia().GetFocusedElement().CurrentControlType in _MENU_CONTROL_TYPES
    except Exception:
        return False
