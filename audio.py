"""Always-hot microphone recorder.

The stream opens once and never closes between recordings: opening a device
takes 50-300ms, which would clip the first word and stall the Windows input
hook that requested it. Hotkey threads only flip flags here; the PortAudio
callback is the sole owner of the block buffer and pre-roll deque, so there
are no cross-thread races on audio data.
"""

import collections
import logging
import os
import queue
import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # what Whisper expects

log = logging.getLogger("wisprclone")


class Ducker(threading.Thread):
    """Lowers other apps' playback while recording so speech stays clean,
    like Wispr Flow does. Runs in its own thread: the per-session COM volume
    calls are far too slow for an input hook or the UI tick."""

    def __init__(self, factor):
        super().__init__(daemon=True, name="ducker")
        self.factor = factor
        self.cmds = queue.Queue()
        self._saved = []  # (SimpleAudioVolume, level before ducking)

    def set(self, active):
        self.cmds.put(active)

    def run(self):
        import comtypes
        comtypes.CoInitialize()
        from pycaw.pycaw import AudioUtilities
        me = os.getpid()
        while True:
            active = self.cmds.get()
            while not self.cmds.empty():  # collapse rapid flip-flops
                active = self.cmds.get()
            try:
                if active and not self._saved:
                    for s in AudioUtilities.GetAllSessions():
                        if s.Process and s.Process.pid == me:
                            continue
                        v = s.SimpleAudioVolume
                        cur = v.GetMasterVolume()
                        v.SetMasterVolume(cur * self.factor, None)
                        self._saved.append((v, cur))
                elif not active and self._saved:
                    for v, cur in self._saved:
                        try:
                            v.SetMasterVolume(cur, None)
                        except Exception:
                            pass  # session may have ended mid-recording
                    self._saved = []
            except Exception:
                log.exception("audio ducking failed")


class Recorder:
    def __init__(self, cfg, jobs: queue.Queue, status):
        a = cfg["audio"]
        self.blocksize = a["blocksize"]
        preroll_blocks = max(1, int(a["preroll_ms"] * SAMPLE_RATE / 1000 / self.blocksize))
        self.preroll = collections.deque(maxlen=preroll_blocks)
        self.jobs = jobs
        self.status = status
        self.level = 0.0
        self.last_block_ts = time.monotonic()
        self.want_recording = False
        self.discard_next = False
        self.mode = "ptt"
        self._job_mode = "ptt"
        self.rec_start_ts = 0.0
        self._was_recording = False
        self._buffer = []
        self._stream = None
        self.suspended = False

    def _callback(self, indata, frames, t, cb_status):
        block = indata[:, 0].copy()
        self.last_block_ts = time.monotonic()
        self.level = float(np.sqrt(np.mean(block * block)))
        want = self.want_recording
        if want and not self._was_recording:
            # rising edge: pull in the pre-roll so speech just before the
            # button press isn't lost, then stop feeding the deque
            self._buffer = list(self.preroll)
            self.preroll.clear()
            self._job_mode = self.mode
        if want:
            self._buffer.append(block)
        else:
            if self._was_recording:
                # falling edge: hand the buffer off by reference. The too-short
                # check lives in the state machine (held duration), because the
                # pre-roll alone would nearly satisfy a sample-count minimum.
                if not self.discard_next:
                    self.status.inc_transcribing()
                    self.jobs.put({"blocks": self._buffer, "mode": self._job_mode})
                self._buffer = []
                self.discard_next = False
            self.preroll.append(block)
        self._was_recording = want

    def start_recording(self, mode="ptt"):
        self.mode = mode  # set before the flag; the callback reads both together
        self.discard_next = False  # a discard whose falling edge got merged
                                   # away must not eat this recording
        self.rec_start_ts = time.monotonic()
        self.want_recording = True

    def stop_recording(self, discard=False):
        self.discard_next = discard
        self.want_recording = False

    @property
    def recording_seconds(self):
        return time.monotonic() - self.rec_start_ts if self.want_recording else 0.0

    def open(self):
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=self.blocksize, callback=self._callback)
        self._stream.start()
        self.last_block_ts = time.monotonic()
        self.suspended = False
        self.status.mic_ok = True

    def suspend(self):
        """Release the mic while idle so the Windows in-use indicator clears.
        The pre-roll is gone until the next open, so the first dictation
        after a suspend may clip its opening syllable - documented tradeoff."""
        self.suspended = True
        self.preroll.clear()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

    def reopen(self):
        """Recover from a dead stream or follow a changed default device.

        PortAudio freezes its device list at init, so a full re-init is the
        only way to see a new default mic. sounddevice's _terminate/_initialize
        are private API - acceptable with the version pinned.
        """
        try:
            if self._stream is not None:
                self._stream.abort()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            sd._terminate()
            sd._initialize()
            self.open()
        except Exception:
            self.status.mic_ok = False
            raise

    def close(self):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Self-test: record 3 seconds to test.wav with a live level meter.
    import sys
    import tomllib
    import wave

    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)

    class _Status:
        mic_ok = True
        def inc_transcribing(self):
            pass

    jobs = queue.Queue()
    rec = Recorder(cfg, jobs, _Status())
    rec.open()
    print("recording 3s...")
    rec.start_recording()
    end = time.monotonic() + 3
    while time.monotonic() < end:
        bar = "#" * int(rec.level * 300)
        sys.stdout.write(f"\rlevel {rec.level:.4f} {bar:<40}")
        sys.stdout.flush()
        time.sleep(0.05)
    rec.stop_recording()
    time.sleep(0.2)  # let the callback hand the buffer off
    rec.close()
    print()
    job = jobs.get_nowait()
    audio = np.concatenate(job["blocks"])
    with wave.open("test.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    print(f"wrote test.wav ({len(audio) / SAMPLE_RATE:.2f}s)")
