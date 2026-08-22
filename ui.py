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
    def __init__(self, root, on_result_click=None):
        self.root = root
        self.on_result_click = on_result_click
        self.state = "hidden"
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-transparentcolor", _KEY,
                        "-alpha", _ALPHA)
        root.configure(bg=_KEY)
        x = (root.winfo_screenwidth() - _W) // 2
        y = root.winfo_screenheight() - _H - 60
        root.geometry(f"{_W}x{_H}+{x}+{y}")
        self.canvas = tk.Canvas(root, width=_W, height=_H, bg=_KEY,
                                highlightthickness=0)
        self.canvas.pack()
        # NOACTIVATE means this click never steals focus from the app the
        # user wants to paste into - which is the whole point of the feature
        self.canvas.bind("<Button-1>", self._clicked)
        self.levels = collections.deque([0.0] * _NBARS, maxlen=_NBARS)
        self.visible = False
        self.flash_until = 0
        root.withdraw()

    def _clicked(self, event):
        if self.state == "result" and self.on_result_click:
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

    def _draw_base(self, fill):
        c = self.canvas
        c.delete("all")
        r = _RADIUS
        c.create_oval(0, 0, 2 * r, _H, fill=fill, outline="")
        c.create_oval(_W - 2 * r, 0, _W, _H, fill=fill, outline="")
        c.create_rectangle(r, 0, _W - r, _H, fill=fill, outline="")

    def update(self, state, level=0.0):
        """Called only from the tk tick. state: hidden|loading|recording|result|error"""
        self.state = state
        if state == "hidden":
            self._hide()
            return
        self._show()
        self._draw_base(_ERR if state == "error" else _BG)
        c = self.canvas
        if state == "recording":
            self.levels.append(min(1.0, level * 12))
            bw = (_W - 2 * _RADIUS) / _NBARS
            mid = _H / 2
            for i, lv in enumerate(self.levels):
                x = _RADIUS + i * bw + bw / 2
                h = max(1.5, lv * (_H - 10) / 2)
                c.create_line(x, mid - h, x, mid + h, fill=_BAR, width=2,
                              capstyle=tk.ROUND)
        elif state == "loading":
            c.create_text(_W / 2, _H / 2, text="loading…",
                          fill="#aaaaaa", font=("Segoe UI", 8))
        elif state == "result":
            # click-to-repaste offer: checkmark plus seconds-left countdown.
            # `level` carries the remaining seconds for this state.
            m = _H / 2
            cx = _W / 2 - 10
            c.create_line(cx - 8, m, cx - 2, m + 5, cx + 8, m - 6,
                          fill=_BAR, width=3, capstyle=tk.ROUND, joinstyle=tk.ROUND)
            c.create_text(_W / 2 + 16, m, text=str(max(1, math.ceil(level))),
                          fill="#aaaaaa", font=("Segoe UI", 9))
        elif state == "error":
            c.create_text(_W / 2, _H / 2, text="error",
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
