"""Pill overlay (tk, main thread only) and system tray (pystray, own thread)."""

import collections
import ctypes
import logging
import math
import os
import tkinter as tk

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger("wisprclone")

_KEY = "#ff00ff"  # transparency color key
_BG = "#1e1e28"
_BAR = "#34c759"
_ERR = "#c04040"
_ALPHA = 0.65     # whole-pill translucency, Wispr-style (lower = more see-through)
_W, _H, _RADIUS = 76, 28, 14
_W_RESULT = 108  # wider: room for the checkmark, countdown, and dismiss X
_NBARS = 16

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2

_user32 = ctypes.windll.user32
_user32.GetAncestor.restype = ctypes.c_void_p
_user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
_user32.GetWindowLongPtrW.restype = ctypes.c_longlong
_user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
_user32.SetWindowLongPtrW.restype = ctypes.c_longlong
_user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong)


class Pill:
    def __init__(self, root, on_result_click=None, on_dismiss=None):
        self.root = root
        self.on_result_click = on_result_click
        self.on_dismiss = on_dismiss
        self.state = "hidden"
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-transparentcolor", _KEY,
                        "-alpha", _ALPHA)
        root.configure(bg=_KEY)
        self._w = _W
        self._y = root.winfo_screenheight() - _H - 60
        x = (root.winfo_screenwidth() - _W) // 2
        root.geometry(f"{_W}x{_H}+{x}+{self._y}")
        self.canvas = tk.Canvas(root, width=_W, height=_H, bg=_KEY,
                                highlightthickness=0)
        self.canvas.pack()
        # NOACTIVATE means this click never steals focus from the app the
        # user wants to paste into - which is the whole point of the feature
        self.canvas.bind("<Button-1>", self._clicked)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", self._leave)
        self.levels = collections.deque([0.0] * _NBARS, maxlen=_NBARS)
        self.visible = False
        self.flash_until = 0
        self._dismiss_box = None  # (x0, y0, x1, y1) hit-box, result state only
        self._check_box = None
        self._x_hover = False
        self._x_dismissing = False  # click-flash in progress, then hide
        self._check_hover = False
        self._check_flashing = False  # click-flash in progress, then repaste
        root.withdraw()

    def _resize(self, w):
        if self._w == w:
            return
        self._w = w
        x = (self.root.winfo_screenwidth() - w) // 2
        self.root.geometry(f"{w}x{_H}+{x}+{self._y}")
        self.canvas.config(width=w)

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

    def _apply_noactivate(self):
        # Tk resets ex-styles on remap, so this runs after every deiconify.
        # Read-modify-write: assigning outright would clobber WS_EX_LAYERED
        # and break the color-key transparency.
        hwnd = _user32.GetAncestor(self.canvas.winfo_id(), GA_ROOT)
        style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def _show(self):
        if not self.visible:
            self.root.deiconify()
            self._apply_noactivate()
            self.visible = True

    def _hide(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
        self.levels.extend([0.0] * _NBARS)
        self._x_hover = False
        self._x_dismissing = False
        self._check_hover = False
        self._check_flashing = False

    def _draw_base(self, fill):
        c = self.canvas
        c.delete("all")
        r = _RADIUS
        c.create_oval(0, 0, 2 * r, _H, fill=fill, outline="")
        c.create_oval(self._w - 2 * r, 0, self._w, _H, fill=fill, outline="")
        c.create_rectangle(r, 0, self._w - r, _H, fill=fill, outline="")

    def update(self, state, level=0.0):
        """Called only from the tk tick. state: hidden|loading|recording|result|error"""
        self.state = state
        if state == "hidden":
            self._hide()
            return
        self._resize(_W_RESULT if state == "result" else _W)
        self._show()
        self._draw_base(_ERR if state == "error" else _BG)
        c = self.canvas
        self._dismiss_box = None
        self._check_box = None
        if state == "recording":
            self.levels.append(min(1.0, level * 12))
            bw = (self._w - 2 * _RADIUS) / _NBARS
            mid = _H / 2
            for i, lv in enumerate(self.levels):
                x = _RADIUS + i * bw + bw / 2
                h = max(1.5, lv * (_H - 10) / 2)
                c.create_line(x, mid - h, x, mid + h, fill=_BAR, width=2,
                              capstyle=tk.ROUND)
        elif state == "loading":
            c.create_text(self._w / 2, _H / 2, text="loading…",
                          fill="#aaaaaa", font=("Segoe UI", 8))
        elif state == "result":
            # click-to-repaste offer: checkmark, seconds-left countdown, and
            # an X to dismiss early instead of waiting out the countdown.
            m = _H / 2
            cx = 30
            if self._check_flashing:
                c.create_oval(cx - 12, m - 12, cx + 12, m + 12, fill=_BAR, outline="")
                check_fill = "#ffffff"
            elif self._check_hover:
                c.create_oval(cx - 12, m - 12, cx + 12, m + 12, fill="#3a3a46", outline="")
                check_fill = "#5fe08a"
            else:
                check_fill = _BAR
            c.create_line(cx - 8, m, cx - 2, m + 5, cx + 8, m - 6,
                          fill=check_fill, width=3, capstyle=tk.ROUND, joinstyle=tk.ROUND)
            self._check_box = (cx - 12, m - 12, cx + 12, m + 12)
            c.create_text(58, m, text=str(max(1, math.ceil(level))),
                          fill="#aaaaaa", font=("Segoe UI", 9))
            xx = self._w - _RADIUS - 8
            if self._x_dismissing:
                c.create_oval(xx - 10, m - 10, xx + 10, m + 10, fill=_ERR, outline="")
                x_fill = "#ffffff"
            elif self._x_hover:
                c.create_oval(xx - 10, m - 10, xx + 10, m + 10, fill="#3a3a46", outline="")
                x_fill = "#ffffff"
            else:
                x_fill = "#aaaaaa"
            c.create_line(xx - 5, m - 5, xx + 5, m + 5, fill=x_fill, width=2,
                          capstyle=tk.ROUND)
            c.create_line(xx - 5, m + 5, xx + 5, m - 5, fill=x_fill, width=2,
                          capstyle=tk.ROUND)
            self._dismiss_box = (xx - 10, m - 10, xx + 10, m + 10)
        elif state == "error":
            c.create_text(self._w / 2, _H / 2, text="error",
                          fill="white", font=("Segoe UI", 8))


def _icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(24, 26, 32, 255))
    d.line((14, 18, 24, 48, 32, 26, 40, 48, 50, 18), fill=(52, 199, 89, 255),
           width=7, joint="curve")
    return img


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

    menu = pystray.Menu(
        pystray.MenuItem("Re-copy last transcription", recopy_last),
        pystray.MenuItem("View history",
                         lambda i, m: os.startfile(base_dir / cfg["files"]["history"])),
        pystray.MenuItem("Open config",
                         lambda i, m: os.startfile(base_dir / "config.toml")),
        pystray.MenuItem("Reconnect mic", reconnect_mic),
        pystray.MenuItem("Quit", quit_app),
    )
    return pystray.Icon("WisprClone", _icon_image(), "WisprClone", menu)


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
