"""
Screengrab for the Tildagon badge.

Launch once and press F/CANCEL to send it to the background (or switch on
"Start at boot" on the settings tile), then hold LEFT and RIGHT together in any app to
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

from app_components.dialog import TextDialog

from events.input import Buttons, BUTTON_TYPES, ButtonDownEvent, ButtonUpEvent
from events.keyboard import KEYBOARD_BUTTONS
from system.eventbus import eventbus
from system.scheduler.events import RequestForegroundPushEvent
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
MASK_KEY = "screengrab_mask"  # black out the corners outside the round display
CARD_KEY = "screengrab_card"  # where new shots go: "flash", a mount name like "sdcard_1", or unset = auto
DELAY_KEY = "screengrab_delay"  # seconds to wait after LEFT+RIGHT before grabbing; 0 = at once
INTERVAL_KEY = "screengrab_interval"  # seconds between time-lapse shots
DELAYS = (0, 1, 2, 3, 5, 10, 15, 20, 30, 60)
INTERVALS = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 300, 600, 1800, 3600)
NOTIFY_DELAY_S = 5  # a "grab in N s" banner lasts ~3 s, so only show it if it will be gone

WEB_PIN_KEY = "screengrab_web_pin"  # ask for a PIN, default on
WEB_NET_KEY = "screengrab_web_net"  # "wifi" (join the usual network) or "hotspot" (the badge makes its own)
WEB_IDLE_KEY = "screengrab_web_idle"  # minutes without a request before it switches itself off; 0 = never
WEB_AP_KEY = "screengrab_web_appass"  # the hotspot password, made once so phones can remember it
NAME_KEY = "screengrab_web_name"  # the badge's network name: <name>.local on your Wi-Fi
DEFAULT_NAME = "tildagon"
NAME_ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789-"
NAME_MAX = 24
WEB_WORDS_KEY = "screengrab_web_words"  # make that password from four short words instead of 8 characters
WEB_IDLES = (0, 1, 5, 10, 30, 60)
SHOT_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
FREE_REFRESH_MS = {"flash": 120000}  # statvfs on the badge flash takes ~700 ms; cards are instant

MENU = ("Move flash to SD", "Delete flash shots")
SETTINGS = ("Start at boot", "F key", "Save to", "Auto move")
CAPTURE = ("Delay", "Interval", "Round mask", "Timelapse")
WEBROWS = ("Server", "PIN", "Network", "AP pass", "Idle off", "Name")

ICON_GREEN = (0.0, 0.85, 0.3)
ICON_AMBER = (1.0, 0.75, 0.2)
ICON_RED = (1.0, 0.3, 0.3)


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


def valid_name(name):
    """A hostname: lowercase letters, digits and hyphens, not starting or ending with one."""
    return (isinstance(name, str) and 1 <= len(name) <= NAME_MAX and not name.startswith("-") and not name.endswith("-")
            and all(c in NAME_ALLOWED for c in name))


try:
    import network

    # The name the badge came up with. The firmware's mDNS responder takes it once, when Wi-Fi first
    # connects, so a name changed later only becomes <name>.local after the next restart.
    _BOOT_NAME = network.hostname()
except Exception:  # pragma: no cover
    _BOOT_NAME = None


def _wifi_qr(ssid, password):
    """The text a phone's camera understands as "join this Wi-Fi network"."""
    def esc(s):
        return "".join("\\" + c if c in '\\;,":' else c for c in s)

    return "WIFI:T:WPA;S:%s;P:%s;;" % (esc(ssid), esc(password))


def _fmt_bytes(n):
    if n >= 1 << 30:
        return "%.1f GB" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%d MB" % (n >> 20)
    return "%d KB" % (n >> 10)


