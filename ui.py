"""Pill overlay (tk, main thread only) and system tray (pystray, own thread)."""

import collections
import ctypes
import ctypes.wintypes as wt
import logging
import math
import os
import tkinter as tk

import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("wisprclone")

_BG = "#1e1e28"
_BAR = "#5ac8fa"
_BAR_RGB = (90, 200, 250)       # bright center of the bar gradient, and the glow
_BAR_EDGE_RGB = (58, 118, 240)  # deeper blue the gradient falls to at the edges
_ERR = "#c04040"
_ALPHA = 0.65     # whole-pill translucency, Wispr-style (lower = more see-through)
_W, _H, _RADIUS = 51, 20, 10  # ~20% down from 64x24x12 (2026-09-02); radius
                              # stays exactly half the height, same capsule
                              # ratio as before
_PAD = 10  # transparent margin around the pill; the recording glow lives here
_W_RESULT = 86  # wider: room for the checkmark, countdown, and dismiss X
_NBARS = 10
_S = 4  # supersample factor: draw big, Lanczos down for antialiased edges

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
GA_ROOT = 2
ULW_ALPHA = 2
AC_SRC_ALPHA = 1

_user32 = ctypes.windll.user32
_user32.GetAncestor.restype = ctypes.c_void_p
_user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
_user32.GetWindowLongPtrW.restype = ctypes.c_longlong
_user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
_user32.SetWindowLongPtrW.restype = ctypes.c_longlong
_user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong)
_user32.GetDC.restype = ctypes.c_void_p
_user32.GetDC.argtypes = (ctypes.c_void_p,)
_user32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_user32.UpdateLayeredWindow.argtypes = (
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD)

