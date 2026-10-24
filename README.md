# Screengrab

Tildagon badge app that saves a screenshot when you hold **LEFT + RIGHT** in
any app.

1. Launch **Screengrab** from the Apps menu, then press CANCEL to send it to
   the background.
2. In any app, hold LEFT and RIGHT together. A "Saved shot_000N.bmp"
   notification confirms it.

Screenshots are 240x240 16-bit BMPs. They go to `/sd` or `/sdcard` if one is
mounted (no limit), otherwise to `/screenshots` in badge flash, where the
newest 6 are kept.

The frame comes from `display.get_framebuffer()`. Button events normally only
reach the focused app, so the app registers its handlers on an always-focused
stand-in object. Change the combo with `COMBO` in `app.py`.

Pulling files over USB is slow (about 28 s per shot with `mpremote fs cp`);
an SD card avoids that.

License: MIT
