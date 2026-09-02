"""WisprClone — local push-to-talk dictation.

Hold the configured mouse/keyboard button to record; release to transcribe
and paste into the focused app. Tap Right Ctrl to toggle long-form recording.
Run with --echo to print raw input events (for diagnosing G HUB remaps).
"""

import ctypes
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tomllib
from pathlib import Path

from pynput import keyboard, mouse

BASE = Path(__file__).parent

IDLE, RECORDING_PTT, RECORDING_TOGGLE = range(3)
VK = {"x1": 0x05, "x2": 0x06}

log = logging.getLogger("wisprclone")


def setup_logging():
    h = logging.handlers.RotatingFileHandler(BASE / "wisprclone.log",
                                             maxBytes=1_000_000, backupCount=1,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(h)
    if sys.stderr is not None:  # console run: mirror to terminal
        log.addHandler(logging.StreamHandler())
    sys.excepthook = lambda *a: log.error("uncaught", exc_info=a)
    threading.excepthook = lambda a: log.error("uncaught in %s", a.thread.name,
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def single_instance():
    import win32api
    import win32event
    import winerror
    handle = win32event.CreateMutex(None, False, "Global\\WisprClone")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        sys.exit(0)
    return handle  # keep a reference so it isn't GC'd


def resolve_ptt(name):
    """Returns (kind, pynput object, virtual-key code) for the configured PTT input."""
    if name in VK:
        return "mouse", getattr(mouse.Button, name), VK[name]
    key = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)
    vk = key.value.vk if isinstance(key, keyboard.Key) else key.vk
    return "keyboard", key, vk


class StateMachine:
    def __init__(self, cfg, recorder):
        hk = cfg["hotkeys"]
        self.min_s = cfg["audio"]["min_recording_s"]
        self.max_s = cfg["audio"]["max_recording_s"]
        self.tap_max = hk["toggle_max_tap_ms"] / 1000
        self.debounce = hk["debounce_ms"] / 1000
        self.recorder = recorder
        self.lock = threading.Lock()
        self.state = IDLE
        self.last_tap = 0.0
        # RCtrl tap bookkeeping (keyboard listener thread only)
        self.rctrl_down_ts = None
        self.rctrl_dirty = False

    def ptt_press(self):
        with self.lock:
            if self.state == IDLE:
                self.state = RECORDING_PTT
                self.recorder.start_recording(mode="ptt")

    def ptt_release(self):
        with self.lock:
            if self.state == RECORDING_PTT:
                too_short = self.recorder.recording_seconds < self.min_s
                self.recorder.stop_recording(discard=too_short)
                self.state = IDLE

    def toggle_tap(self):
        with self.lock:
            now = time.monotonic()
            if now - self.last_tap < self.debounce:
                return
            self.last_tap = now
            if self.state == IDLE:
                self.state = RECORDING_TOGGLE
                self.recorder.start_recording(mode="toggle")
            elif self.state == RECORDING_TOGGLE:
                too_short = self.recorder.recording_seconds < self.min_s
                self.recorder.stop_recording(discard=too_short)
                self.state = IDLE

    def timeout_stop(self):
        """Max-length hit. A PTT this long is a stuck button: discard, never
        paste desk noise. Toggle mode pastes — long dictation is its job."""
        with self.lock:
            if self.state == RECORDING_PTT:
                self.recorder.stop_recording(discard=True)
                self.state = IDLE
                return "discarded"
            if self.state == RECORDING_TOGGLE:
                self.recorder.stop_recording()
                self.state = IDLE
                return "enqueued"
        return None


def main():
    echo = "--echo" in sys.argv
    setup_logging()
    mutex = single_instance()
    with open(BASE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)

    if echo:
        run_echo(cfg)
        return

    from audio import Ducker, Recorder
    from transcribe import Status, Transcriber
    from ui import Pill, make_tray

    status = Status()
    status.quit_requested = False
    status.restart_requested = False
    jobs = queue.Queue()
    recorder = Recorder(cfg, jobs, status)
    try:
        recorder.open()
    except Exception:
        # e.g. launched at login before a USB mic/dock has enumerated yet.
        # Don't let this kill the app before the tray icon even exists -
        # the stale-stream watchdog in tick() retries a few seconds in.
        log.exception("mic open failed at startup")
        status.mic_ok = False
    ducker = None
    if cfg["audio"]["duck"]:
        ducker = Ducker(cfg["audio"]["duck_factor"])
        ducker.start()
    sm = StateMachine(cfg, recorder)
    worker = Transcriber(cfg, BASE, jobs, status)
    worker.start()

    ptt_kind, ptt_obj, ptt_vk = resolve_ptt(cfg["hotkeys"]["ptt"])
    toggle_key = getattr(keyboard.Key, cfg["hotkeys"]["toggle_key"])

    # --- input listeners -------------------------------------------------
    # The PTT button is handled inside the win32 event filter so it can be
    # acted on AND suppressed system-wide in one step. Suppression is
    # mandatory for mouse PTT: X buttons natively mean back/forward in
    # browsers, and Wispr Flow-style rebinds fire on them too — a dictation
    # press must not leak to other apps. suppress_event() also skips the
    # normal callbacks, so PTT logic lives here, not in on_click.
    # Callback/filter bodies are wrapped: an uncaught exception silently
    # kills the pynput listener thread, i.e. all hotkeys until restart.
    # suppress_event() itself raises to work, so it stays OUTSIDE the try.
    WM_XDOWN, WM_XUP = {0x020B, 0x00AB}, {0x020C, 0x00AC}
    WM_KEYDOWN, WM_KEYUP = {0x0100, 0x0104}, {0x0101, 0x0105}
    LLKHF_INJECTED = 0x10
    ptt_is_down = [False]  # our own physical-state tracking; suppressed
    #                        events never reach GetAsyncKeyState

    def mouse_filter(msg, data):
        if ptt_kind != "mouse" or (msg not in WM_XDOWN and msg not in WM_XUP):
            return True
        if (data.mouseData >> 16) & 0xFFFF != ptt_xnum:
            return True
        try:
            down = msg in WM_XDOWN
            ptt_is_down[0] = down
            sm.ptt_press() if down else sm.ptt_release()
        except Exception:
            log.exception("mouse ptt filter")
        mouse_listener.suppress_event()

    def kb_filter(msg, data):
        if ptt_kind != "keyboard" or data.vkCode != ptt_vk or data.flags & LLKHF_INJECTED:
            return True
        try:
            down = msg in WM_KEYDOWN
            ptt_is_down[0] = down
            sm.ptt_press() if down else sm.ptt_release()
        except Exception:
            log.exception("keyboard ptt filter")
        kb_listener.suppress_event()

    def on_press(key, injected=False):
        try:
            if injected:  # our own synthetic Ctrl+V must not feed back
                return
            if key == toggle_key:
                if sm.rctrl_down_ts is None:  # ignore key auto-repeat
                    sm.rctrl_down_ts = time.monotonic()
                    sm.rctrl_dirty = False
            elif sm.rctrl_down_ts is not None:
                sm.rctrl_dirty = True  # combo like Ctrl+C: not a tap
        except Exception:
            log.exception("key press callback")

    def on_release(key, injected=False):
        try:
            if injected:
                return
            if key == toggle_key and sm.rctrl_down_ts is not None:
                held = time.monotonic() - sm.rctrl_down_ts
                if held < sm.tap_max and not sm.rctrl_dirty:
                    sm.toggle_tap()
                sm.rctrl_down_ts = None
        except Exception:
            log.exception("key release callback")

    ptt_xnum = {"x1": 1, "x2": 2}.get(cfg["hotkeys"]["ptt"], 0)
    mouse_listener = mouse.Listener(win32_event_filter=mouse_filter)
    kb_listener = keyboard.Listener(on_press=on_press, on_release=on_release,
                                    win32_event_filter=kb_filter)
    mouse_listener.start()
    kb_listener.start()

    # --- UI + tick (main thread) -----------------------------------------
    root = tk.Tk()
    clipboard = worker.clipboard

    def repaste_last():
        status.result_until = 0.0  # click registered: hide the offer
        if status.last_text:
            # manual re-aim: don't presume where this lands, just avoid
            # gluing onto whatever's already there
            threading.Thread(target=clipboard.paste,
                             args=(" " + status.last_text,), daemon=True).start()

    def dismiss_result():
        status.result_until = 0.0  # X clicked: hide without repasting

    pill = Pill(root, on_result_click=repaste_last, on_dismiss=dismiss_result,
                pos_file=BASE / "pill_pos.txt")
    tray = make_tray(BASE, cfg, status, clipboard, recorder)
    tray.run_detached()

    user32 = ctypes.windll.user32
    DESKTOP_READOBJECTS = 0x0001
    mic_recovery_tried = False
    was_recording = False
    idle_close_s = cfg["audio"]["idle_close_s"]
    last_activity = time.monotonic()
    last_menu_update = 0.0

    def input_desktop_ours():
        # Fails while the secure desktop (UAC) or lock screen holds input —
        # exactly the windows in which a PTT release event gets swallowed.
        h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if h:
            user32.CloseDesktop(h)
            return True
        return False

    def tick():
        nonlocal mic_recovery_tried, was_recording, last_menu_update
        if status.quit_requested:
            teardown()
            return

        rec_now = sm.state != IDLE
        if ducker and rec_now != was_recording:
            ducker.set(rec_now)
        was_recording = rec_now

        # Lost-release guard, PTT ONLY: suppressed events never update
        # GetAsyncKeyState, so physical state is tracked in our own filter;
        # a desktop switch mid-hold means the release will never arrive.
        # Toggle mode intentionally has no such guard — nothing is held.
        if sm.state == RECORDING_PTT and (not ptt_is_down[0] or not input_desktop_ours()):
            sm.ptt_release()

        if sm.state != IDLE and recorder.recording_seconds > sm.max_s:
            if sm.timeout_stop() == "discarded":
                status.flash_error = True
                log.warning("PTT hit max length; recording discarded as stuck button")

        # Idle mic release: clears the Windows in-use indicator when the app
        # hasn't been used for a while; reopens the instant a recording is
        # requested (first syllable after a reopen may clip - documented).
        nonlocal last_activity
        if rec_now or status.transcribing > 0:
            last_activity = time.monotonic()
        if recorder.suspended:
            if rec_now:
                try:
                    recorder.open()
                except Exception:
                    log.exception("mic reopen after idle failed")
        elif (idle_close_s > 0
              and time.monotonic() - last_activity > idle_close_s):
            log.info("mic released after %ds idle", idle_close_s)
            recorder.suspend()

        # Stream death: device removal doesn't raise, callbacks just stop.
        if not recorder.suspended and time.monotonic() - recorder.last_block_ts > 2.0:
            if not mic_recovery_tried:
                mic_recovery_tried = True
                log.warning("mic stream stale, attempting recovery")
                try:
                    recorder.reopen()
                except Exception:
                    log.exception("mic recovery failed")
        else:
            mic_recovery_tried = False

        if status.flash_error:
            status.flash_error = False
            pill.flash_until = time.monotonic() + 1.2
        if rec_now:
            status.result_until = 0.0  # a new recording supersedes the offer
        if time.monotonic() < pill.flash_until:
            pill.update("error")
        elif sm.state != IDLE:
            pill.update("recording", recorder.level)
        elif time.monotonic() < status.result_until:
            pill.update("result", status.result_until - time.monotonic())
        elif not status.ready and not status.error:
            pill.update("loading")
        else:
            pill.update("hidden")

        tooltip = {"": "WisprClone",
                   "cuda": "WisprClone (GPU)",
                   "cpu": "WisprClone (CPU)"}[status.device]
        if status.error:
            tooltip = f"WisprClone — {status.error}"
        elif not status.mic_ok:
            tooltip = "WisprClone — mic unavailable"
        else:
            tooltip += f" — {status.words_today} words today"
        if tray.title != tooltip:
            tray.title = tooltip

        # pystray's Win32 backend caches the native menu text, so the
        # word-count entries (built from lambdas) never refresh on their
        # own - unlike tray.title, which does. update_menu() rebuilds it;
        # throttled since it's a real Win32 call, not just a state check.
        # Skipped while the menu is actually open - update_menu() destroys
        # the live hmenu, which kills hover highlighting for the rest of
        # that popup if it happens mid-click (see ui._fix_menu_hover).
        if (time.monotonic() - last_menu_update > 1.0
                and not getattr(tray, "_menu_open", False)):
            last_menu_update = time.monotonic()
            try:
                tray.update_menu()
            except Exception:
                log.exception("tray menu refresh failed")

        root.after(33, tick)

    def teardown():
        # A live recording must hand off before the stream closes, and queued
        # jobs must finish pasting - Restart clicked right after dictating
        # would otherwise eat the dictation (or die mid-paste with the user's
        # clipboard clobbered).
        with sm.lock:
            stopping = sm.state != IDLE
            if stopping:
                recorder.stop_recording(
                    discard=recorder.recording_seconds < sm.min_s)
                sm.state = IDLE
        if stopping:
            time.sleep(0.1)  # two callback periods for the buffer handoff
        deadline = time.monotonic() + 10
        while status.transcribing > 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        mouse_listener.stop()
        kb_listener.stop()
        recorder.close()
        if ducker:
            ducker.set(False)
            time.sleep(0.25)  # let the daemon thread restore volumes
        try:
            tray.stop()
        except Exception:
            pass
        root.destroy()

    log.info("started (ptt=%s, pid=%d)", cfg["hotkeys"]["ptt"], os.getpid())
    tick()
    root.mainloop()

    if status.restart_requested:
        import subprocess
        import win32api
        win32api.CloseHandle(mutex)  # the new instance checks the mutex at startup
        launcher = Path(sys.prefix) / "Scripts" / "WisprClone.exe"
        log.info("restarting via %s", launcher)
        try:
            subprocess.Popen([str(launcher), str(BASE / "wisprclone.py")], cwd=BASE)
        except OSError:
            # launcher missing (base-python run, rebuilt venv) or elevation
            # refused - the scheduled task knows how to launch us either way
            log.exception("direct relaunch failed, delegating to scheduled task")
            subprocess.run(["schtasks", "/run", "/tn", "WisprClone"], check=False)


def run_echo(cfg):
    """Print raw input events incl. the injected flag — G HUB diagnosis."""
    print("echo mode: press your PTT button / Right Ctrl. Ctrl+C to exit.")

    def on_click(x, y, button, pressed, injected=False):
        print(f"mouse {button} {'down' if pressed else 'up  '} injected={injected}")

    def on_press(key, injected=False):
        print(f"key   {key} down injected={injected}")

    def on_release(key, injected=False):
        print(f"key   {key} up   injected={injected}")

    with mouse.Listener(on_click=on_click), \
         keyboard.Listener(on_press=on_press, on_release=on_release):
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
