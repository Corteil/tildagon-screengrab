# Screengrab

Tildagon badge app that saves a screenshot when you hold **LEFT + RIGHT** in
any app.

> **Firmware requirement:** Screengrab needs `display.get_framebuffer()`.
> Some firmware builds, including custom ones, don't have it. There the app
> can't take screenshots: it shows a WARNING screen and a notification instead
> of its menu, and does nothing else.

1. Launch **Screengrab** from the Apps menu, then press CANCEL to send it to
   the background.
2. In any app, hold LEFT and RIGHT together. A "Saved to SD: shot_000N_….png"
   notification confirms it.

Screenshots are 240x240 PNGs named `shot_<number>_<YYYYMMDD-HHMMSS>.png`
(UTC). The badge clock reads 2000 until it is set, so the app tries NTP over
Wi-Fi every 5 minutes; until then a random UUID (the part of a
`urn:uuid:` reference after the prefix) stands in, e.g.
`shot_0003_3f2b8c1e-5a47-4d0e-9b61-7c2d4e8a1f90.png`. They go to a mounted storage card if there is one, otherwise to `/screenshots`
in badge flash, where the newest 6 are kept. A card is any top-level mount of
at least 256 KiB, read from the mount table, so it doesn't matter which port
the hexpansion is in or what its app calls the mount (`/sdcard`, `/sdcard_1`,
...). The tiny hexpansion EEPROM mounts are skipped. Shots go in a
`screenshots` folder on the card, with no limit.

Open the app for its main screen (UP/DOWN to choose, CONFIRM to run). The
other screens are tiles in a row, with a chevron on the edge showing the way:

    Settings  <-  Web  <-  Capture  <-  Main  ->  Storage

- **LEFT** moves one tile left (main -> Capture -> Web -> Settings), and
  **RIGHT** moves back. **RIGHT** on the main screen opens the Storage tile, where LEFT
  comes back. F also steps back, one tile at a time.
- LEFT and RIGHT together are the screenshot combo, so on every tile a button
  only acts when it is released, and not if the other one was held too.

The main screen shows a viewfinder icon, coloured like the HDMI Manager's
status icon: **green** with a filled dot when new shots go to a card, **amber**
with a ring when they go to flash, and **red** with a slash after a failed
save, or a solid red dot while a time-lapse is running. Under it are the
target, the shot counts and two items:

- **Move flash to SD** copies the flash shots onto the SD card (renumbered
  into its sequence) and deletes them from flash.
- **Delete flash shots** deletes them all after a second CONFIRM.

**Capture tile** (UP/DOWN to choose, CONFIRM to change):

- **Delay** waits this long after LEFT+RIGHT before grabbing, so you can
  switch to another screen or set something up first. OK, then UP/DOWN to
  change it (off, 1, 2, 3, 5, 10, 15, 20, 30, 60 s), then OK again. A "Grab in
  N s" banner shows for delays of 5 s or more (it lasts about 3 s, so it is
  gone before the grab); shorter delays are silent.
- **Interval** is the time between time-lapse shots (1 s up to 1 h), set the
  same way.
- **Round mask** (off by default) blacks out the corners outside the round
  display when a shot is saved, so it is a circle on black rather than a
  square. It only affects shots saved afterwards. It's exact (a pixel is kept
  if its centre is inside the 240 px circle) and adds about 1 KB to a file.
- **Start timelapse** / **Stop timelapse** takes a shot every Interval, first
  one a full interval after you start so you can switch away. It runs in the
  background, with no "Saved" banners (so a banner never lands in a frame), and
  stops when you tell it to, if the card goes away, or if a save fails, with a
  "Timelapse stopped: N shots" banner. It refuses to start when new shots go to
  flash, since flash only keeps the newest 6. It is never started at boot. If
  F is set to quit the app, the time-lapse stops when you quit.

