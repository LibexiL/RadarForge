# Changelog

## 1.6.0 – 2026-10-01

* **Warning lines** (Settings → Warnings): every warning type and threat level has its own line –
  colour, width and style (solid, black centre line or double). Codes: TOR, TORR (reported),
  TORP (PDS), TORE (emergency); SVR, SVRC (considerable), SVRD (destructive); FFW, FFWC, FFWE;
  SMW, SQW, EWW, DSW, SPS and the watches (TOA, SVA). The level comes from each warning's NWS
  impact tags (live and archive).
* Defaults use the NWS colours with the threat levels told apart by line style; the **Classic
  colours** preset gives green flash flood, yellow severe and magenta reported / PDS / emergency
  tornado.
* Double-clicking a warning in the Warnings panel switches to the radar nearest it before zooming
  in (can be turned off in Settings → Warnings).
* The Warnings list and hover text show the threat level and its code.

## 1.5.0 – 2026-10-01

* Warning outlines use the **National Weather Service hazard colours** by default (flash flood
  warnings are now dark red, special marine orange, extreme wind dark orange).
* **Settings → Warnings**: choose your own colour for each warning and watch type, or reset to the
  NWS colours.
* Hovering the map only shows warnings and watches that are switched on. Watches no longer pop up
  their text when they're hidden.
* The Warnings panel's buttons (Tornado, Severe, Flood, Other, Watches) now control the map as
  well as the list, and are remembered. Watches is the same switch as Map → Watches.

## 1.4.0 – 2026-09-30

* **Windows 10 / 11 support**: `install.bat`, `run.bat` and `uninstall.bat`, Start Menu and desktop
  shortcuts, and settings / downloads in the usual Windows folders (`%APPDATA%`, `%LOCALAPPDATA%`).
* The download has a **`Windows`** and a **`Linux`** folder with that system's install, run and
  uninstall scripts and a short `HOW TO INSTALL.txt`. Starting a script from inside the ZIP (without
  extracting it) now explains what to do instead of failing.
* Text on the map uses the system font on every platform.
* Repository tidied for GitHub: clear install instructions, changelog, licence, and automatic tests on
  Windows and Linux.

## 1.3.3

* The radar map and the 3-D view now draw in their own OpenGL windows instead of through Qt's widget
  compositor. Fixes a black map, a frozen window, or a mirrored copy of the window after resizing
  with some NVIDIA drivers.
* The map redraws correctly when the window gets smaller; Esc cancels a panel drag.

## 1.3.2

* Removed a Qt message hook that could freeze the window with some drivers.
* Everything the program prints goes to a log file; if the window stops responding for 10 s the log
  also records what every thread was doing.
* `--safe-graphics` option and a *Reuse the drawn map* setting for driver trouble.
* The 3-D view's OpenGL context is only created when the 3-D panel is first opened.

## 1.3.1

* OpenGL is set up before the application starts, which NVIDIA's X11 driver needs (fixed a black map).
* If the driver reports that it can't show the picture, RadarForge restarts itself with the next
  OpenGL setup (platform × context type) and remembers the one that works.

## 1.3.0

* **Panels snap into place**: drag a tab and big drop zones show where it will go (split a panel, add
  as a tab, dock beside the map or along a window edge, or float it). Layout is saved.
* **Themes**: six built-in themes, theme files (`.rftheme`) to import / export / drag onto the
  window, and an editor for your own.
* **Tidier interface**: grouped toolbar with drawn icons, a timeline bar for frames, reorganised menus,
  side-panel sections, and a Settings window with categories.
* **Faster**: moving the mouse no longer redraws the whole map; map labels and overlays are drawn once
  per layout instead of once per panel; Level III decoding moved to background processes.

## 1.2.0

* **Side panel** (F9): products and tilts, warning manager, storm-cell table, cursor inspector,
  placefile manager and map layers.
* Panels can be moved, stacked, resized and floated.
* Automatic OpenGL detection (no more `--x11` needed on Wayland + NVIDIA).

## 1.1.0

* Much smoother while downloading (radar files are decoded in separate processes).
* Clean, readable velocity: a noise filter and fold-aware smoothing.
* Cross-section line disappears when the cross section is closed; range rings clarified.
* Placefiles can be drawn above or below the radar data.
* 3-D view of a box you drag around a storm; drag-and-drop colour tables; radar sites on the map.

## 1.0.0

* First release: live and archive NEXRAD Level II / III, 1–6 linked panels, derived products,
  GRLevelX placefiles and colour tables, NWS warnings, cross sections and a 3-D view.
