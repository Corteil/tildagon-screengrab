# Screengrab

Tildagon badge app that saves a screenshot when you hold **LEFT + RIGHT** in
any app.

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

    Settings  <-  Capture  <-  Main  ->  Storage

- **LEFT** moves one tile left (main -> Capture -> Settings), and **RIGHT**
  moves back. **RIGHT** on the main screen opens the Storage tile, where LEFT
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

## Start at boot

Add this to `/boot.py` on the badge so Screengrab starts in the background
on every boot (no need to launch it from the menu). The "Start at boot" item
in the app's menu turns this on and off (the `screengrab_autostart` setting,
off by default, so launch the app once and switch it on):

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
as well won't double up the screenshots. The `display_manager` app rewrites
`/boot.py` when it changes the mirror setting, which removes this block.
The SD card still has to be mounted from the SDHexp app after each boot.