**Web tile** (UP/DOWN to choose, CONFIRM to change): see [Web server](#web-server).

**Settings tile:**

- **Start at boot** switches the `/boot.py` autostart on or off (off by
  default, takes effect next boot).
- **F key** chooses what F (CANCEL) does: keep Screengrab running in the
  background (default), or quit it, which stops the LEFT+RIGHT listener until
  you launch it again.
- **Save to** picks where new shots go: `auto` (the first card by name, else
  flash), `flash`, or a specific card. If the pinned card isn't there, it falls
  back to the first one that is.
- **Auto move** (off by default) moves flash shots onto a card, oldest first,
  as soon as one is mounted, even while the app is in the background, and
  notifies "Moved N to SD" when done. It does nothing while you're in the
  Storage tile or if **Save to** is `flash`, and backs off for a minute if the
  card is full.

**Storage tile:**

- The list shows flash and every card with its shot count and free space. The
  dot marks where new shots go. OK makes the highlighted storage the target,
  and RIGHT opens its shots.
- In the shot list (newest first), OK **views** the highlighted shot full
  screen and RIGHT moves it to another storage (the save target if that's a
  different one, otherwise the first other). Moved shots are renumbered into
  the destination's sequence.
- In the viewer, UP/DOWN flip to the previous or next shot, OK deletes the
  shot after a second OK, RIGHT moves it, and LEFT or F goes back. The name,
  position and key hints show for a few seconds after each key press.

Shots are drawn straight from their PNG with `ctx.image()`, so there is no
conversion step. Decoding needs roughly 500 KB of free heap; if the screen
comes out blank the viewer says "Blank or low memory".

## Web server

Switch it on from the **Web tile** to use the badge from a browser: browse,
download, move and delete shots, mirror the screen live, and start captures
(with the delay, or a time-lapse) remotely. It is off by default, never starts
at boot, and costs no memory while off.

Switching the server on shows a big **connection card**: what to join, the
password, the address and the PIN, in large type so it can be read across a
table. OK steps through **QR codes** so a phone can do the typing:

1. **Join the Wi-Fi** (hotspot mode only): a `WIFI:` code that the phone's
   camera offers to join, with no password to type.
2. **Sign in**: a link to `/q?t=CODE` that opens the page already signed in,
   with no PIN to type. The code works **once** and lapses after 10 minutes, so
   the PIN never lands in the phone's history and a copy of the link is no use;
   the screen changes to "Signed in" when a phone uses it, and OK makes another.
   With the PIN off it is just the address. In Wi-Fi mode the phone has to be on
   the same network already, so there is only this one.

Then DOWN opens the options, UP steps back a page, and UP from the top option
comes back to the card.

The options are:

- **Server: ON/OFF** starts or stops it. While it runs, the title on the main
  screen turns cyan.
- **PIN: ON/OFF** (on by default) asks for a six-digit PIN, shown only on the
  badge and new each time the server starts. With it off, anyone on the network
  can see your screen and the card warns "No PIN - open!".
- **Network** cycles three ways. *Wi-Fi* joins whatever network the badge is
  already on and answers only on that address. *Hotspot* makes the badge its own
  network (`Tildagon-xxxx`, WPA2) at `http://192.168.4.1` for phones at events
  with no shared Wi-Fi, and answers only there; the phone is shown a sign-in
  page (see **Hotspot and phones**). *Hotspot (quiet)* is the same network but
  tells phones it is online instead.
- **AP pass: 8 chars/4 words** chooses the hotspot password: eight random
  letters and digits with the look-alikes (0 O 1 I L) left out, or four random
  short words such as `acid-acre-army-bath`, which are easier to read out and
  type. Changing it makes a new password, which is kept so a phone can remember
  the network. Four words are about 41 bits and eight characters about 40, so
  either is fine for a hotspot that is on for a few minutes, but neither is a
  substitute for the PIN.
- **Idle off** switches the server off after this long with no requests (never,
  1, 5, 10, 30 or 60 min; 10 by default).
- **Name** is the badge's network name (`tildagon` by default), so on your Wi-Fi
  it answers at `http://tildagon.local` as well as by IP. OK opens the firmware's
  own text entry: the badge's six buttons pick letters (a group, then the
  letter), and a **USB keyboard** types straight into it (Backspace, Space, Shift;
  Enter saves, Escape cancels). Names are 1 to 24 lowercase letters, digits and
  hyphens, not starting or ending with a hyphen; capitals are lowercased. The
  page's Control tab can change it too. The firmware's mDNS responder takes the
  name once, when Wi-Fi first connects, so a new name works as `<name>.local`
  after the next restart (the card says "after restart" until then); the
  boot snippet below applies the saved name before that happens.

The page has three tabs: **Gallery** (thumbnails of the shots on every storage,
with open, download, move and delete), **Live** (the badge's screen as PNG
frames, optionally round-masked, with "Save this screen") and **Control**
(capture now or after a delay, start and stop the time-lapse, pick where shots
go, round mask). `/#live` and `/#control` open those tabs directly.

