"""
Screengrab for the Tildagon badge.

Launch once and press F/CANCEL to send it to the background (or switch on
"Start at boot" in its menu), then hold LEFT and RIGHT together in any app to
save the current screen as a PNG named shot_<number>_<YYYYMMDD-HHMMSS>.png.
The stamp is UTC (the clock is set over NTP when Wi-Fi is up); with no clock a
random UUID stands in as the unique reference.

Shots go to a mounted storage card when there is one: any top-level mount of
at least 256 KiB (found from the mount table, so the port and mount name
don't matter), and the menu can pin one when several are mounted. Otherwise
they go to /screenshots in badge flash (oldest are deleted to keep
MAX_INTERNAL). Open the app to move the flash shots onto the card or delete
them, or press RIGHT for the storage tile: every storage with its shot count
and free space, a choice of where new shots go, and a browser with a full
screen viewer to view, delete or move single shots.

The frame comes from display.get_framebuffer() (240x240 RGB565,
byte-swapped) and is written by pngenc.py as a compressed PNG, which the
viewer then draws straight from the file with ctx.image().

Button events normally only reach the focused app, so this registers its
handlers against an always-focused stand-in object instead of the app.

License: MIT
"""
import gc
import math
import os
import time

import app
import display
import settings
import vfs

from events.input import Buttons, BUTTON_TYPES, ButtonDownEvent, ButtonUpEvent
from system.eventbus import eventbus
from system.notification.events import ShowNotificationEvent

from .pngenc import write_png

W = H = 240
COMBO = ("LEFT", "RIGHT")
MIN_CARD_BYTES = 256 << 10  # smaller mounts (hexpansion EEPROMs, ~8 KB) aren't storage
INTERNAL_DIR = "/screenshots"
MAX_INTERNAL = 6
SLACK = 16384  # free space to leave on the filesystem
VIEW_UI_MS = 3000
CLOCK_OK_YEAR = 2024  # the RTC reads 2000 until something sets it
NTP_RETRY_MS = 300000

AUTOSTART_KEY = "screengrab_autostart"  # read by the /boot.py snippet, default off
QUIT_KEY = "screengrab_cancel_quits"  # F/CANCEL quits instead of backgrounding
AUTOMOVE_KEY = "screengrab_automove"  # move flash shots onto a card as soon as one is mounted
CARD_KEY = "screengrab_card"  # where new shots go: "flash", a mount name like "sdcard_1", or unset = auto
MENU = ("Move flash to SD", "Delete flash shots", "Start at boot", "F key", "Save to", "Auto move")


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


def _fmt_bytes(n):
    if n >= 1 << 30:
        return "%.1f GB" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%d MB" % (n >> 20)
    return "%d KB" % (n >> 10)


def _short(name):
    """shot_0008_20261024-170312.png -> '0008 20261024-170312'."""
    num, _, rest = name[5:-4].partition("_")
    if len(rest) > 15:
        rest = rest[:8]  # a UUID: its first 8 hex digits are plenty
    return num + " " + rest if rest else num