_gdi32 = ctypes.windll.gdi32
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
_gdi32.CreateDIBSection.restype = ctypes.c_void_p
_gdi32.CreateDIBSection.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
_gdi32.DeleteDC.argtypes = (ctypes.c_void_p,)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class Pill:
    def __init__(self, root, on_result_click=None, on_dismiss=None, pos_file=None):
        self.root = root
        self.on_result_click = on_result_click
        self.on_dismiss = on_dismiss
        self.state = "hidden"
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self._w = _W
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self._cx = sw // 2             # pill center x; dragging moves it
        self._y = sh - _H - 60 - _PAD  # window top y
        self._pos_file = pos_file
        self._load_pos(sw, sh)
        root.geometry(f"{_W + 2 * _PAD}x{_H + 2 * _PAD}"
                      f"+{self._cx - (_W + 2 * _PAD) // 2}+{self._y}")
        # NOACTIVATE means clicks and drags never steal focus from the app
        # the user wants to paste into - the whole point of the feature
        root.bind("<Button-1>", self._press)
        root.bind("<B1-Motion>", self._drag)
        root.bind("<ButtonRelease-1>", self._release)
        root.bind("<Motion>", self._motion)
        root.bind("<Leave>", self._leave)
        # sized for the pill's ~20% shrink (2026-09-02); tk font sizes are
        # points, PIL wants pixels (96dpi: 8pt=11px, 9pt=12px originally)
        self._font = ImageFont.truetype("segoeui.ttf", 9 * _S)
        self._font_count = ImageFont.truetype("segoeui.ttf", 10 * _S)
        self._hwnd = None
        self._last_sig = None
        self.levels = collections.deque([0.0] * _NBARS, maxlen=_NBARS)
        self._glow = 0.0   # halo intensity this frame, driven by _pulse
        self._pulse = 0.0  # phase of the breathing cycle, advances while recording
        self.visible = False
        self.flash_until = 0
        self._dismiss_box = None  # (x0, y0, x1, y1) hit-box, result state only
        self._check_box = None
        self._x_hover = False
        self._x_dismissing = False  # click-flash in progress, then hide
        self._check_hover = False
        self._check_flashing = False  # click-flash in progress, then repaste
        self._press_xy = None   # screen coords where the button went down
        self._press_win = None  # window origin at that moment
        self._dragged = False   # past the click-vs-drag threshold this press
        root.withdraw()

    def _load_pos(self, sw, sh):
        try:
            if self._pos_file and os.path.exists(self._pos_file):
                cx, y = map(int, open(self._pos_file).read().split(","))
                self._cx = min(max(cx, _W // 2), sw - _W // 2)
                self._y = min(max(y, -_PAD), sh - _H - _PAD)
        except Exception:
            log.exception("bad pill position file, using default")

    def _save_pos(self):
        if not self._pos_file:
            return
        try:
            with open(self._pos_file, "w") as f:
                f.write(f"{self._cx},{self._y}")
        except OSError:
            log.exception("could not save pill position")

    def _press(self, event):
        self._press_xy = (event.x_root, event.y_root)
        self._press_win = (self.root.winfo_x(), self.root.winfo_y())
        self._dragged = False

    def _drag(self, event):
        dx = event.x_root - self._press_xy[0]
        dy = event.y_root - self._press_xy[1]
        if not self._dragged and abs(dx) + abs(dy) < 5:
            return  # not past the threshold yet; might still be a click
        self._dragged = True
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = min(max(self._press_win[0] + dx, -_PAD), sw - self._w - _PAD)
        y = min(max(self._press_win[1] + dy, -_PAD), sh - _H - _PAD)
        self.root.geometry(f"+{x}+{y}")

    def _release(self, event):
        if self._dragged:
            self._cx = self.root.winfo_x() + (self._w + 2 * _PAD) // 2
            self._y = self.root.winfo_y()
            self._save_pos()
            self._dragged = False
        else:
            self._clicked(event)

    def _resize(self, w):
        if self._w == w:
            return
        self._w = w
        # widen/narrow around the dragged center, clamped on-screen
        sw = self.root.winfo_screenwidth()
        x = min(max(self._cx - (w + 2 * _PAD) // 2, -_PAD), sw - w - _PAD)
        self.root.geometry(f"{w + 2 * _PAD}x{_H + 2 * _PAD}+{x}+{self._y}")

    @staticmethod
    def _in_box(x, y, box):
        return bool(box) and box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def _motion(self, event):
        if self.state != "result":
            return
        if not self._x_dismissing:
            self._x_hover = self._in_box(event.x, event.y, self._dismiss_box)
        if not self._check_flashing:
            self._check_hover = self._in_box(event.x, event.y, self._check_box)

    def _leave(self, event):
        self._x_hover = False
        self._check_hover = False

    def _clicked(self, event):
        if self.state != "result":
            return
        if self._in_box(event.x, event.y, self._dismiss_box):
            if not self._x_dismissing:
                self._x_dismissing = True
                self.root.after(120, self._finish_dismiss)
        elif self.on_result_click:
            if not self._check_flashing:
                self._check_flashing = True
                self.root.after(120, self._finish_repaste)

    def _finish_dismiss(self):
        self._x_dismissing = False
        self._x_hover = False
        if self.on_dismiss:
            self.on_dismiss()

    def _finish_repaste(self):
        self._check_flashing = False
        self._check_hover = False
        if self.on_result_click:
            self.on_result_click()

    def _apply_styles(self):
        # Tk resets ex-styles on remap, so this runs after every deiconify.
        # LAYERED here (not via tk attributes) because the pill is painted
        # with UpdateLayeredWindow: per-pixel alpha, so the rounded edges
        # blend smoothly instead of the hard staircase color-key gives.
        self._hwnd = _user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        style = _user32.GetWindowLongPtrW(self._hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongPtrW(
            self._hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def _show(self):
        if not self.visible:
            self.root.deiconify()
            self._apply_styles()
            self._last_sig = None  # layered content is gone after a remap
            self.visible = True

    def _hide(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
        self.levels.extend([0.0] * _NBARS)
        self._glow = 0.0
        self._pulse = 0.0
        self._x_hover = False
        self._x_dismissing = False
        self._check_hover = False
        self._check_flashing = False

    def _render(self, state, level):
        """Draw one frame at _S x scale, downsample for antialiasing."""
        S = _S
        w, h = (self._w + 2 * _PAD) * S, (_H + 2 * _PAD) * S
        pill = (_PAD * S, _PAD * S, (_PAD + self._w) * S - 1, (_PAD + _H) * S - 1)
        img = Image.new("RGBA", (w, h))
        if state == "recording":
            # halo: the pill's own silhouette blurred into the padding,
            # breathing on the pulse clock. Floor of 120 keeps a visible
            # ring at the dim end of the cycle - it breathes, never vanishes
            gd = ImageDraw.Draw(img)
            gd.rounded_rectangle(pill, radius=_RADIUS * S,
                                 fill=(195, 238, 255, int(120 + 135 * self._glow)))
            img = img.filter(ImageFilter.GaussianBlur((2.5 + 3.5 * self._glow) * S))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(pill, radius=_RADIUS * S,
                            fill=_ERR if state == "error" else _BG)
        self._dismiss_box = None
        self._check_box = None
        mid = h / 2
        if state == "recording":
            # interior vignette: a cold-blue radial lift at the center that
            # falls to the dark rim, breathing in sync with the halo so the
            # pulse reads as coming from inside the pill
            px0, py0 = (_PAD + 2) * S, (_PAD + 2) * S
            pw, ph = (self._w - 4) * S, (_H - 4) * S
            ys, xs = np.ogrid[0:ph, 0:pw]
            r = np.sqrt(((xs - pw / 2) / (pw / 2)) ** 2
                        + ((ys - ph / 2) / (ph / 2)) ** 2)
            lift = np.clip(1 - r, 0, 1) ** 1.5
            mask = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, pw - 1, ph - 1), radius=(_RADIUS - 2) * S, fill=255)
            tint = np.zeros((ph, pw, 4), np.uint8)
            tint[:, :, :3] = (70, 130, 180)
            tint[:, :, 3] = (lift * (70 + 60 * self._glow)
                             * (np.asarray(mask) / 255.0)).astype(np.uint8)
            img.alpha_composite(Image.fromarray(tint), (px0, py0))
            bw = (self._w - 2 * _RADIUS) * S / _NBARS
            recent = list(self.levels)[-(_NBARS // 2):]  # oldest..newest
            for i in range(_NBARS):
                ring = int(abs(i - (_NBARS - 1) / 2))  # 0 at center, 4 at edge
                lv = recent[-(ring + 1)]  # newest audio center, aging outward
                t = 1 - ring / (_NBARS // 2 - 1)  # deep blue edge -> light center
                bright = 0.4 + 0.6 * min(1.0, lv)  # quiet dims, loud blooms
                fill = tuple(int((e + (c - e) * t) * bright)
                             for c, e in zip(_BAR_RGB, _BAR_EDGE_RGB)) + (255,)
                x = (_PAD + _RADIUS) * S + i * bw + bw / 2
                bh = max(1.5, lv * (_H - 10) / 2) * S
                d.rounded_rectangle((x - S, mid - bh, x + S, mid + bh),
                                    radius=S, fill=fill)
        elif state == "loading":
            d.text((w / 2, h / 2), "loading…", fill="#aaaaaa",
                   font=self._font, anchor="mm")
        elif state == "result":
            # click-to-repaste offer: checkmark, seconds-left countdown, and
            # an X to dismiss early instead of waiting out the countdown.
            m = mid
            my = _PAD + _H / 2  # unscaled midline, for the hit-boxes
            cx = (_PAD + 24) * S  # 30 scaled ~20% down with the rest of the pill
            if self._check_flashing:
                d.ellipse((cx - 9 * S, m - 9 * S, cx + 9 * S, m + 9 * S),
                          fill=_BAR)
                check_fill = "#ffffff"
            elif self._check_hover:
                d.ellipse((cx - 9 * S, m - 9 * S, cx + 9 * S, m + 9 * S),
                          fill="#3a3a46")
                check_fill = "#8edcff"
            else:
                check_fill = _BAR
            pts = [(cx - 6 * S, m), (cx - 2 * S, m + 4 * S), (cx + 6 * S, m - 5 * S)]
            d.line(pts, fill=check_fill, width=3 * S, joint="curve")
            for px, py in (pts[0], pts[-1]):  # round caps
                d.ellipse((px - 1.5 * S, py - 1.5 * S, px + 1.5 * S, py + 1.5 * S),
                          fill=check_fill)
            self._check_box = (_PAD + 24 - 10, my - 10, _PAD + 24 + 10, my + 10)
            d.text(((_PAD + 46) * S, m), str(max(1, math.ceil(level))),
                   fill="#aaaaaa", font=self._font_count, anchor="mm")
            xx = (_PAD + self._w - _RADIUS - 6) * S  # 8 scaled the same way
            if self._x_dismissing:
                d.ellipse((xx - 8 * S, m - 8 * S, xx + 8 * S, m + 8 * S),
                          fill=_ERR)
                x_fill = "#ffffff"
            elif self._x_hover:
                d.ellipse((xx - 8 * S, m - 8 * S, xx + 8 * S, m + 8 * S),
                          fill="#3a3a46")
                x_fill = "#ffffff"
            else:
                x_fill = "#aaaaaa"
            d.line((xx - 4 * S, m - 4 * S, xx + 4 * S, m + 4 * S),
                   fill=x_fill, width=2 * S)
            d.line((xx - 4 * S, m + 4 * S, xx + 4 * S, m - 4 * S),
                   fill=x_fill, width=2 * S)
            self._dismiss_box = (_PAD + self._w - _RADIUS - 14, my - 8,
                                 _PAD + self._w - _RADIUS + 2, my + 8)
        elif state == "error":
            d.text((w / 2, h / 2), "error", fill="white",
                   font=self._font, anchor="mm")
        return img.resize((self._w + 2 * _PAD, _H + 2 * _PAD), Image.LANCZOS)

    def _push_frame(self, img):
        """Blit an RGBA frame to the layered window (premultiplied BGRA)."""
        w, h = img.size
        arr = np.asarray(img, dtype=np.uint16)
        a = arr[:, :, 3] * int(_ALPHA * 255) // 255
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[:, :, 0] = arr[:, :, 2] * a // 255
        bgra[:, :, 1] = arr[:, :, 1] * a // 255
        bgra[:, :, 2] = arr[:, :, 0] * a // 255
        bgra[:, :, 3] = a
        screen = _user32.GetDC(None)
        mem = _gdi32.CreateCompatibleDC(screen)
        bmi = _BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = _gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0,
                                      ctypes.byref(bits), None, 0)
        data = bgra.tobytes()
        ctypes.memmove(bits, data, len(data))
        old = _gdi32.SelectObject(mem, bmp)
        blend = _BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
        _user32.UpdateLayeredWindow(self._hwnd, screen, None,
                                    ctypes.byref(wt.SIZE(w, h)), mem,
                                    ctypes.byref(wt.POINT(0, 0)), 0,
                                    ctypes.byref(blend), ULW_ALPHA)
        _gdi32.SelectObject(mem, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mem)
        _user32.ReleaseDC(None, screen)

    def update(self, state, level=0.0):
        """Called only from the tk tick. state: hidden|loading|recording|result|error"""
        self.state = state
        if state == "hidden":
            self._hide()
            return
        self._resize(_W_RESULT if state == "result" else _W)
        self._show()
        if state == "recording":
            # x24: sized so a normal voice at ~2.5ft on this quiet mic drives
            # the mid-range; the pill sees raw level, normalize_peak doesn't
            # apply until transcription
            self.levels.append(min(1.0, level * 24))
            # halo breathes on its own clock, independent of voice level;
            # 1.2s felt too quick, 1.8 reads as calm breathing
            self._pulse = (self._pulse + 0.033 / 1.8) % 1.0
            self._glow = 0.5 - 0.5 * math.cos(2 * math.pi * self._pulse)
        sig = (state, self._w, tuple(self.levels), round(self._glow, 2),
               max(1, math.ceil(level)) if state == "result" else 0,
               self._x_hover, self._x_dismissing,
               self._check_hover, self._check_flashing)
        if sig != self._last_sig:
            self._last_sig = sig
            self._push_frame(self._render(state, level))


def _icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(24, 26, 32, 255))
    d.line((14, 18, 24, 48, 32, 26, 40, 48, 50, 18), fill=(90, 200, 250, 255),
           width=7, joint="curve")
    return img


def _fix_menu_hover(icon):
    """pystray 0.19.5's Win32 backend calls SetForegroundWindow on the tray
    icon's message window before showing the popup menu, but TrackPopupMenuEx
    is given a *different* hidden window as owner (_menu_hwnd) - that mismatch
    is why hovering over menu items shows no highlight (right-clicking the
    desktop highlights fine, so it isn't a Windows theme setting). Same
    right-click handling as pystray's Icon._on_notify, just foregrounding the
    window that actually owns the popup.

    Also tracks icon._menu_open: _update_menu() destroys and rebuilds the
    live hmenu, which if it happens while TrackPopupMenuEx is still tracking
    that same handle (i.e. the menu is open) kills hover highlighting stone
    dead for the rest of that popup - the tick loop checks this flag before
    calling update_menu() to avoid stepping on an open menu."""
    from pystray._util import win32

    icon._menu_open = False

    def on_notify(wparam, lparam):
        if lparam == win32.WM_LBUTTONUP:
            icon()
        elif icon._menu_handle and lparam == win32.WM_RBUTTONUP:
            win32.SetForegroundWindow(icon._menu_hwnd)
            point = wt.POINT()
            win32.GetCursorPos(ctypes.byref(point))
            hmenu, descriptors = icon._menu_handle
            icon._menu_open = True
            try:
                index = win32.TrackPopupMenuEx(
                    hmenu,
                    win32.TPM_RIGHTALIGN | win32.TPM_BOTTOMALIGN | win32.TPM_RETURNCMD,
                    point.x, point.y, icon._menu_hwnd, None)
            finally:
                icon._menu_open = False
            if index > 0:
                descriptors[index - 1](icon)

    icon._message_handlers[win32.WM_NOTIFY] = on_notify


def make_tray(base_dir, cfg, status, clipboard, recorder):
    def recopy_last(icon, item):
        if status.last_text:
            clipboard.set_text(status.last_text)

    def reconnect_mic(icon, item):
        try:
            recorder.reopen()
        except Exception:
            log.exception("mic reconnect failed")

    def quit_app(icon, item):
        status.quit_requested = True

    def restart_app(icon, item):
        status.restart_requested = True
        status.quit_requested = True

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: f"Words today: {status.words_today}",
                         None, enabled=False),
        pystray.MenuItem(lambda item: f"Total words: {status.words_total}",
                         None, enabled=False),
        pystray.MenuItem("Re-copy last transcription", recopy_last),
        pystray.MenuItem("View history",
                         lambda i, m: os.startfile(base_dir / cfg["files"]["history"])),
        pystray.MenuItem("Open config",
                         lambda i, m: os.startfile(base_dir / "config.toml")),
        pystray.MenuItem("Reconnect mic", reconnect_mic),
        pystray.MenuItem("Restart", restart_app),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("WisprClone", _icon_image(), "WisprClone", menu)
    _fix_menu_hover(icon)
    return icon


if __name__ == "__main__":
    # Self-test: cycle pill states with fake levels; click other windows to
    # confirm no focus theft, across several hide/show cycles.
    import math
    import time

    root = tk.Tk()
    pill = Pill(root)
    t0 = time.monotonic()

    def tick():
        t = time.monotonic() - t0
        phase = int(t) % 10
        if phase < 3:
            pill.update("loading")
        elif phase < 6:
            pill.update("recording", 0.04 + 0.04 * math.sin(t * 9))
        elif phase < 7:
            pill.update("error")
        else:
            pill.update("hidden")
        root.after(33, tick)

    tick()
    root.mainloop()