### Hotspot and phones

A phone joined to the badge's hotspot finds it has no internet, and Android in
particular then sends the browser's traffic over mobile data, which can't reach
`192.168.4.1`: the page times out, or Chrome says `ERR_EMPTY_RESPONSE`. Logging
a real Android phone's traffic showed why the obvious fix doesn't work. The
phone does use the hotspot's DNS and probes `connectivitycheck.gstatic.com`,
`www.google.com/gen_204` and `play.googleapis.com/generate_204` again and again,
and answering "204, online" is never enough, because Android also probes over
HTTPS, which the badge can't answer without a real certificate. So it never
accepts the network as having internet, and its browser stays on mobile data.

So the hotspot behaves like a hotel Wi-Fi (a captive portal):

- its DHCP hands out the badge as the DNS server, and a tiny DNS responder
  (in its own thread, so a busy screen can't make it drop queries) answers
  every name with the badge's address;
- the connectivity checks phones and laptops make on joining (Android's
  `/generate_204`, Apple's "Success" page, Windows, Firefox and Linux ones) are
  answered with a **redirect to the page**, so the phone shows its own "Sign in
  to Wi-Fi network" screen. That screen is bound to the hotspot, so it works
  with mobile data on. iPhones and laptops open it by themselves; on Android
  tap the notification. Enter the PIN from the badge there;
- these checks don't count as use, so they can't keep the server awake;
- any other name that arrives through that DNS is redirected to the badge's
  page (and non-GET requests to a wrong name are refused, which also blocks DNS
  rebinding).

What does not change: a link opened in the phone's ordinary browser (including
the **sign-in QR code**, which opens in Chrome) still uses mobile data on
Android, so for that, or for the sign-in screen's limits, turn mobile data off.
*Hotspot (quiet)* answers the checks "online" instead, which satisfies Apple and
laptops but not Android. `tests/captive_test.py` checks the responder, the
redirects and the quiet answers in both modes against a server in the same mode.

### How it behaves

- **Speed.** It is a small server on non-blocking sockets, because MicroPython's
  stream server answered every request in 0.6 to 1 s here (a raw socket takes
  40 to 170 ms). It shares the badge's event loop, which turns once per redraw
  of whatever app is in front, and every wait costs one turn. Measured:

  | | Screengrab in front, idle | a busy app redrawing every frame in front |
  |---|---|---|
  | status / shot list | 0.13 s | 0.45 s |
  | a shot (10-15 KB) | 0.3-0.45 s | 1.6 s |
  | live frame | 0.46 s (about 2 fps) | 2.5 s (0.4 fps) |

  The server yields rather than blocking, so the badge's own screen and buttons
  stay responsive either way. Screengrab's own screen skips redrawing when
  nothing has changed, which is what keeps the loop fast while you sit on the
  Web tile. Opening the Storage tile still pauses the screen for ~0.7 s while it
  reads the flash's free space (the badge's flash is slow at that).
- **Security.** Every request needs the PIN (a cookie after logging in, or an
  `X-PIN` header), five wrong PINs lock it for a minute, and a page you visit
  can't drive it: the session cookie is `SameSite=Lax` and anything that changes
  state is a `POST` with an
  `X-Requested-With: screengrab` header, which a web page on another site can't
  send. Shots can only be named as `shot_NNNN_….png` on a known storage, so
  `../` tricks go nowhere. It is plain HTTP, so on a shared network a PIN can
  be sniffed: use **Hotspot** at events, or leave PIN on and the idle timeout
  short. At most three clients are served at once, the rest wait their turn.
- **API** (JSON unless noted): `GET /api/status`, `GET /api/shots?storage=&offset=&limit=`,
  `GET /shot/<storage>/<name>[?dl=1]` (PNG), `GET /api/frame[?mask=1]` (PNG of the
  screen now), `POST /api/capture {delay}`, `/api/delete {storage,name}`,
  `/api/move {storage,name,to}`, `/api/timelapse {on,interval}` and
  `/api/options {delay,interval,mask,save_to}`.
- **Why `Lax`, not `Strict`.** The sign-in QR opens its link from a scanner or
  another app, which a browser treats as a cross-site navigation. With
  `SameSite=Strict` the browser dropped the new cookie on the redirect straight
  after the link and showed the PIN page; `Lax` is sent on a link you open but
  still not on cross-site POSTs or sub-requests. `tests/web_signin_xsite.py`
  guards against this by following the link from a page on another site in
  headless Chromium.

