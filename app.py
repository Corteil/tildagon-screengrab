"""
Screengrab for the Tildagon badge.

Launch once, press CANCEL to send it to the background (or start it at boot
from /boot.py, see the README; the app's menu switches that on and off), then hold LEFT and RIGHT together in any app to save the current screen as a BMP, named
shot_<number>_<YYYYMMDD-HHMMSS>.bmp (UTC; the clock is set over NTP when
Wi-Fi is up, otherwise a random UUID is used as the unique reference).
Screenshots go to /sd or /sdcard when one is mounted, otherwise /screenshots
in badge flash (oldest are deleted to keep MAX_INTERNAL). Open the app to
move the flash shots onto the SD card or delete them.

The frame comes from display.get_framebuffer() (240x240 RGB565,
byte-swapped), written as a 16-bit BMP.

Button events normally only reach the focused app, so this registers its
handlers against an always-focused stand-in object instead of the app.

License: MIT
"""
import os
import time

import app
import display
import micropython
import settings

from events.input import Buttons, BUTTON_TYPES, ButtonDownEvent, ButtonUpEvent
from system.eventbus import eventbus
from system.notification.events import ShowNotificationEvent

W = H = 240
COMBO = ("LEFT", "RIGHT")
EXTERNAL_DIRS = ("/sd", "/sdcard")
INTERNAL_DIR = "/screenshots"
MAX_INTERNAL = 6
SLACK = 16384  # free space to leave on the filesystem
CLOCK_OK_YEAR = 2024  # the RTC reads 2000 until something sets it
NTP_RETRY_MS = 300000

AUTOSTART_KEY = "screengrab_autostart"  # read by the /boot.py snippet, default off
QUIT_KEY = "screengrab_cancel_quits"  # F/CANCEL quits instead of backgrounding
MENU = ("Move flash to SD", "Delete flash shots", "Start at boot", "F key")


@micropython.viper
def _swap16(src: ptr8, dst: ptr8, n: int):
    # Framebuffer is big-endian RGB565, BMP wants little-endian.
    i = 0
    while i < n:
        dst[i] = src[i + 1]
        dst[i + 1] = src[i]
        i += 2


def _bmp_header():
    size = W * H * 2
    hdr = bytearray(66)
    hdr[0:2] = b"BM"
    hdr[2:6] = (66 + size).to_bytes(4, "little")
    hdr[10:14] = (66).to_bytes(4, "little")
    hdr[14:18] = (40).to_bytes(4, "little")
    hdr[18:22] = W.to_bytes(4, "little")
    hdr[22:26] = (0x100000000 - H).to_bytes(4, "little")  # negative height = top-down
    hdr[26:28] = (1).to_bytes(2, "little")
    hdr[28:30] = (16).to_bytes(2, "little")
    hdr[30:34] = (3).to_bytes(4, "little")  # BI_BITFIELDS
    hdr[34:38] = size.to_bytes(4, "little")
    hdr[54:58] = (0xF800).to_bytes(4, "little")
    hdr[58:62] = (0x07E0).to_bytes(4, "little")
    hdr[62:66] = (0x001F).to_bytes(4, "little")
    return hdr


def _uuid4():
    b = bytearray(os.urandom(16))
    b[6] = b[6] & 0x0F | 0x40  # version 4
    b[8] = b[8] & 0x3F | 0x80  # RFC 4122 variant
    h = "".join("%02x" % x for x in b)
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def _stamp():
    t = time.localtime()
    if t[0] >= CLOCK_OK_YEAR:
        return "%04d%02d%02d-%02d%02d%02d" % t[:6]
    return _uuid4()  # no clock: a unique reference instead (the UUID of urn:uuid:...)


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


class _Focused:
    """Stand-in event bus subscriber that always counts as focused."""

    _focused = True


