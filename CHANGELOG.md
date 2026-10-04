# Changelog

## 1.9.3 – 2026-10-04

* Removed the automatic Level III **mesocyclone circles** (NMD, "strength rank") and **TVS triangles** (NTV)
  from the map, with their switches in Layers → Level III overlays and the Layers panel. They're no longer
  downloaded for the map. The Storm cells table still lists mesocyclone rank and TVS while it's open.

## 1.9.2 – 2026-10-04

* **Street cameras** (Layers → Street cameras): traffic camera icons on the map. Click one to see its
  picture, which refreshes every minute; if several cameras share a spot you can pick between them.
  * To keep the radar readable, cameras only show when you're zoomed in (about 250 miles across or
    less), as small icons, one per patch of screen.
  * California's cameras (Caltrans) work out of the box.
  * New York, Georgia, Idaho, Alaska, Louisiana, Utah, Wisconsin, Arizona, Nevada, Connecticut and
    Florida (their 511 systems), plus Windy Webcams worldwide, need a free developer key. Add keys in
    **Layers → Street cameras → Camera sources & keys**.
* **Map style** (Settings → Map style): colours for interstates, highways, state, county and country
  lines, radar site markers and labels, and city labels and dots, plus line widths. Fonts (family,
  size, bold) for city labels, radar site labels and panel titles. Changes preview live. Editing a
  built-in theme saves your version as "<theme> – my map". The theme editor has the fonts too.
* **Archive Level III fixed**: cases from before the super-res Level III products (N0B / N0G) existed
  now fall back to N0Q / N0U (and N0R / N0V), so L3 reflectivity and velocity load for older dates.
* **Archive frames fixed**: Level III products that finished downloading before the Level II volumes no
  longer turn into extra frames without radar data. They now wait and attach to the volume they
  belong to, so the loop has one frame per volume with base reflectivity and velocity in each.
* **Dealias velocity** now also works on Level III velocity (L3 super-res velocity and L3 SRV). The panel
  title says "dealiased" when it's on. On most scans only gates that were actually aliased change, so
  the difference shows in strong-wind areas such as hurricanes, intense couplets and strong jets.

## 1.9.1 – 2026-10-04

* **Steadier, quicker live data**:
  * The newest volume now appears first, within a second or two, and the rest of the loop fills in
    behind it newest first. Before, every earlier volume had to download before live data showed.
  * A chunk that never arrives no longer stalls the live feed: after 30 seconds it's skipped and
    the rest of the volume keeps coming. When the complete archive file is published a few minutes
    later, it replaces the gappy volume automatically (no more switching radars to fix it).
  * If a volume never finishes, or the radar restarts its numbering, RadarForge notices within about
    90 seconds and moves to the newest volume (it used to wait 15 minutes).
  * Holes in the loop are filled from the archive every 90 seconds, including the volume the archive
    is still behind on (loaded from its live chunks).
  * Chunks download in parallel and on their own thread, so loading the loop or Level III can't hold
    up live updates. While a volume is scanning RadarForge checks again every few seconds.
  * Failed downloads and S3 "slow down" replies are retried automatically, and switching radars stops
    the old radar's downloads straight away.
  * **Radar → Reload live data (F5)** restarts the feed for the current radar if anything looks wrong.
  * The download cache is kept under 3 GB (files older than 10 days are removed).
* **Toolbar switches** (also in the View menu):
  * **Smoothing (S).**
  * **Dealias velocity (D)**: base velocity and storm-relative velocity panels show unfolded
    velocities. The panel title says "dealiased".
  * **Σ Max value trail (Ctrl+T)**: every panel shows the most extreme value at each spot over the
    loop up to the frame shown. Reflectivity or MESH gives hail swaths, azimuthal shear gives rotation
    tracks, and velocity gives the strongest winds. CC shows its lowest value, which traces debris.
    Step back through the frames to shorten the trail.
* The toolbar fits narrow windows: the words beside the icons are dropped first, so every button stays visible.
* Warnings panel: **Only in view** is now off by default, and RadarForge remembers your choice.

## 1.9.0 – 2026-10-03