def _chevron(ctx, x, direction):
    ctx.rgb(0.45, 0.5, 0.65).begin_path()
    ctx.move_to(x - 4 * direction, -7).line_to(x + 4 * direction, 0).line_to(x - 4 * direction, 7)
    ctx.close_path().fill()


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
        self._card_pref = settings.get(CARD_KEY)
        self._automove = settings.get(AUTOMOVE_KEY, False)
        self._am_next = 0  # ticks_ms before which auto move stays idle
        self._am_moved = 0
        self._sizes = {}  # (mount path, fs id) -> total bytes, which never change
        self._queue = []  # flash shots still to move
        self._moved = 0
        # Storage tile: face 0 is the main menu, face 1 the storage tile, whose
        # level 0 lists storages and level 1 the shots on one of them.
        self._face = 0
        self._lvl = 0
        self._stor = []  # (name, dir, shot count, free bytes)
        self._tname = "flash"
        self._ssel = 0
        self._files = []
        self._fsel = 0
        self._fconfirm = False
        self._vcheck = False  # sample the screen once the shown shot is drawn
        self._vdrawn = False
        self._ui_ms = 0  # how much longer the viewer overlay stays up
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

    def _cards(self):
        """Names of top-level mounts that look like storage cards: at least
        MIN_CARD_BYTES, which skips the tiny hexpansion EEPROM mounts. The
        mount table is used rather than statvfs on every root entry, because
        statvfs on the badge's own flash takes ~700 ms each."""
        names = []
        for fs, path in vfs.mount():
            if path == "/" or path.count("/") != 1:
                continue
            key = (path, id(fs))
            if key not in self._sizes:
                try:
                    v = os.statvfs(path)
                    self._sizes[key] = v[1] * v[2]
                except OSError:
                    continue
            if self._sizes[key] >= MIN_CARD_BYTES:
                names.append(path[1:])
        return sorted(names)

    def _sd_dir(self, create=False):
        cards = self._cards()
        if not cards:
            return None
        name = self._card_pref if self._card_pref in cards else cards[0]
        d = "/" + name + "/screenshots"
        if create and not _exists(d):
            os.mkdir(d)
        return d

    def _flash_dir(self, create=False):
        if create and not _exists(INTERNAL_DIR):
            os.mkdir(INTERNAL_DIR)
        return INTERNAL_DIR

    def _target_name(self, cards):
        pref = self._card_pref
        if pref == "flash" or not cards:
            return "flash"
        return pref if pref in cards else cards[0]

    def _target(self):
        name = self._target_name(self._cards())
        if name == "flash":
            return self._flash_dir(create=True), False
        d = "/%s/screenshots" % name
        if not _exists(d):
            os.mkdir(d)
        return d, True

    def _storages(self):
        return [("flash", INTERNAL_DIR)] + [(n, "/%s/screenshots" % n) for n in self._cards()]

    def _shots(self, d):
        try:
            names = os.listdir(d)
        except OSError:
            return []
        return sorted(f for f in names if f.startswith("shot_") and f.endswith(".png"))

    def _next_number(self, shots):
        return int(shots[-1][5:9]) + 1 if shots else 1

    def _free(self, d):
        s = os.statvfs(d)
        return s[0] * s[4]

    def _save(self, fb):
        d, external = self._target()
        shots = self._shots(d)
        # Flash keeps the newest MAX_INTERNAL; a card keeps everything.
        while shots and not external and len(shots) >= MAX_INTERNAL:
            os.remove(d + "/" + shots.pop(0))
        path = "%s/shot_%04d_%s.png" % (d, self._next_number(shots), _stamp())
        # statvfs on the badge flash takes ~700 ms, so don't ask how much room
        # there is: write, and if it runs out, drop the oldest shot and retry.
        while True:
            try:
                write_png(path, fb)
                return path, external
            except OSError:
                try:
                    os.remove(path)
                except OSError:
                    pass
                if not shots:
                    raise
                os.remove(d + "/" + shots.pop(0))

    def _move_file(self, src_dir, name, dst_dir):
        """Copy a shot into dst_dir, renumbered into its sequence (keeping any
        timestamp suffix), verify the copy, then delete the original."""
        if not _exists(dst_dir):
            os.mkdir(dst_dir)
        src = src_dir + "/" + name
        size = os.stat(src)[6]
        if self._free(dst_dir) < size + SLACK:
            raise OSError("destination full")
        dst = "%s/shot_%04d%s" % (dst_dir, self._next_number(self._shots(dst_dir)), name[9:])
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
        return dst

    def _move_one(self):
        """Move the next queued flash shot to the SD card."""
        sd = self._sd_dir()
        if sd is None:
            raise OSError("SD removed")
        self._move_file(INTERNAL_DIR, self._queue[0], sd)
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

    def _auto_move(self):
        """Move flash shots onto a card, oldest first, one per tick, whenever a
        card is mounted. It stays out of the way while the storage tile is
        open, and does nothing if you chose to save to flash."""
        if not self._automove or self._face or self._queue or self._card_pref == "flash":
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._am_next) < 0:
            return
        shots = self._shots(INTERNAL_DIR)
        sd = self._sd_dir() if shots else None
        if sd is None:  # nothing to move, or no card yet: look again shortly
            if self._am_moved:
                eventbus.emit(ShowNotificationEvent(message="Moved %d to SD" % self._am_moved))
                self._am_moved = 0
            self._am_next = time.ticks_add(now, 3000)
            return
        try:
            self._move_file(INTERNAL_DIR, shots[0], sd)
            self._am_moved += 1
        except Exception as e:
            self.status = "Auto move: %s" % e
            self._am_moved = 0
            self._am_next = time.ticks_add(now, 60000)  # e.g. card full: don't hammer it

    def background_update(self, delta):
        fb, self._pending = self._pending, None
        if fb is None:
            self._sync_clock()
            self._auto_move()
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
        if i == 4:
            return "Save to: " + (self._card_pref or "auto")
        if i == 5:
            return "Auto move: " + ("ON" if self._automove else "OFF")
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
        if self._sel == 5:
            self._automove = not self._automove
            settings.set(AUTOMOVE_KEY, self._automove)
            settings.save()
            self._am_next = 0
            self.status = "Moves when SD is in" if self._automove else "Auto move off"
            return
        if self._sel == 4:
            cards = self._cards()
            options = [None, "flash"] + cards
            i = options.index(self._card_pref) if self._card_pref in options else 0
            self._card_pref = options[(i + 1) % len(options)]
            settings.set(CARD_KEY, self._card_pref)
            settings.save()
            self.status = "Saving to " + self._target_name(cards)
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

    def _show(self):
        """A new shot is about to be drawn in the viewer."""
        gc.collect()  # ctx.image() needs ~500 KB to decode, so start with a clean heap
        self._vcheck, self._vdrawn = True, False
        self._ui_ms = VIEW_UI_MS

    def _check_view(self):
        """ctx.image() draws nothing, silently, if it can't decode (too little
        heap). Look at the screen once the shot has been drawn."""
        self._vcheck = False
        fb = display.get_framebuffer()
        # Rows 20-150 only: the overlay text along the bottom is lit anyway.
        if not any(fb[i] or fb[i + 1] for i in range(20 * W * 2, 150 * W * 2, 97)):
            self.status = "Blank or low memory"
            self._ui_ms = VIEW_UI_MS

    def _view_step(self, step):
        self._fsel = (self._fsel + step) % len(self._files)
        self._fconfirm = False
        self.status = ""
        self._show()

    def _scan_storages(self):
        cards = self._cards()
        self._tname = self._target_name(cards)
        self._stor = []
        for name, d in self._storages():
            root = "/" if name == "flash" else "/" + name
            self._stor.append((name, d, len(self._shots(d)), self._free(root)))
        self._ssel = min(self._ssel, len(self._stor) - 1)

    def _open_tile(self):
        self._face, self._lvl, self.status = 1, 0, ""
        self._scan_storages()
        self._ssel = next((i for i, st in enumerate(self._stor) if st[0] == self._tname), 0)

    def _back(self):
        self.status = ""
        self._fconfirm = False
        if self._face == 1 and self._lvl == 2:
            self._lvl = 1
        elif self._face == 1 and self._lvl == 1:
            self._lvl = 0
            self._scan_storages()
        else:
            self._face = 0
            self._cnt_ms = 0

    def _update_tile(self, b):
        down = b.pressed(BUTTON_TYPES["DOWN"]) - b.pressed(BUTTON_TYPES["UP"])
        right = b.pressed(BUTTON_TYPES["RIGHT"])
        if b.pressed(BUTTON_TYPES["LEFT"]):
            self._back()
            return
        confirm = b.pressed(BUTTON_TYPES["CONFIRM"])
        if self._lvl == 0:
            self._ssel = (self._ssel + down) % len(self._stor)
            if down:
                self.status = ""
            name, d, n, free = self._stor[self._ssel]
            if confirm:
                self._card_pref = name
                settings.set(CARD_KEY, name)
                settings.save()
                self._tname = name
                self.status = "Saving to " + name
            elif right:
                if n == 0:
                    self.status = "No shots here"
                else:
                    self._files = self._shots(d)[::-1]  # newest first
                    self._fsel, self._lvl, self.status = 0, 1, ""
            return
        # levels 1 and 2: the shots on one storage, as a list and as a viewer
        name, d, n, free = self._stor[self._ssel]
        if self._lvl == 2 and self._vcheck and self._vdrawn:
            self._check_view()
        if down:
            if self._lvl == 2:
                self._view_step(down)
            else:
                self._fsel = (self._fsel + down) % max(1, len(self._files))
                self._fconfirm = False
                self.status = ""
            return
        if not self._files:
            self.status = "No shots"
            return
        f = self._files[self._fsel]
        if self._lvl == 1:
            if confirm:
                self._lvl, self.status = 2, ""
                self._show()
                return
        elif confirm:
            self._ui_ms = VIEW_UI_MS
            if not self._fconfirm:
                self._fconfirm = True
                self.status = "OK again to delete"
                return
            os.remove(d + "/" + f)
            self._files.pop(self._fsel)
            self._fconfirm = False
            self.status = "Deleted"
            self._after_removal()
            return
        if right:
            others = [st for st in self._stor if st[0] != name]
            if not others:
                self.status = "No other storage"
                return
            dest = next((st for st in others if st[0] == self._tname), others[0])
            self._move_file(d, f, dest[1])
            self._files.pop(self._fsel)
            self.status = "Moved to " + dest[0]
            self._after_removal()

    def _after_removal(self):
        """A shot was deleted or moved away: keep the selection in range."""
        self._ui_ms = VIEW_UI_MS
        if not self._files:
            self._lvl = 1
            return
        self._fsel = min(self._fsel, len(self._files) - 1)
        if self._lvl == 2:
            self._show()

    def update(self, delta):
        b = self.button_states
        if b.get(BUTTON_TYPES["CANCEL"]):
            b.clear()
            if self._face:
                self._back()
            elif self._cancel_quits:
                self._quit()
            else:
                self.minimise()
            return
        if self._face:
            self._ui_ms = max(0, self._ui_ms - delta)
            try:
                self._update_tile(b)
            except Exception as e:
                self.status = "Error: %s" % e
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
        # RIGHT opens the storage tile, but not as half of the LEFT+RIGHT grab.
        if b.pressed(BUTTON_TYPES["RIGHT"]) and not b.get(BUTTON_TYPES["LEFT"]):
            try:
                self._open_tile()
            except Exception as e:
                self.status = "Error: %s" % e
            return
        if b.pressed(BUTTON_TYPES["CONFIRM"]):
            try:
                self._activate()
            except Exception as e:
                self.status = "Error: %s" % e
            self._cnt_ms = 0

    def _draw_view(self, ctx):
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        if self._files:
            ctx.image(self._stor[self._ssel][1] + "/" + self._files[self._fsel], -120, -120, 240, 240)
            self._vdrawn = True
        if self._ui_ms > 0:
            ctx.rgba(0, 0, 0, 0.65).rectangle(-120, 46, 240, 74).fill()
            ctx.font_size = 13
            if self.status:
                ctx.rgb(1.0, 0.8, 0.3).move_to(0, 58).text(self.status[:20])
            elif self._files:
                ctx.rgb(0.9, 0.92, 1.0).move_to(0, 58).text(
                    "%s  (%d/%d)" % (_short(self._files[self._fsel]), self._fsel + 1, len(self._files))
                )
            ctx.font_size = 11
            ctx.rgb(0.6, 0.65, 0.8).move_to(0, 76).text("OK: delete   R: move")

    def _draw_tile(self, ctx):
        if self._lvl == 2:
            self._draw_view(ctx)
            return
        ctx.font_size = 17
        ctx.rgb(0.8, 0.85, 1.0)
        if self._lvl == 0:
            ctx.move_to(0, -82).text("Storage")
            ctx.font_size = 11
            for k in range(3):  # three storages visible at a time
                i = k + max(0, min(self._ssel - 2, len(self._stor) - 3))
                if i >= len(self._stor):
                    break
                name, d, n, free = self._stor[i]
                y = -40 + 30 * k
                sel = i == self._ssel
                if sel:
                    ctx.rgb(1.0, 0.55, 0.0).rectangle(-78, y - 13, 156, 26).fill()
                ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
                if name == self._tname:  # where new shots go
                    ctx.begin_path()
                    ctx.arc(-66, y, 3.5, 0, 2 * math.pi, False)
                    ctx.fill()
                ctx.font_size = 14
                ctx.move_to(0, y - 5).text(name)
                ctx.font_size = 11
                ctx.move_to(0, y + 8).text("%d shots, %s free" % (n, _fmt_bytes(free)))
            hint = "OK: save here   R: files"
        else:
            name, d, n, free = self._stor[self._ssel]
            ctx.move_to(0, -82).text(name)
            ctx.font_size = 12
            ctx.rgb(0.4, 0.85, 0.5).move_to(0, -63).text("%d shots" % len(self._files))
            ctx.font_size = 13
            top = max(0, min(self._fsel - 2, len(self._files) - 5))
            for k in range(5):
                i = top + k
                if i >= len(self._files):
                    break
                y = -42 + 20 * k
                sel = i == self._fsel
                if sel:
                    ctx.rgb(1.0, 0.55, 0.0).rectangle(-76, y - 9, 152, 18).fill()
                ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
                ctx.move_to(0, y).text(_short(self._files[i]))
            hint = "OK: view   R: move"
        ctx.font_size = 12
        if self.status:
            ctx.rgb(1.0, 0.8, 0.3).move_to(0, 58).text(self.status[:20])
        ctx.rgb(0.6, 0.65, 0.8).move_to(0, 80).text(hint)
        _chevron(ctx, -96, -1)

    def draw(self, ctx):
        # The visible area is a circle a bit smaller than the 240px panel, so
        # keep text near the top and bottom short and well inside it.
        ctx.rgb(0.05, 0.06, 0.10).rectangle(-120, -120, 240, 240).fill()
        ctx.save()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        if self._face:
            self._draw_tile(ctx)
            ctx.restore()
            self.draw_overlays(ctx)
            return
        _chevron(ctx, 96, 1)
        ctx.font_size = 17
        ctx.rgb(0.8, 0.85, 1.0).move_to(0, -82).text("Screengrab")
        ctx.font_size = 12
        sd, flash = self._cnt
        ctx.rgb(0.4, 0.85, 0.5).move_to(0, -63).text(
            "SD: %s   Flash: %d" % ("-" if sd is None else sd, flash)
        )
        ctx.font_size = 14
        for i in range(len(MENU)):
            y = -44 + 18 * i
            on, sel = self._enabled(i), i == self._sel
            if sel:
                ctx.rgb(1.0, 0.55, 0.0).rectangle(-76, y - 8, 152, 16).fill()
            if on:
                ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
            else:
                ctx.rgb(0.35, 0.2, 0.0) if sel else ctx.rgb(0.4, 0.42, 0.5)
            ctx.move_to(0, y).text(self._label(i))
        ctx.font_size = 12
        if self.status:
            ctx.rgb(1.0, 0.8, 0.3).move_to(0, 63).text(self.status[:20])
        ctx.rgb(0.6, 0.65, 0.8).move_to(0, 81).text("LEFT+RIGHT = grab")
        ctx.restore()
        self.draw_overlays(ctx)


__app_export__ = ScreenGrab  # pylint: disable=invalid-name
