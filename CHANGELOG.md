# Changelog

## 1.8.0 – 2026-10-02

Downloads that need no Python, a lot more data on the map, and tools for people who track storms.

* **Packaged downloads**: a Windows installer / zip and a Linux AppImage, built by GitHub when a version is
  tagged. `radarforge --self-test` checks that every library and data file is present.
* **Export**: loop to **GIF or MP4**, **PNG with a title bar** and colour bar, and a clean **Briefing view** (F10).
* **Satellite**: GOES-East / West infrared, water vapour, shortwave IR and visible, with opacity and automatic
  satellite choice; **GLM lightning** flashes for the last 5–30 minutes. Both work in live and archive modes.
* **MRMS**: rotation tracks, hail swaths and rainfall totals (30 minutes to 3 days).
* **Surface observations**: METAR station plots with wind barbs, temperature, dew point and pressure.
* **SPC**: outlook days 1, 2 and 3, and the full text of a warning or watch.
* **Sounding tool**: a HRRR model sounding at any point, or the latest balloon launch, as a skew-T and hodograph
  with CAPE, CIN, LCL / LFC, shear, helicity, STP and SCP. One click sets the Bunkers right mover for SRV.
* **Saved locations**: any number of places with alert rules (warning types, distance, lead time), a countdown to
  the nearest warning, a sound and a desktop notification. Your own location stays the first one.
* **Bookmarks, shareable views and workspaces**: save a view, send it as a `.rfview` file or a line of text, and
  switch between layouts (Briefing, Tornado hunt, Hail, Flood, Satellite and storms, Everything).
* **Command palette** (Ctrl+K): every command, radar, product, city, bookmark, workspace and saved location.
* **Signature flags**: strong rotation, possible debris and possible ZDR columns are found automatically.
* **Follow a storm**: keeps a cell centred and hands over to the nearest radar as it moves in live mode.
* **Storm trends**: hail probability and size, rotation rank, TVS and speed of one cell across the loop.
* **Hover guide** (Settings → General): says in words what the value under the cursor means.
* **Learn mode** (Help): nine historic storms from the archive, stepped through with a short explanation each.
* **Tidier code and menus**: map overlays now live in `overlays/`, the logic behind the features in `services/`
  (no Qt), data access in `data/`; the main window is split into one file per area. Layers, Locations and
  Tools have their own menus, and the README project tree matches.
* The download cache is capped (6 GB by default, `download_cache_gb`): the least recently used files go first.
* New dependencies (installed automatically): h5py, pillow, imageio-ffmpeg, matplotlib, pyproj.

## 1.7.0 – 2026-10-01

* **Storm track tool** (**T**, or **Track** on the toolbar):
  * Click a storm, then drag the yellow arrowhead to where it's going. Tick marks show the clock
    times along the way.
  * The status bar lists the towns it reaches and when, and your arrival time if you've set your
    location.
  * Right-click for **Use for SRV storm motion**, a 30–120 minute track, reset or clear.
* **Measure** lines now stay on the map until the next measurement or **Esc**.
* **Storm chasers** (Map → Storm chasers): live Spotter Network positions, updated every minute.
  * Arrows show which way each chaser is driving; the colour shows how fresh the position is.
  * Show everyone or only active reporters, with or without names. Hover for details.
* **SPC day 1 outlook and mesoscale discussions** (Map menu): risk areas with labels.
  * Hover inside an outlook area for its category and the tornado, wind and hail chances there.
  * Right-click a discussion to read its full text.
* **Storm reports**:
  * Choose 1–24 hours, and which types to show (tornado, hail, wind, flood, other), from the Map
    menu or the Reports tab.
  * Spotter Network reports are added to the NWS ones.
  * Live reports fade with age. New letters: T, FC (funnel), WC (wall cloud), H, W, G, F.
* **My location**: right-click the map → **Set my location here**, and **Ctrl+L** goes back to it.
  When a new tornado, severe thunderstorm or flash flood warning covers it, RadarForge pops it up
  and flashes the taskbar. You're alerted once per warning, and again if it's upgraded.
* **Favourite radars**: **Ctrl+D**, **Radar → Favourite radars**, and ☆ in the radar list.
* **Copy image** (**Ctrl+Shift+C**) puts the map on the clipboard.
* Hovering a warning or watch shows its text only on its **outline**, not anywhere inside it, so it
  no longer covers the storm you're looking at. SPC outlines work the same way.

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
