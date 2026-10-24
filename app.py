"""
Screengrab for the Tildagon badge.

Launch once, press CANCEL to send it to the background, then hold LEFT and
RIGHT together in any app to save the current screen as a BMP. Screenshots
go to /sd or /sdcard when one is mounted, otherwise /screenshots in badge
flash (oldest are deleted to keep MAX_INTERNAL).

The frame comes from display.get_framebuffer() (240x240 RGB565,
byte-swapped), written as a 16-bit BMP.

Button events normally only reach the focused app, so this registers its
handlers against an always-focused stand-in object instead of the app.

License: MIT
"""
import os

import app
import display
import micropython

from events.input import Buttons, BUTTON_TYPES, ButtonDownEvent, ButtonUpEvent
from system.eventbus import eventbus
from system.notification.events import ShowNotificationEvent

W = H = 240
COMBO = ("LEFT", "RIGHT")
EXTERNAL_DIRS = ("/sd", "/sdcard")
INTERNAL_DIR = "/screenshots"
MAX_INTERNAL = 6
SLACK = 16384  # free space to leave on the filesystem


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

    def _target(self):
        for d in EXTERNAL_DIRS:
            if _exists(d):
                d += "/screenshots"
                if not _exists(d):
                    os.mkdir(d)
                return d, True
        if not _exists(INTERNAL_DIR):
            os.mkdir(INTERNAL_DIR)
        return INTERNAL_DIR, False

    def _shots(self, d):
        return sorted(f for f in os.listdir(d) if f.startswith("shot_") and f.endswith(".bmp"))

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
        n = int(shots[-1][5:-4]) + 1 if shots else 1
        path = "%s/shot_%04d.bmp" % (d, n)
        buf = bytearray(len(fb))
        _swap16(fb, buf, len(fb))
        with open(path, "wb") as f:
            f.write(_bmp_header())
            f.write(buf)
        return path

    def background_update(self, delta):
        fb, self._pending = self._pending, None
        if fb is None:
            return
        try:
            self.last = self._save(fb)
            self.count += 1
            self.status = ""
            msg = "Saved " + self.last.rsplit("/", 1)[1]
        except Exception as e:
            self.status = "Failed: %s" % e
            msg = "Screengrab failed"
            print("Screengrab:", e)
        eventbus.emit(ShowNotificationEvent(message=msg))

    # --- ui ---------------------------------------------------------------

    def update(self, delta):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()

    def draw(self, ctx):
        ctx.rgb(0.05, 0.06, 0.10).rectangle(-120, -120, 240, 240).fill()
        ctx.save()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 22
        ctx.rgb(0.8, 0.85, 1.0).move_to(0, -70).text("Screengrab")
        ctx.font_size = 15
        ctx.rgb(0.6, 0.65, 0.8)
        ctx.move_to(0, -35).text("Hold LEFT + RIGHT")
        ctx.move_to(0, -15).text("in any app to save")
        try:
            d, external = self._target()
            where = d if external else "flash " + d
        except Exception:
            where = "no storage"
        ctx.rgb(0.4, 0.85, 0.5).move_to(0, 15).text(where)
        ctx.rgb(0.8, 0.85, 1.0).move_to(0, 38).text("Saved this run: %d" % self.count)
        if self.status:
            ctx.rgb(1.0, 0.4, 0.4).move_to(0, 58).text(self.status)
        ctx.rgb(0.6, 0.65, 0.8).move_to(0, 85).text("CANCEL: run in background")
        ctx.restore()
        self.draw_overlays(ctx)


__app_export__ = ScreenGrab  # pylint: disable=invalid-name
