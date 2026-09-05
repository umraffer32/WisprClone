"""Paste through the clipboard: save what's there, Ctrl+V the text, put it back."""

import logging
import struct
import time

import pywintypes
import win32clipboard
from pynput.keyboard import Controller, Key

log = logging.getLogger("wisprclone")

# Formats we can save and replay byte-for-byte. CF_DIB/CF_DIBV5/PNG cover a
# copied screenshot's actual image data. A screenshot also litters the
# clipboard with CF_BITMAP (a live GDI handle that dies the moment we touch
# the clipboard, not a copyable buffer) and OS plumbing (DataObject,
# cloud-clipboard flags) - none of that is content a paste target needs, so
# it's fine to drop. We only refuse to restore when TEXT shows up alongside
# something outside this set (rich text from Word/browsers) - there, partial
# restore would silently downgrade it to plain text, so we skip entirely.
_TEXT_FORMATS = {win32clipboard.CF_TEXT, win32clipboard.CF_OEMTEXT,
                 win32clipboard.CF_UNICODETEXT, win32clipboard.CF_LOCALE}
_IMAGE_FORMATS = {win32clipboard.CF_DIB, win32clipboard.CF_DIBV5,
                  win32clipboard.RegisterClipboardFormat("PNG")}
_SAFE_FORMATS = _TEXT_FORMATS | _IMAGE_FORMATS

# The same three formats password managers use to keep secrets out of
# Windows Clipboard History and Cloud Clipboard sync. Nothing we put on the
# clipboard should persist anywhere beyond the one paste it's for - dictation
# stays local per CLAUDE.md.
_EXCLUDE_FORMATS = [win32clipboard.RegisterClipboardFormat(name) for name in (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard")]


class Clipboard:
    def __init__(self, cfg):
        p = cfg["paste"]
        self.retries = p["clipboard_retries"]
        self.retry_s = p["clipboard_retry_ms"] / 1000
        self.restore_delay = p["restore_delay_ms"] / 1000
        self.kb = Controller()

    def _open(self):
        # OpenClipboard routinely loses races against clipboard managers
        for _ in range(self.retries):
            try:
                win32clipboard.OpenClipboard()
                return True
            except pywintypes.error:
                time.sleep(self.retry_s)
        return False

    def _mark_transient(self):
        """Call with the clipboard already open, right after writing to it."""
        zero = struct.pack("i", 0)
        for fmt in _EXCLUDE_FORMATS:
            win32clipboard.SetClipboardData(fmt, zero)

    def _replace_clipboard(self, populate):
        """Call with the clipboard already open: clears it, lets populate()
        write the new contents, then marks the result transient - including
        a restored prior clipboard, so putting it back doesn't create a
        fresh history entry for it."""
        win32clipboard.EmptyClipboard()
        populate()
        self._mark_transient()

    def _write_text(self, text):
        self._replace_clipboard(
            lambda: win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT))

    def paste(self, text):
        if not self._open():
            log.error("clipboard busy, dropping paste: %r", text[:80])
            return None
        saved = {}
        restorable = False
        try:
            formats, f = [], 0
            while (f := win32clipboard.EnumClipboardFormats(f)):
                formats.append(f)
            has_text = any(fmt in _TEXT_FORMATS for fmt in formats)
            restorable = not (has_text and not all(fmt in _SAFE_FORMATS for fmt in formats))
            if restorable:
                saved = {fmt: win32clipboard.GetClipboardData(fmt)
                         for fmt in formats if fmt in _SAFE_FORMATS}
                if formats and not saved:
                    # nothing we can safely carry forward (e.g. a copied
                    # file) - leave the dictated text rather than wipe
                    # the clipboard to empty
                    restorable = False
            self._write_text(text)
        finally:
            win32clipboard.CloseClipboard()

        # A physically held modifier would corrupt the Ctrl+V chord
        for mod in (Key.ctrl, Key.shift, Key.alt):
            self.kb.release(mod)
        with self.kb.pressed(Key.ctrl):
            self.kb.tap("v")
        # the user has their text from here on; returned so the job line's
        # paste= timing stops at the keystroke, not the restore sleep
        sent = time.monotonic()

        # No signal exists for "paste consumed"; the delay is the honest fix
        time.sleep(self.restore_delay)
        if restorable and self._open():
            try:
                def restore():
                    for fmt, data in saved.items():
                        win32clipboard.SetClipboardData(fmt, data)
                self._replace_clipboard(restore)
            finally:
                win32clipboard.CloseClipboard()
        return sent

    def set_text(self, text):
        if self._open():
            try:
                self._write_text(text)
            finally:
                win32clipboard.CloseClipboard()