### Tests

`tests/qr_test.py` checks the QR encoder (`qr.py`, written for the badge since
the firmware has none) against the `qrcode` library module for module, over 300
payloads at both error correction levels, and decodes renderings with
`zxing-cpp`. On the badge the codes come out byte-identical to the Pi's and take
0.4 to 0.75 s each. The codes drawn on the badge's real screen were also
grabbed and decoded with `zxing-cpp`, but not yet scanned by a phone camera.

`tests/captive_test.py` (32 checks, in portal and in quiet mode: the hotspot's DNS
answers, connectivity checks and redirects, run against a server started in the
same mode),
`tests/web_test.py` (about 90 checks: auth, lockout, the one-time sign-in code, path
traversal, malformed requests, every action, live frames, concurrency, a silent
client, leaks) and
`tests/web_soak.py` (a few minutes of live frames, polling and downloads while
counting errors) run from another computer against a badge with the server on:

    python3 tests/web_test.py http://192.168.68.92 <PIN> [<one-time code>]
    python3 tests/web_soak.py http://192.168.68.92 <PIN> 120

`web_test.py` saves, moves and deletes shots, so don't point it at a badge
holding shots you care about.

The frame comes from `display.get_framebuffer()` and is written by `pngenc.py`
as a compressed PNG. The firmware has no deflate compressor, so `pngenc.py`
carries a small one in `@viper`: a single fixed-Huffman block that matches the
pixel to the left and the pixel above. A typical UI screen is 2 to 30 KB and
takes about 0.3 s to save; a photo-like screen can reach ~100 KB and 2 s. It
is lossless (checked pixel for pixel).

 Button events normally only
reach the focused app, so the app registers its handlers on an always-focused
stand-in object. Change the combo with `COMBO` in `app.py`.

Pulling files over USB is slow (about 28 s per shot with `mpremote fs cp`);
an SD card avoids that.

License: MIT

### Known differences

- `qr.py` picks the mask with the penalty rules of the standard as the widely
  used Nayuki implementation reads them. Two other libraries score the
  finder-like-pattern rule differently at the symbol's edge and so sometimes
  choose another mask. Every mask gives a valid code (the codes match the
  `qrcode` library exactly when the same mask is forced, and decode), so this
  only changes how the code looks, not whether it scans.

## `/boot.py`

Two optional blocks for `/boot.py` on the badge. The `display_manager` app
rewrites `/boot.py` when it changes the mirror setting, which removes them.

**Network name.** Gives the badge its name (`tildagon.local`, or whatever you set on
the Web tile) before Wi-Fi connects, which is the one moment the firmware's mDNS
responder reads it. Without this block the name still works as the DHCP
hostname, but `<name>.local` only follows the firmware's default (`mpy-esp32s3`).

```python
try:
    import network
    from settings import get as _get

    network.hostname(_get("screengrab_web_name", "tildagon"))
except Exception as e:
    print("[BOOT] hostname skipped:", e)
```

**Start at boot.** Starts Screengrab in the background on every boot (no need
to launch it from the menu). The "Start at boot" item in Settings turns this on
and off (the `screengrab_autostart` setting, off by default, so launch the app
once and switch it on):

```python
try:
    from settings import get

    if get("screengrab_autostart", default=False):
        from system.scheduler import scheduler  # import first: avoids a circular import
        from system.eventbus import eventbus
        from system.scheduler.events import RequestStartAppEvent
        from apps.tildagon_screengrab.app import ScreenGrab

        eventbus.emit(RequestStartAppEvent(ScreenGrab(), foreground=False))
except Exception as e:
    print("[BOOT] Screengrab autostart skipped:", e)
```

Only one instance listens for the combo, so launching the app from the menu
as well won't double up the screenshots. The SD card still has to be mounted
from the SDHexp app after each boot (a Dual uSD hexpansion mounts itself).

## Credits

The four-word hotspot passwords use the EFF's [Short Wordlist #1](https://www.eff.org/dice)
(`words.txt`: 1295 words of 3 to 5 letters, with "yo-yo" left out because of
its hyphen), by the Electronic Frontier Foundation, licensed
[CC BY 3.0 US](https://creativecommons.org/licenses/by/3.0/us/).