* **Satellite** (Layers → Satellite): GOES-East / West infrared, visible or water vapour under the radar,
  reprojected onto the map, with colour-enhanced IR and an opacity choice. Live data (the Iowa
  Environmental Mesonet's latest CONUS images).
* **Lightning** (Layers → Lightning):
  * GOES lightning mapper (GLM) flashes from NOAA's AWS buckets, coloured by age, for the last 5–30
    minutes. Works with archive cases too. Hover for counts.
  * An NLDN cloud-to-ground lightning-density map (via MRMS).
* **MRMS swaths** (Layers → MRMS swaths): rotation tracks (30 min – 24 h), hail size (MESH, 1–24 h)
  and rainfall (1–24 h) from NOAA's Multi-Radar Multi-Sensor system. Shown for the time on screen, so
  archive cases get them too. Hover for the value. (RadarForge reads the GRIB2 files itself.)
* **Surface observations** (Layers → Surface observations): ASOS station plots – temperature, dew
  point, sky cover and wind barb – decluttered by zoom. Hover for the full report and METAR.
* **SPC day 2 and day 3 outlooks** (Layers → Storm Prediction Center).
* **Model soundings**: right-click the map → **Model sounding here…** for a Skew-T, hodograph and the
  usual numbers (CAPE/CIN, LCL, freezing level, lapse rate, PWAT, 0–1/3/6 km shear, 0–1/3 km helicity,
  Bunkers storm motion, STP) from the RAP, HRRR, NAM or GFS, any forecast hour.
  **Use right-mover as storm motion** sets SRV and the track tool.
* **Saved locations with alert rules** (Location → Saved locations & alerts, Ctrl+Shift+L):
  * Add places by name, coordinates, the map centre or right-click → **Save this location…**.
    "My location" is the first entry (your 1.7 alert setting carries over).
  * Per place: tornado / severe / flash flood / other warnings, watches, storm reports within N miles,
    lightning within N miles.
  * Per place: an alert **sound** (chime, siren or beeps, with volume), a **desktop notification**
    and/or a pop-up. Each warning alerts once, and again when it's upgraded; lightning at most every
    30 minutes.
* **Storm tools**:
  * **Automatic storm flags** (Tools): ROT for rotation, TDS? for a low-CC debris signature beside
    strong rotation, HAIL for hail cores – each explains why when you hover it.
  * **Follow a storm**: right-click → **Follow this storm** keeps it centred as the loop plays (it
    tracks the strongest echo and learns the storm's motion).
  * **Rotation history**: azimuthal shear and rotational velocity of one storm through the loaded
    frames, as a chart and a table you can copy as CSV.
  * **Radar & dual-pol guide** and **Learn mode** (Help): the Inspector explains in plain words what
    the values under the mouse mean.
* Small captions in each panel's corner say which data layers are on and how old they are.
* New dependency: `h5py` (reads the GLM files). The installers include it. `radarforge --check` tests the
  optional parts (lightning files, MRMS decoding, alert sounds, MP4 export, soundings).

## 1.8.0 – 2026-10-02

* **Installers**: a Windows installer (`RadarForge-Setup-1.8.0.exe` – no Python, no admin rights) and a
  Linux **AppImage**, built automatically for every release. The source zip and its install scripts still work.
* **Export** (File → Export):
  * **Loop as GIF or MP4** (Ctrl+E): pick the frames, speed, pause on the last frame and size; every
    panel and overlay is recorded as shown.
  * **Save image with legend and details** (Ctrl+Shift+S): colour bars plus a title bar (radar, UTC and
    local time) and a details bar (products, storm motion, warnings).
  * **Briefing view** (Ctrl+B): the map with warnings in view, storm reports, the SPC outlook, storm
    motion and storm-track arrivals beside it. It follows the frame shown; save or copy it.
* **Menus reorganised**: File (data in, pictures out), View, Radar (with favourites), **Layers**
  (everything drawn on the map, grouped: warnings & reports, chasers, SPC, Level III, map), Tools,
  **Location**, Panels, Help.
* The main window's code is split into topic files (menus, layers, storm tools, location, export).
* Fixed: a saved picture could show the map from just before the last change.

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
