# Changelog

## 1.0.1

- Check for `display.get_framebuffer()` at start-up. On firmware without it
  the app shows a warning on the display and as a notification, and stays
  inactive instead of failing.

## 1.0.0

- Hold LEFT+RIGHT in any app to save the screen as a compressed PNG (own
  on-badge encoder), to an SD card if present, otherwise badge flash.
- Timestamped names (UUID until the clock is set), optional round mask.
- Tiles: Main, Capture (self-timer, time-lapse), Settings, Storage (move,
  delete, auto-move to SD), Web.
- On-badge viewer for saved shots.
- Web server: gallery, live mirror, remote control, optional PIN, one-time QR
  sign-in, Wi-Fi or own hotspot (8-char or four-word password), captive-portal
  sign-in for phones, changeable `tildagon.local` name.
- Optional start at boot (off by default).