class ScreenGrab(app.App):
    _primary = None  # the instance that listens; launching again must not double up

    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self._sink = _Focused()
        self._held = set()
        self._armed = True
        self._pending = None
        self.count = 0
        self.last = None
        self.status = ""
        self._sel = 0
        self._cnt = (None, 0)  # (SD shots or None, flash shots), refreshed in update
        self._cnt_ms = 0
        self._confirm = False
        self._autostart = settings.get(AUTOSTART_KEY, False)
        self._cancel_quits = settings.get(QUIT_KEY, False)
        self._queue = []  # flash shots still to move
        self._moved = 0
        self._ntp_next = time.ticks_add(time.ticks_ms(), 30000)
        if ScreenGrab._primary is None:
            ScreenGrab._primary = self
            eventbus.on(ButtonDownEvent, self._down, self._sink)
            eventbus.on(ButtonUpEvent, self._up, self._sink)

    # --- trigger ----------------------------------------------------------

    def _down(self, event):
        try:
            for name in COMBO:
                if BUTTON_TYPES[name] in event.button:
                    self._held.add(name)
            if self._armed and len(self._held) == len(COMBO):
                self._armed = False
                # Grab now, before any notification is drawn; write later.
                self._pending = display.get_framebuffer()
        except Exception as e:
            print("Screengrab:", e)

    def _up(self, event):
        for name in COMBO:
            if BUTTON_TYPES[name] in event.button:
                self._held.discard(name)
        if not self._held:
            self._armed = True

    # --- storage ----------------------------------------------------------

    def _sd_dir(self, create=False):
        for d in EXTERNAL_DIRS:
            if _exists(d):
                d += "/screenshots"
                if create and not _exists(d):
                    os.mkdir(d)
                return d
        return None

    def _flash_dir(self, create=False):
        if create and not _exists(INTERNAL_DIR):
            os.mkdir(INTERNAL_DIR)
        return INTERNAL_DIR

    def _target(self):
        d = self._sd_dir(create=True)
        if d:
            return d, True
        return self._flash_dir(create=True), False

    def _shots(self, d):
        try:
            names = os.listdir(d)
        except OSError:
            return []
        return sorted(f for f in names if f.startswith("shot_") and f.endswith(".bmp"))

    def _next_number(self, shots):
        return int(shots[-1][5:9]) + 1 if shots else 1

    def _free(self, d):
        s = os.statvfs(d)
        return s[0] * s[4]

    def _save(self, fb):
        d, external = self._target()
        shots = self._shots(d)
        need = 66 + W * H * 2
        while shots and (
            self._free(d) < need + SLACK or (not external and len(shots) >= MAX_INTERNAL)
        ):
            os.remove(d + "/" + shots.pop(0))
        if self._free(d) < need + SLACK:
            raise OSError("no space")
        path = "%s/shot_%04d_%s.bmp" % (d, self._next_number(shots), _stamp())
        buf = bytearray(len(fb))
        _swap16(fb, buf, len(fb))
        with open(path, "wb") as f:
            f.write(_bmp_header())
            f.write(buf)
        return path, external

    def _move_one(self):
        """Copy the next queued flash shot to the SD card, then delete it."""
        sd = self._sd_dir(create=True)
        if sd is None:
            raise OSError("SD removed")
        name = self._queue[0]
        src = INTERNAL_DIR + "/" + name
        size = os.stat(src)[6]
        if self._free(sd) < size + SLACK:
            raise OSError("SD full")
        # Renumber into the SD sequence, keeping any timestamp suffix.
        dst = "%s/shot_%04d%s" % (sd, self._next_number(self._shots(sd)), name[9:])
        buf = bytearray(4096)
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                n = fi.readinto(buf)
                if not n:
                    break
                fo.write(memoryview(buf)[:n])
        if os.stat(dst)[6] != size:
            os.remove(dst)
            raise OSError("copy failed")
        os.remove(src)
        self._queue.pop(0)
        self._moved += 1

    # --- background -------------------------------------------------------

    def _sync_clock(self):
        if time.localtime()[0] >= CLOCK_OK_YEAR:
            return
        if time.ticks_diff(time.ticks_ms(), self._ntp_next) < 0:
            return
        self._ntp_next = time.ticks_add(time.ticks_ms(), NTP_RETRY_MS)
        try:
            import network
            import ntptime

            if network.WLAN(network.STA_IF).isconnected():
                ntptime.settime()
        except Exception as e:
            print("Screengrab: clock sync failed:", e)

    def background_update(self, delta):
        fb, self._pending = self._pending, None
        if fb is None:
            self._sync_clock()
            return
        try:
            self.last, external = self._save(fb)
            self.count += 1
            self.status = ""
            msg = "Saved to %s: %s" % ("SD" if external else "flash", self.last.rsplit("/", 1)[1])
        except Exception as e:
            self.status = "Failed: %s" % e
            msg = "Screengrab failed"
            print("Screengrab:", e)
        eventbus.emit(ShowNotificationEvent(message=msg))

    # --- ui ---------------------------------------------------------------

    def _counts(self):
        sd = self._sd_dir()
        return (len(self._shots(sd)) if sd else None), len(self._shots(self._flash_dir()))

    def _enabled(self, i):
        sd, flash = self._cnt
        return i >= 2 or flash > 0 and (sd is not None if i == 0 else True)

    def _label(self, i):
        if i == 2:
            return "Start at boot: " + ("ON" if self._autostart else "OFF")
        if i == 3:
            return "F key: " + ("quit" if self._cancel_quits else "background")
        return MENU[i]

    def _quit(self):
        """Stop listening for the combo and close the app."""
        main = ScreenGrab._primary
        ScreenGrab._primary = None
        if main is not None:
            eventbus.deregister(main._sink)
            if main is not self:
                main.terminate()
        self.terminate()

    def _activate(self):
        self._cnt = self._counts()
        if self._sel == 2:
            self._autostart = not self._autostart
            settings.set(AUTOSTART_KEY, self._autostart)
            settings.save()
            self.status = "Applies next boot"
            return
        if self._sel == 3:
            self._cancel_quits = not self._cancel_quits
            settings.set(QUIT_KEY, self._cancel_quits)
            settings.save()
            self.status = "F quits" if self._cancel_quits else "F backgrounds"
            return
        if not self._enabled(self._sel):
            self.status = "SD not mounted" if self._sel == 0 and self._sd_dir() is None else "No flash shots"
            return
        if self._sel == 0:
            self._queue = self._shots(self._flash_dir())
            self._moved = 0
            self.status = "Moving..."
        elif not self._confirm:
            self._confirm = True
            self.status = "CONFIRM to delete"
        else:
            self._confirm = False
            n = 0
            for f in self._shots(self._flash_dir()):
                os.remove(INTERNAL_DIR + "/" + f)
                n += 1
            self.status = "Deleted %d" % n

    def update(self, delta):
        b = self.button_states
        if b.get(BUTTON_TYPES["CANCEL"]):
            b.clear()
            if self._cancel_quits:
                self._quit()
            else:
                self.minimise()
            return
        self._cnt_ms -= delta
        if self._cnt_ms <= 0 or self._queue:
            self._cnt = self._counts()
            self._cnt_ms = 1000
        if self._queue:  # one file per tick keeps the screen alive
            try:
                self._move_one()
                if not self._queue:
                    self.status = "Moved %d to SD" % self._moved
            except Exception as e:
                self._queue = []
                self.status = "Move: %s" % e
            return
        if b.pressed(BUTTON_TYPES["UP"]):
            self._sel = (self._sel - 1) % len(MENU)
            self._confirm = False
            self.status = ""
        if b.pressed(BUTTON_TYPES["DOWN"]):
            self._sel = (self._sel + 1) % len(MENU)
            self._confirm = False
            self.status = ""
        if b.pressed(BUTTON_TYPES["CONFIRM"]):
            try:
                self._activate()
            except Exception as e:
                self.status = "Error: %s" % e
            self._cnt_ms = 0

    def draw(self, ctx):
        # The visible area is a circle a bit smaller than the 240px panel, so
        # keep text near the top and bottom short and well inside it.
        ctx.rgb(0.05, 0.06, 0.10).rectangle(-120, -120, 240, 240).fill()
        ctx.save()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 18
        ctx.rgb(0.8, 0.85, 1.0).move_to(0, -80).text("Screengrab")
        ctx.font_size = 13
        sd, flash = self._cnt
        ctx.rgb(0.4, 0.85, 0.5).move_to(0, -60).text(
            "SD: %s   Flash: %d" % ("-" if sd is None else sd, flash)
        )
        ctx.font_size = 15
        for i in range(len(MENU)):
            y = -36 + 22 * i
            on, sel = self._enabled(i), i == self._sel
            if sel:
                ctx.rgb(1.0, 0.55, 0.0).rectangle(-76, y - 10, 152, 20).fill()
            if on:
                ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
            else:
                ctx.rgb(0.35, 0.2, 0.0) if sel else ctx.rgb(0.4, 0.42, 0.5)
            ctx.move_to(0, y).text(self._label(i))
        ctx.font_size = 12
        if self.status:
            ctx.rgb(1.0, 0.8, 0.3).move_to(0, 56).text(self.status[:20])
        ctx.rgb(0.6, 0.65, 0.8).move_to(0, 80).text("LEFT+RIGHT = grab")
        ctx.restore()
        self.draw_overlays(ctx)


__app_export__ = ScreenGrab  # pylint: disable=invalid-name