def _fmt_secs(n):
    if n == 0:
        return "off"
    if n >= 3600 and n % 3600 == 0:
        return "%d h" % (n // 3600)
    if n >= 60 and n % 60 == 0:
        return "%d min" % (n // 60)
    return "%d s" % n


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


def _draw_icon(ctx, cx, cy, r, state, color):
    """A viewfinder icon: corner brackets around a shutter dot. Filled dot while
    saving to a card, a ring while saving to flash, crossed out after a failed
    save."""
    w = r * 1.7
    h = r * 1.25
    arm = r * 0.4
    ctx.save()
    ctx.line_width = max(1.5, r * 0.09)
    ctx.rgb(*color)
    for sx in (-1, 1):
        for sy in (-1, 1):
            x = cx + sx * w / 2
            y = cy + sy * h / 2
            ctx.begin_path()
            ctx.move_to(x, y - sy * arm)
            ctx.line_to(x, y)
            ctx.line_to(x - sx * arm, y)
            ctx.stroke()
    ctx.begin_path()
    ctx.arc(cx, cy, r * 0.3, 0, 2 * math.pi, False)
    if state in ("sd", "rec"):
        ctx.fill()
    else:
        ctx.stroke()
    if state == "failed":
        ctx.begin_path()
        ctx.move_to(cx - w / 2 - r * 0.1, cy - h / 2 - r * 0.1)
        ctx.line_to(cx + w / 2 + r * 0.1, cy + h / 2 + r * 0.1)
        ctx.stroke()
    ctx.restore()


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
        self._mask = settings.get(MASK_KEY, False)
        self._am_next = 0  # ticks_ms before which auto move stays idle
        self._am_moved = 0
        self._failed = False  # the last save failed (drives the icon)
        self._web = None  # the WebServer while it's on (never started at boot)
        self._web_pin_on = settings.get(WEB_PIN_KEY, True)
        self._web_net = settings.get(WEB_NET_KEY, "wifi")
        self._web_idle = settings.get(WEB_IDLE_KEY, 10)
        self._web_words = settings.get(WEB_WORDS_KEY, False)
        name = settings.get(NAME_KEY, DEFAULT_NAME)
        self._name = name if valid_name(name) else DEFAULT_NAME
        self._tdlg = None  # the firmware's TextDialog while the name is being typed
        self._wview = 0  # 0 the option rows, 1 the connection card, 2 the join QR, 3 the sign-in QR
        self._qr = None  # (text, size, modules) of the last QR drawn, so a redraw is free
        self._qr_signins = 0  # the server's sign-in count when the showing code was made
        self._web_pin = None
        self._web_ap = None  # (ssid, password) while the hotspot is up
        self._web_err = ""
        self._free_cache = {}
        self._last_sig = None  # of the last frame drawn, so an unchanged screen isn't redrawn
        self._delay_s = settings.get(DELAY_KEY, 0)
        self._interval_s = settings.get(INTERVAL_KEY, 10)
        self._timer_at = None  # ticks_ms when a delayed grab fires
        self._pending_silent = False  # the pending shot is a time-lapse frame: no banner
        self._tl_on = False  # time-lapse running (never persisted or started at boot)
        self._tl_next = 0
        self._tl_count = 0
        self._editing = False  # UP/DOWN change the highlighted number on the capture tile
        self._sset = 0  # highlighted row on the settings tile
        self._cset = 0  # highlighted row on the capture tile
        self._wset = 0  # highlighted row on the web tile
        self._nav_dir = None  # "L"/"R": the first of LEFT/RIGHT held on the main screen
        self._nav_spoiled = False  # both were held: that's the grab combo, not navigation
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
        eventbus.on_async(RequestForegroundPushEvent, self._on_foreground, self)
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
                self.trigger_capture()
        except Exception as e:
            print("Screengrab:", e)

    def trigger_capture(self, delay=None):
        """Take a screenshot, after the Delay setting unless a delay in seconds
        is given. This is the LEFT+RIGHT grab, and what the web page calls."""
        wait = self._delay_s if delay is None else delay
        if wait:
            self._timer_at = time.ticks_add(time.ticks_ms(), wait * 1000)
            if wait >= NOTIFY_DELAY_S:
                eventbus.emit(ShowNotificationEvent(message="Grab in %d s" % wait))
        else:
            # Grab now, before any notification is drawn; write later.
            self._pending = display.get_framebuffer()
            self._pending_silent = False

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
                write_png(path, fb, self._mask)
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

    # --- web server ---------------------------------------------------------

    def web_start(self):
        """Switch the web server on. Returns "" or why it couldn't start."""
        if self._web:
            return ""
        try:
            import wifi
            from .webserver import WebServer

            if self._web_net == "hotspot":
                pw = settings.get(WEB_AP_KEY) or self._new_ap_password()
                import binascii
                import network

                ssid = "Tildagon-" + binascii.hexlify(network.WLAN(network.STA_IF).config("mac")[-2:]).decode()
                wifi.accesspoint_start(ssid, pw)
                self._web_ap = (ssid, pw)
                ip = wifi.accesspoint_get_ip()  # the hotspot's DHCP offers this as the DNS server too, so phones ask the badge
                network.WLAN(network.AP_IF).ifconfig((ip, "255.255.255.0", ip, ip))
            elif not wifi.status():
                self._web_err = "No Wi-Fi"
                return self._web_err
            self._web_pin = "%06d" % (int.from_bytes(os.urandom(4), "big") % 1000000) if self._web_pin_on else None
            gc.collect()
            self.apply_name()
            host = wifi.accesspoint_get_ip() if self._web_ap else wifi.get_ip()
            self._web = WebServer(self, self._web_pin, self._web_idle * 60, host=host, captive=self._web_ap is not None)
            self._web.start()
            self._web_err = ""
            self._wview = 1  # show the big connection card
        except Exception as e:
            self._web_err = str(e)[:40] or "error"
            self.web_stop()
            return self._web_err
        return ""

    def _new_ap_password(self):
        """Make and keep a hotspot password in the chosen style (kept, so a phone can remember it)."""
        from .webserver import passphrase, password

        pw = passphrase(4) if self._web_words else password(8)
        settings.set(WEB_AP_KEY, pw)
        settings.save()
        return pw

    def apply_name(self):
        """Use the saved name as the hostname. The router sees it at the next connection."""
        try:
            import network

            network.hostname(self._name)
        except Exception as e:
            print("Screengrab: hostname:", e)

    def name_live(self):
        """Is <name>.local answering already, or only after a restart?"""
        return _BOOT_NAME == self._name

    def web_stop(self):
        if self._web:
            self._web.stop()
            self._web = None
        if self._web_ap:
            try:
                import wifi

                wifi.accesspoint_stop()
            except Exception:
                pass
            self._web_ap = None
        gc.collect()

    def web_idle_off(self):
        self._wview = 0
        self.web_stop()
        eventbus.emit(ShowNotificationEvent(message="Web server off (idle)"))

    def web_url(self):
        if not self._web:
            return ""
        import wifi

        ip = wifi.accesspoint_get_ip() if self._web_ap else wifi.get_ip()
        return "http://%s%s" % (ip, "" if self._web.port == 80 else ":%d" % self._web.port)

    def _free_cached(self, name):
        now = time.ticks_ms()
        ent = self._free_cache.get(name)
        if ent is None or time.ticks_diff(now, ent[0]) > FREE_REFRESH_MS.get(name, 5000):
            try:
                ent = (now, self._free("/" if name == "flash" else "/" + name))
            except OSError:
                ent = (now, None)
            self._free_cache[name] = ent
        return ent[1]

    def web_status(self, collect=False):
        if collect:
            gc.collect()  # so the heap figure is what's really free, not what's awaiting a sweep
        cards = self._cards()
        return {
            "target": self._target_name(cards),
            "storages": [
                {"name": n, "count": len(self._shots(d)), "free": self._free_cached(n)}
                for n, d in self._storages()
            ],
            "timelapse": {"on": self._tl_on, "count": self._tl_count},
            "delay": self._delay_s,
            "interval": self._interval_s,
            "mask": self._mask,
            "save_to": self._card_pref,
            "delays": DELAYS,
            "failed": self._failed,
            "taken": self.count,
            "requests": self._web.requests if self._web else 0,
            "name": self._name,
            "name_live": self.name_live(),
            "clock": time.localtime()[0] >= CLOCK_OK_YEAR,
            "heap": gc.mem_free(),
            "uptime": time.ticks_ms() // 1000,
        }

    def _storage_dir(self, storage):
        for n, d in self._storages():
            if n == storage:
                return d
        return None

    def web_shot_path(self, storage, name):
        """The real path of a shot, or None. The only way the web server turns
        a name from a request into a file, so it checks both parts."""
        d = self._storage_dir(storage)
        if not d or not (name and name.startswith("shot_") and name.endswith(".png") and len(name) <= 80):
            return None
        if any(ch not in SHOT_CHARS for ch in name) or ".." in name:
            return None
        path = d + "/" + name
        return path if _exists(path) else None

    def web_shots(self, storage, offset=0, limit=60):
        storage = storage or self._target_name(self._cards())
        d = self._storage_dir(storage)
        if d is None:
            return {"storage": storage, "total": 0, "offset": 0, "shots": []}
        names = self._shots(d)[::-1]  # newest first
        offset, limit = max(0, offset), max(1, min(100, limit))
        return {
            "storage": storage,
            "total": len(names),
            "offset": offset,
            "shots": [{"n": n, "s": os.stat(d + "/" + n)[6]} for n in names[offset:offset + limit]],
        }

    def _forget_file(self, d, name):
        """Keep the Storage tile's list straight when a shot goes away behind it."""
        self._cnt_ms = 0
        if self._face == 1 and self._lvl >= 1 and self._stor and self._stor[self._ssel][1] == d and name in self._files:
            self._files.remove(name)
            self._fsel = min(self._fsel, max(0, len(self._files) - 1))
            if not self._files:
                self._lvl = 1

    def web_delete(self, storage, name):
        path = self.web_shot_path(storage, name)
        if not path:
            return "no such shot"
        os.remove(path)
        self._forget_file(self._storage_dir(storage), name)
        return ""

    def web_move(self, storage, name, to):
        if not self.web_shot_path(storage, name):
            return "no such shot"
        dst = self._storage_dir(to)
        if not dst or to == storage:
            return "pick a different, existing storage"
        try:
            self._move_file(self._storage_dir(storage), name, dst)
        except OSError as e:
            return str(e)
        self._forget_file(self._storage_dir(storage), name)
        return ""

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
        if not self._automove or self._face == 1 or self._queue or self._card_pref == "flash":
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

    def _timers(self):
        """Fire the self-timer and the time-lapse when they are due."""
        now = time.ticks_ms()
        if self._timer_at is not None and time.ticks_diff(now, self._timer_at) >= 0:
            self._timer_at = None
            self._pending = display.get_framebuffer()
            self._pending_silent = False
        elif self._tl_on and time.ticks_diff(now, self._tl_next) >= 0:
            self._tl_next = time.ticks_add(now, self._interval_s * 1000)
            if self._target_name(self._cards()) == "flash":
                self._stop_timelapse("no card")
            else:
                self._pending = display.get_framebuffer()
                self._pending_silent = True

    def _stop_timelapse(self, why=""):
        self._tl_on = False
        msg = "Timelapse stopped: %d shots" % self._tl_count
        eventbus.emit(ShowNotificationEvent(message=msg + (" (%s)" % why if why else "")))

    def background_update(self, delta):
        self._timers()
        fb, self._pending = self._pending, None
        if fb is None:
            self._sync_clock()
            self._auto_move()
            return
        silent = self._pending_silent
        try:
            self.last, external = self._save(fb)
            self.count += 1
            if silent:
                self._tl_count += 1
            self._failed = False
            self.status = ""
            msg = "Saved to %s: %s" % ("SD" if external else "flash", self.last.rsplit("/", 1)[1])
        except Exception as e:
            self._failed = True
            self.status = "Failed: %s" % e
            msg = "Screengrab failed"
            print("Screengrab:", e)
            if silent:
                self._stop_timelapse("save failed")
                return
        if not silent or self._failed:
            eventbus.emit(ShowNotificationEvent(message=msg))

    # --- ui ---------------------------------------------------------------

    def _counts(self):
        sd = self._sd_dir()
        return (len(self._shots(sd)) if sd else None), len(self._shots(self._flash_dir()))

    def _enabled(self, i):
        sd, flash = self._cnt
        return i >= 2 or flash > 0 and (sd is not None if i == 0 else True)

    def _slabel(self, i):
        if i == 0:
            return "Start at boot: " + ("ON" if self._autostart else "OFF")
        if i == 1:
            return "F key: " + ("quit" if self._cancel_quits else "background")
        if i == 2:
            return "Save to: " + (self._card_pref or "auto")
        return "Auto move: " + ("ON" if self._automove else "OFF")

    def _clabel(self, i):
        if i == 2:
            return "Round mask: " + ("ON" if self._mask else "OFF")
        if i == 3:
            return "Stop timelapse (%d)" % self._tl_count if self._tl_on else "Start timelapse"
        value = _fmt_secs(self._delay_s if i == 0 else self._interval_s)
        if self._editing and i == self._cset:
            value = "< %s >" % value
        return ("Delay: " if i == 0 else "Interval: ") + value

    def _wlabel(self, i):
        if i == 0:
            return "Server: " + ("ON" if self._web else "OFF")
        if i == 1:
            return "PIN: " + ("ON" if self._web_pin_on else "OFF")
        if i == 2:
            return "Network: " + ("Hotspot" if self._web_net == "hotspot" else "Wi-Fi")
        if i == 3:
            return "AP pass: " + ("4 words" if self._web_words else "8 chars")
        if i == 5:
            return "Name: " + self._name
        value = "never" if not self._web_idle else "%d min" % self._web_idle
        if self._editing and i == self._wset:
            value = "< %s >" % value
        return "Idle off: " + value

    def _qr_code(self):
        """Make a new one-time sign-in code, if signing in needs one."""
        web = self._web
        if web and web.pin:
            web.new_qr_token()
            self._qr_signins = web.signins

    def _wrestart(self):
        """Apply a changed web option to a server that is already running."""
        if self._web:
            self.web_stop()
            self.status = self.web_start() or "Restarted"

    def _wtoggle(self, i):
        """Act on row i of the web tile."""
        if i == 0:
            if self._web:
                self.web_stop()
                self.status = "Web server off"
            else:
                self.status = self.web_start() or "Web server on"
        elif i == 1:
            self._web_pin_on = not self._web_pin_on
            settings.set(WEB_PIN_KEY, self._web_pin_on)
            settings.save()
            self._wrestart()
            self.status = "PIN required" if self._web_pin_on else "Open: no PIN!"
        elif i == 2:
            self._web_net = "wifi" if self._web_net == "hotspot" else "hotspot"
            settings.set(WEB_NET_KEY, self._web_net)
            settings.save()
            self._wrestart()
            self.status = "Badge Wi-Fi hotspot" if self._web_net == "hotspot" else "Your Wi-Fi"
        elif i == 3:
            self._web_words = not self._web_words
            settings.set(WEB_WORDS_KEY, self._web_words)
            self._new_ap_password()  # a new style means a new password; phones must rejoin
            self._wrestart()
            self.status = "New: 4 short words" if self._web_words else "New: 8 characters"
        elif i == 5:
            self._name_dialog()
        else:
            self._editing = True
            self.status = "UP/DOWN to change"

    def _name_dialog(self):
        """Ask for the name with the firmware's own text entry: the badge's buttons pick
        letters, and a USB keyboard types straight into it."""
        dlg = TextDialog("Badge name", self, on_complete=self._name_done, on_cancel=self._name_cancel)
        dlg.text = self._name
        self._tdlg = dlg
        self.overlays.append(dlg)
        eventbus.on(ButtonDownEvent, self._name_extra_keys, self)

    def _name_close(self):
        if self._tdlg is not None:
            if self._tdlg in self.overlays:
                self.overlays.remove(self._tdlg)
            eventbus.remove(ButtonDownEvent, self._name_extra_keys, self)
            self._tdlg = None
        self.button_states.clear()

    def _name_extra_keys(self, event):
        """Enter and Escape on a USB keyboard: the firmware dialog doesn't act on those."""
        dlg = self._tdlg
        if dlg is None or dlg._result is not None:
            return
        if KEYBOARD_BUTTONS["ENTER"] in event.button and event.button.find_parent_in_group("Keyboard"):
            dlg._cleanup()
            dlg._result = dlg.text
            self._name_done()
        elif KEYBOARD_BUTTONS["ESCAPE"] in event.button and event.button.find_parent_in_group("Keyboard"):
            dlg._cleanup()
            dlg._result = False
            self._name_cancel()

    def _name_done(self):
        text = (self._tdlg.text if self._tdlg else "").strip().lower()
        self._name_close()
        if not valid_name(text):
            self.status = "Use a-z 0-9 - (24 max)"
            return
        self._name = text
        settings.set(NAME_KEY, text)
        settings.save()
        self.apply_name()
        self.status = "Saved" if self.name_live() else "Saved: restart for .local"

    def _name_cancel(self):
        self._name_close()
        self.status = "Name unchanged"

    def _wnudge(self, step):
        i = min(range(len(WEB_IDLES)), key=lambda k: abs(WEB_IDLES[k] - self._web_idle))
        self._web_idle = WEB_IDLES[max(0, min(len(WEB_IDLES) - 1, i + step))]
        settings.set(WEB_IDLE_KEY, self._web_idle)
        if self._web:
            self._web.idle_s = self._web_idle * 60
            self._web.last_active = time.ticks_ms()
        self.status = "Switches off after %d min" % self._web_idle if self._web_idle else "Stays on"

    def _update_web(self, b, tap):
        step = b.pressed(BUTTON_TYPES["DOWN"]) - b.pressed(BUTTON_TYPES["UP"])
        confirm = b.pressed(BUTTON_TYPES["CONFIRM"])
        if self._wview and not self._web:
            self._wview = 0
        if self._wview:  # the connection card and its QR pages
            if step > 0:
                self._wview = 0
                return
            if step < 0:  # back a page
                self._wview = 1 if self._wview == 3 and not self._web_ap else max(1, self._wview - 1)
                return
            if confirm:
                if self._wview == 1:
                    self._wview = 2 if self._web_ap else 3
                elif self._wview == 2:
                    self._wview = 3
                if self._wview == 3:
                    self._qr_code()  # a fresh one-time code each time the sign-in page is opened or asked for
                return
        elif self._editing:
            if step:
                self._wnudge(-step)
            if tap == "R" or confirm:
                self._editing = False
                settings.save()
                self.status = ""
            return
        if tap == "R":
            self._back()
            return
        if tap == "L":
            self._face, self.status = 2, ""
            return
        if self._wview:
            return
        if step < 0 and self._wset == 0 and self._web:  # UP from the top row goes back to the card
            self._wview = 1
            return
        if step:
            self._wset = (self._wset + step) % len(WEBROWS)
            self.status = ""
        if confirm:
            self._wtoggle(self._wset)

    def _toggle(self, i):
        """Change setting i on the settings tile."""
        if i == 0:
            self._autostart = not self._autostart
            settings.set(AUTOSTART_KEY, self._autostart)
            self.status = "Applies next boot"
        elif i == 1:
            self._cancel_quits = not self._cancel_quits
            settings.set(QUIT_KEY, self._cancel_quits)
            self.status = "F quits" if self._cancel_quits else "F backgrounds"
        elif i == 2:
            cards = self._cards()
            options = [None, "flash"] + cards
            k = options.index(self._card_pref) if self._card_pref in options else 0
            self._card_pref = options[(k + 1) % len(options)]
            settings.set(CARD_KEY, self._card_pref)
            self._tname = self._target_name(cards)
            self.status = "Saving to " + self._tname
        else:
            self._automove = not self._automove
            settings.set(AUTOMOVE_KEY, self._automove)
            self._am_next = 0
            self.status = "Moves when SD is in" if self._automove else "Auto move off"
        settings.save()

    def _ctoggle(self, i):
        """Act on row i of the capture tile."""
        if i < 2:
            self._editing = True  # UP/DOWN now change the number; OK or RIGHT/F finishes
            self.status = "UP/DOWN to change"
        elif i == 2:
            self._mask = not self._mask
            settings.set(MASK_KEY, self._mask)
            settings.save()
            self.status = "Corners masked" if self._mask else "Full square"
        else:
            self.set_timelapse(not self._tl_on)

    def set_timelapse(self, on):
        """Start or stop the time-lapse. Returns "" or why it didn't start."""
        if not on:
            if self._tl_on:
                self._stop_timelapse()
            self.status = "Timelapse stopped"
        elif self._tl_on:
            pass
        elif self._target_name(self._cards()) == "flash":
            self.status = "Needs an SD card"
            return self.status
        else:
            self._tl_on, self._tl_count = True, 0
            self._tl_next = time.ticks_add(time.ticks_ms(), self._interval_s * 1000)
            self.status = "First shot in %d s" % self._interval_s
        return ""

    def set_option(self, name, value):
        """Change a capture option from outside the tiles (the web page).
        Returns "" or what was wrong with the value."""
        if name == "delay":
            if not isinstance(value, int) or value not in DELAYS:
                return "delay must be one of %s" % (DELAYS,)
            self._delay_s = value
            settings.set(DELAY_KEY, value)
        elif name == "interval":
            if not isinstance(value, int) or not 1 <= value <= 3600:
                return "interval must be 1-3600 s"
            self._interval_s = value
            settings.set(INTERVAL_KEY, value)
        elif name == "mask":
            if not isinstance(value, bool):
                return "mask must be true or false"
            self._mask = value
            settings.set(MASK_KEY, value)
        elif name == "name":
            if not valid_name(value):
                return "name: 1-24 lowercase letters, digits or hyphens, not starting or ending with a hyphen"
            self._name = value
            settings.set(NAME_KEY, value)
            self.apply_name()
        elif name == "save_to":
            cards = self._cards()
            if value not in [None, "flash"] + cards:
                return "save_to must be null, flash or a card"
            self._card_pref = value
            settings.set(CARD_KEY, value)
            self._tname = self._target_name(cards)
        else:
            return "unknown option"
        settings.save()
        return ""

    def _nudge(self, step):
        """Move the number being edited one rung up or down its ladder."""
        ladder = DELAYS if self._cset == 0 else INTERVALS
        cur = self._delay_s if self._cset == 0 else self._interval_s
        i = min(range(len(ladder)), key=lambda k: abs(ladder[k] - cur))
        value = ladder[max(0, min(len(ladder) - 1, i + step))]
        if self._cset == 0:
            self._delay_s = value
            settings.set(DELAY_KEY, value)
            self.status = "Grab after %s" % _fmt_secs(value) if value else "Grab at once"
        else:
            self._interval_s = value
            settings.set(INTERVAL_KEY, value)
            self.status = "Shot every %s" % _fmt_secs(value)

    def _finish_edit(self):
        self._editing = False
        settings.save()
        self.status = ""

    def _update_settings(self, b, tap):
        if tap == "R":  # the settings tile sits left of the capture tile
            self._back()
            return
        step = b.pressed(BUTTON_TYPES["DOWN"]) - b.pressed(BUTTON_TYPES["UP"])
        if step:
            self._sset = (self._sset + step) % len(SETTINGS)
            self.status = ""
        if b.pressed(BUTTON_TYPES["CONFIRM"]):
            self._toggle(self._sset)

    def _update_capture(self, b, tap):
        step = b.pressed(BUTTON_TYPES["DOWN"]) - b.pressed(BUTTON_TYPES["UP"])
        confirm = b.pressed(BUTTON_TYPES["CONFIRM"])
        if self._editing:
            if step:
                self._nudge(-step)  # UP raises the number, DOWN lowers it
            if tap == "R" or confirm:
                self._finish_edit()
            return
        if tap == "R":
            self._back()
            return
        if tap == "L":
            self._face, self.status = 4, ""
            return
        if step:
            self._cset = (self._cset + step) % len(CAPTURE)
            self.status = ""
        if confirm:
            self._ctoggle(self._cset)

    def _quit(self):
        """Stop listening for the combo and close the app."""
        self._name_close()
        self.web_stop()
        main = ScreenGrab._primary
        ScreenGrab._primary = None
        if main is not None:
            eventbus.deregister(main._sink)
            if main is not self:
                main.terminate()
        self.terminate()

    def _activate(self):
        self._cnt = self._counts()
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
        if self._face == 3 and self._editing:
            self._finish_edit()
            return
        if self._face == 4 and self._editing:
            self._editing = False
            settings.save()
            self.status = ""
            return
        self.status = ""
        self._fconfirm = False
        if self._face == 2:  # settings -> web
            self._face = 4
        elif self._face == 4:  # web -> capture
            self._face = 3
        elif self._face == 1 and self._lvl == 2:
            self._lvl = 1
        elif self._face == 1 and self._lvl == 1:
            self._lvl = 0
            self._scan_storages()
        else:
            self._face = 0
            self._cnt_ms = 0
            b = self.button_states
            if b.get(BUTTON_TYPES["LEFT"]) or b.get(BUTTON_TYPES["RIGHT"]):
                # The button that brought us back is still down: its release
                # must not count as navigation on the main screen.
                self._nav_dir, self._nav_spoiled = "X", True

    def _update_tile(self, b, tap):
        down = b.pressed(BUTTON_TYPES["DOWN"]) - b.pressed(BUTTON_TYPES["UP"])
        right = tap == "R"
        if tap == "L":
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

    def _lr_tap(self, b):
        """Returns "L" or "R" when LEFT or RIGHT is released on its own. Tiles
        act on that rather than on the press, because LEFT+RIGHT together is
        the grab combo: a release only counts if the other was never held too."""
        left = b.get(BUTTON_TYPES["LEFT"])
        right = b.get(BUTTON_TYPES["RIGHT"])
        if left and right:
            self._nav_spoiled = True
        if left or right:
            if self._nav_dir is None:
                self._nav_dir = "L" if left else "R"
            return None
        direction, spoiled = self._nav_dir, self._nav_spoiled
        self._nav_dir, self._nav_spoiled = None, False
        return None if spoiled or direction not in ("L", "R") else direction

    async def _on_foreground(self, _):
        """Back in front: the screen holds another app's last frame, so draw again."""
        self._last_sig = None

    def _sig(self):
        """What the current screen depends on: if this hasn't changed, there is
        nothing to redraw, and a redraw costs the whole badge ~90 ms."""
        web = self._web
        return (
            self._face, self._lvl, self._sel, self._ssel, self._sset, self._cset, self._wset, self._wview, self._web_words, self._fsel,
            self.status, self._cnt, self._tname, self._tl_on, self._tl_count, self._failed, self._editing,
            self._ui_ms > 0, web is not None, web and (web.idle_left(), web.requests),
            self._web_err, web and (web.qr_valid(), web.signins), self._name, self._delay_s, self._interval_s, self._mask, self._autostart, self._cancel_quits,
            self._card_pref, self._automove, self._web_pin_on, self._web_net, self._web_idle, bool(self._queue),
            len(self._files), self._stor and self._stor[self._ssel][2:] or None,
        )

    def update(self, delta):
        if self._tdlg is not None:
            self._update(delta)
            return  # redraw every frame while the dialog is open
        if self._update(delta) is False:
            return False
        sig = self._sig()
        if sig == self._last_sig:
            return False
        self._last_sig = sig

    def _update(self, delta):
        b = self.button_states
        if self._tdlg is not None:  # the text dialog is handling every key; keep out of its way (and keep redrawing it)
            b.clear()
            return
        tap = self._lr_tap(b)
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
                if self._face == 1:
                    self._update_tile(b, tap)
                elif self._face == 2:
                    self._update_settings(b, tap)
                elif self._face == 4:
                    self._update_web(b, tap)
                else:
                    self._update_capture(b, tap)
            except Exception as e:
                self.status = "Error: %s" % e
            return
        self._cnt_ms -= delta
        if self._cnt_ms <= 0 or self._queue:
            self._cnt = self._counts()
            self._tname = self._target_name(self._cards())
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
        if tap == "R":
            try:
                self._open_tile()
            except Exception as e:
                self.status = "Error: %s" % e
            return
        if tap == "L":
            self._face, self._cset, self.status = 3, 0, ""
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

    def _draw_rows(self, ctx, title, labels, selected, hint):
        ctx.font_size = 17
        ctx.rgb(0.8, 0.85, 1.0).move_to(0, -82).text(title)
        ctx.font_size = 14
        for i in range(len(labels)):
            y = -34 + 26 * i
            sel = i == selected
            if sel:
                ctx.rgb(1.0, 0.55, 0.0).rectangle(-80, y - 11, 160, 22).fill()
            ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
            ctx.move_to(0, y).text(labels[i])
        ctx.font_size = 12
        if self.status:
            ctx.rgb(1.0, 0.8, 0.3).move_to(0, 66).text(self.status[:20])
        ctx.rgb(0.6, 0.65, 0.8).move_to(0, 82).text(hint)

    def _draw_card(self, ctx):
        """Everything needed to connect, big enough to read across a table."""
        web = self._web
        dim, white, amber, green = (0.55, 0.6, 0.78), (0.97, 0.97, 1.0), (1.0, 0.82, 0.25), ICON_GREEN
        ctx.font_size = 15
        ctx.rgb(*dim).move_to(0, -88).text("Connect")

        def line(text, y, size, colour):
            ctx.font_size = size
            ctx.rgb(*colour).move_to(0, y).text(text)

        pin = ("PIN " + self._web_pin, white) if self._web_pin else ("No PIN - open!", ICON_RED)
        if self._web_ap:
            ssid, pw = self._web_ap
            line("Join this Wi-Fi", -68, 11, dim)
            line(ssid, -51, 19, white)
            line("password", -34, 11, dim)
            if "-" in pw:  # four words: two to a line, the hyphen at the break is part of it
                parts = pw.split("-")
                line("-".join(parts[:2]) + "-", -15, 22, amber)
                line("-".join(parts[2:]), 8, 22, amber)
            else:
                line(pw, -9, 26, amber)
            line("mobile data off, then open:", 28, 11, dim)
            line(self.web_url().replace("http://", ""), 43, 17, green)
            line(pin[0], 65, 19, pin[1])
        else:
            line("Open in a browser", -68, 12, dim)
            line(self.web_url().replace("http://", ""), -48, 21, green)
            nm = self._name + ".local"
            line("or " + (nm if self.name_live() else nm + " (after restart)"), -28, 12, (0.35, 0.7, 0.45))
            line(pin[0], 14, 30 if self._web_pin else 18, pin[1])
            left = web.idle_left()
            if left is not None:
                line("off in %d:%02d" % (left // 60, left % 60), 52, 13, dim)
            line("%d requests" % web.requests, 67, 11, dim)
        line("OK: QR codes", 84, 11, dim)

    def _qr_matrix(self, text):
        if not self._qr or self._qr[0] != text:
            from . import qr

            size, modules = qr.best(text)
            self._qr = (text, size, modules)
        return self._qr[1], self._qr[2]

    def _draw_qr_page(self, ctx, join):
        web = self._web
        ctx.font_size = 12
        ctx.rgb(0.55, 0.6, 0.78).move_to(0, -92).text("1  Join the Wi-Fi" if join else ("2  Sign in" if self._web_ap else "Sign in"))
        if join:
            text = _wifi_qr(*self._web_ap)
        elif web.pin:
            if not web.qr_valid():
                ctx.font_size = 22
                ctx.rgb(*ICON_GREEN if web.signins > self._qr_signins else ICON_AMBER).move_to(0, -18).text(
                    "Signed in" if web.signins > self._qr_signins else "Code expired")
                ctx.font_size = 13
                ctx.rgb(0.8, 0.85, 1.0).move_to(0, 14).text("OK: make another code")
                return
            text = "%s/q?t=%s" % (self.web_url(), web.qr_token)
        else:
            text = self.web_url()  # no PIN, so there is nothing to sign in with: just the address
        size, modules = self._qr_matrix(text)
        m = max(3, min(7, 150 // size))  # as big as fits, with the code's corners inside the round glass
        side = size * m
        x0, y0 = -(side // 2), -(side // 2) + 3
        q = 2 * m  # the white margin a scanner needs
        ctx.rgb(1, 1, 1).rectangle(x0 - q, y0 - q, side + 2 * q, side + 2 * q).fill()
        ctx.rgb(0, 0, 0)
        for y in range(size):  # a row at a time: one path of every run overflows the graphics buffer and drops the rest
            row, x = modules[y], 0
            ctx.begin_path()
            while x < size:
                if row[x]:
                    end = x
                    while end < size and row[end]:
                        end += 1
                    ctx.rectangle(x0 + x * m, y0 + y * m, (end - x) * m, m)
                    x = end
                else:
                    x += 1
            ctx.fill()

    def _draw_web(self, ctx):
        if self._wview >= 2 and self._web:
            self._draw_qr_page(ctx, self._wview == 2 and self._web_ap is not None)
            return
        if self._wview and self._web:
            self._draw_card(ctx)
            _chevron(ctx, 96, 1)  # capture
            _chevron(ctx, -96, -1)  # settings
            return
        ctx.font_size = 17
        ctx.rgb(0.8, 0.85, 1.0).move_to(0, -88).text("Web")
        ctx.font_size = 11
        if self._web:
            ctx.rgb(*ICON_GREEN).move_to(0, -69).text("on: " + self.web_url().replace("http://", ""))
        else:
            ctx.rgb(0.55, 0.6, 0.78).move_to(0, -69).text("off: browse, mirror, control")
        ctx.font_size = 14
        ctx.font_size = 13
        for i in range(len(WEBROWS)):
            y = -50 + 19 * i
            sel = i == self._wset
            if sel:
                ctx.rgb(1.0, 0.55, 0.0).rectangle(-82, y - 9, 164, 18).fill()
            ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
            ctx.move_to(0, y).text(self._wlabel(i))
        ctx.font_size = 11
        if self.status or self._web_err:
            ctx.rgb(*((1.0, 0.8, 0.3) if self.status else ICON_RED)).move_to(0, 68).text((self.status or self._web_err)[:26])
        if self._web:
            ctx.rgb(0.55, 0.6, 0.78).move_to(0, 84).text("UP: show details")
        _chevron(ctx, 96, 1)  # capture
        _chevron(ctx, -96, -1)  # settings

    def _draw_settings(self, ctx):
        self._draw_rows(ctx, "Settings", [self._slabel(i) for i in range(len(SETTINGS))], self._sset, "OK: change")
        _chevron(ctx, 96, 1)  # back toward the web tile

    def _draw_capture(self, ctx):
        hint = "UP/DOWN  OK: done" if self._editing else "OK: change"
        self._draw_rows(ctx, "Capture", [self._clabel(i) for i in range(len(CAPTURE))], self._cset, hint)
        _chevron(ctx, 96, 1)  # main
        _chevron(ctx, -96, -1)  # settings

    def draw(self, ctx):
        # The visible area is a circle a bit smaller than the 240px panel, so
        # keep text near the top and bottom short and well inside it.
        ctx.rgb(0.05, 0.06, 0.10).rectangle(-120, -120, 240, 240).fill()
        ctx.save()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        if self._face == 1:
            self._draw_tile(ctx)
            ctx.restore()
            self.draw_overlays(ctx)
            return
        if self._face in (2, 3, 4):
            if self._face == 2:
                self._draw_settings(ctx)
            elif self._face == 4:
                self._draw_web(ctx)
            else:
                self._draw_capture(ctx)
            ctx.restore()
            self.draw_overlays(ctx)
            return
        _chevron(ctx, 96, 1)  # storage tile
        _chevron(ctx, -96, -1)  # capture tile
        ctx.font_size = 16
        ctx.rgb(*((0.4, 0.85, 1.0) if self._web else (0.8, 0.85, 1.0))).move_to(0, -86).text("Screengrab")
        if self._tl_on:
            state, color, label = "rec", ICON_RED, "Timelapse %s: %d" % (_fmt_secs(self._interval_s), self._tl_count)
        elif self._failed:
            state, color, label = "failed", ICON_RED, "Last save failed"
        elif self._tname == "flash":
            state, color, label = "flash", ICON_AMBER, "Saving to flash"
        else:
            state, color, label = "sd", ICON_GREEN, "Saving to " + self._tname
        _draw_icon(ctx, 0, -50, 26, state, color)
        ctx.font_size = 12
        ctx.rgb(*color).move_to(0, -20).text(label[:22])
        sd, flash = self._cnt
        ctx.font_size = 17  # shot counts, a row of their own just above the options
        ctx.rgb(*(ICON_GREEN if sd is not None else (0.4, 0.42, 0.5))).move_to(-44, 6).text(
            "SD: %s" % ("-" if sd is None else sd)
        )
        ctx.rgb(*ICON_AMBER).move_to(40, 6).text("Flash: %d" % flash)
        ctx.font_size = 15
        for i in range(len(MENU)):
            y = 30 + 26 * i
            on, sel = self._enabled(i), i == self._sel
            if sel:
                ctx.rgb(1.0, 0.55, 0.0).rectangle(-76, y - 11, 152, 22).fill()
            if on:
                ctx.rgb(0.0, 0.0, 0.0) if sel else ctx.rgb(0.9, 0.92, 1.0)
            else:
                ctx.rgb(0.35, 0.2, 0.0) if sel else ctx.rgb(0.4, 0.42, 0.5)
            ctx.move_to(0, y).text(MENU[i])
        ctx.font_size = 12
        if self.status:
            ctx.rgb(1.0, 0.8, 0.3).move_to(0, 78).text(self.status[:20])
        ctx.font_size = 11
        ctx.rgb(0.6, 0.65, 0.8).move_to(0, 91).text("L+R = grab")
        ctx.restore()
        self.draw_overlays(ctx)


__app_export__ = ScreenGrab  # pylint: disable=invalid-name
