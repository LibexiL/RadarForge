# Changelog

## 1.14.0 – 2026-10-09

Radars load faster and more reliably, and a loop is now 10 frames unless you choose more.

### 10 frames by default
- **New default:** choosing a radar loads a 10-frame loop.
  - If your settings still have the old default of 12, they change to 10.
  - A length you picked yourself is kept.
- **Choosing more:**
  - The **frames** button beside the timeline offers 6 to 60 frames, plus **Other number…**.
  - **Settings → Loop & live** takes any number from 1 to 60.
- **Longer loops fill in straight away.** Before, the extra frames waited for the next archive check, up to
  90 seconds later.

### Switching radars
- **The radar you left stops loading.** Its downloads, decoding and Level III stop as soon as you pick
  another radar. Before, they carried on and the new radar's files waited behind them, so clicking
  through a few radars could double the wait.
- **Level III comes in at once.** Its first check used to be skipped while the previous radar's check was
  still running, which left the panel empty for up to 30 seconds. Rarely, the new radar's Level III then
  stopped updating altogether.
- **No more empty panels while the current scan loads.** Level II panels show the newest volume already
  loaded, marked "(prev vol)". Before, they could show "No Level II volume for this frame" for several
  seconds.

### Loading the loop
- **Each volume is decoded as soon as it downloads**, newest first. Before, nothing was decoded until every
  file had arrived, and then one at a time.
- **The newest file downloads first, on its own**, so the first picture comes sooner. The rest then download
  several at a time.
- **Files stream straight to disk** instead of being held in memory as they download.
- **Level III fills the whole loop.** Its files download several at a time, and enough are fetched to reach
  the oldest frame. With SAILS, each volume can have up to four, so the old count only covered the last few
  volumes.
- **In tests** with a simulated 100 Mbit/s connection:
  - A 10-frame loop was ready to play about a quarter sooner after choosing a radar.
  - After clicking through several radars, it was ready in less than half the time.
  - A 30-frame loop was ready about 40% sooner, using a little less memory.

### Steadier
- **The map stays smooth while a loop decodes.** The decoder processes run at a lower priority than the
  app, and one is always kept free for what's on screen.
- **Live catch-up runs in a decoder process.** When a live volume has many new chunks, they are now decoded
  there instead of in the app itself.
- **A stuck or crashed decoder process is restarted.** Before, one slow decode could switch RadarForge to
  slower in-app decoding until it was restarted.
- **Level III lands on the right frame.** While the loop downloads, a product whose own volume hasn't arrived
  yet waits on a frame of its own. Before, it could land on an older frame and stay there.
- **Each volume is decoded once.** A second request for a volume that took over two minutes used to start a
  second decode of the same file.
- **Long loops use less memory.** A long loop kept up to 40 decoded volumes in memory, whatever the volume
  cache was set to. Loops of more than 40 frames also decoded volumes again while playing.
- **The loop no longer pauses on frames with a missing product.** A frame without one of the panels'
  products (often Level III) made the loop wait on it every time round.
- **Fixed rare glitches while frames arrive:** a brief "no data", or a jump in the timeline, when the frame
  list was read while a frame was being added.

## 1.13.0 – 2026-10-07

### Live data from polling servers
- **Radar → Live data source** picks where live Level II comes from:
  - NOAA on AWS, as before and still the default.
  - Iowa State's free Level II polling server.
  - Any GR2Analyst-style polling server you add (the address GR2Analyst calls the polling URL).
- **Settings → Loop & live** lists the servers, with **Add**, **Edit**, **Remove** and **Test**. **Test**
  lists the current radar's volumes on that server.
  - A subscription server takes your user name and password in the address
    (`https://user:password@server/path/`). Lists and messages show it as `***`.
- **How it fetches:** RadarForge reads the server's `dir.list`, loads the loop from it, and then downloads
  only the new part of the volume being scanned (a byte-range request, as the servers ask).
- **Where it says so:** the live badge names the server.
- **If the server fails:** when it can't be reached or has nothing for the radar, live data comes from AWS
  instead.
- Archive data and Level III still come from AWS.

## 1.12.0 – 2026-10-07

A new 3-D storm view, and Level III velocity (N0G) that shows up in live mode.

### 3-D view, rebuilt
- **Drawn on the graphics card by ray marching**: every pixel walks through the storm front to back, so
  see-through layers always blend in the right order. The old view drew its see-through shells in the
  wrong order, which made the green mush.
- **Two styles:**
  - **Surfaces:** lit surfaces at the levels you choose. The innermost is solid and the outer ones are glassy.
  - **Volume:** a see-through cloud in the colour table's colours, denser where values stand out.
- **Products:** reflectivity, dealiased velocity, storm-relative velocity, CC, ZDR, KDP, spectrum width
  and azimuthal shear.
  - Dual-pol products are hidden where reflectivity is weak, because there they are only noise.
  - Other products get a faint 30 dBZ reflectivity outline, so you can see where in the storm they are:
    for example the debris column (low CC) inside the Moore storm.
- **A better grid underneath:**
  - Each tilt is sampled smoothly, and points between beams are interpolated in height, the way you'd
    read a stack of tilts.
  - Above the highest beam, the radar's cone of silence is left empty instead of being made up.
  - A 40 km box builds in a fraction of a second.
- **The scene around it:**
  - The lowest tilt and the map (states, counties, roads, towns) on the floor.
  - A height scale in kft or km.
  - A compass and a colour legend.
- **Cut plane** (north–south, east–west or at a height): the half facing you is cut away, and the cut face
  shows the data.
- **Follow loop**: the 3-D view shows the frame on the map and plays along with the loop. Frames are kept
  (up to 160 MB), so the second time round the 3-D loop is smooth.
- Vertical exaggeration, box height, grid detail, preset angles (from above, south, west ...), double-click
  to centre, **R** to reset, and **Save image…**.
- On a slow graphics card it draws at a lower resolution while you rotate it, and sharpens when you stop.
- **Panel layout:**
  - The 3-D panel opens beside the map at about half its width, instead of as a thin strip under it.
  - Its controls wrap onto more lines when the panel is narrow, so none are cut off.

### Level III
- **N0G (and other Level III) no longer goes missing in live mode.**
  - A new live volume has no N0G for its first minute or two, and RadarForge only checked Level III once a
    minute. So the newest frame, the one you're usually looking at, was empty for much of every volume.
  - Now a new frame keeps the previous volume's product until its own arrives. The panel says
    "(earlier volume)" while it does.
  - Level III is checked every 20 seconds, and each check lists only files newer than the last one it
    saw, so it's cheaper than before.
- A Level III download or decode that fails is tried again (up to 3 times) instead of being skipped for good.

## 1.11.1 – 2026-10-05

Much less memory. In live mode RadarForge used to keep growing: a 12-frame, 6-panel live loop passed
4 GB within minutes and kept climbing. It now stays at about 1–1.5 GB.

### Memory
- **Decoded radar volumes are kept compressed** in memory: 3.5–7× smaller (a volume takes about
  6–12 MB instead of about 40 MB). Only the sweeps being worked on are unpacked, which takes a few
  milliseconds.
- **Live mode no longer piles up old data.** Each live update made new panel images and a new tilt list,
  and the old ones stayed. Those kept up to 64 earlier volumes alive. Earlier revisions are now let go
  as soon as a newer one arrives, and so are frames that have left the loop.
- **The rendered image cache counts everything it holds.** It stored a second copy of each image for
  the graphics card without counting it, so its 600 MB limit really meant about 1 GB. That copy is now
  dropped once the image is on the graphics card. The default limit is 300 MB (an unchanged 600 MB
  setting moves to 300 MB), but never less than the loop on screen needs.
- **Decoder processes are much smaller:** about 70 MB each instead of about 230 MB. Only one of them
  loads MetPy, the library the Level III products need, and they give memory back after each volume.
  There are at most 3 of them for Level II.
- The live feed no longer keeps every radial of the volume being scanned once its sweeps are complete.
- Dealiased velocity is kept at half the size.
- On Linux, freed memory is handed back to the system after loading and every minute while idle. Fewer
  per-thread memory pools are used.
- **Settings → Performance** shows how much memory RadarForge and its decoder processes are using.

## 1.11.0 – 2026-10-05

RadarForge now keeps itself up to date, and the panel layouts are one key away.

> Updating to 1.11.0 itself is still done by hand (1.10.0 has no updater). From 1.11.0 on, new versions
> install from inside RadarForge.

### New
- **Automatic updates.** About once a day RadarForge asks GitHub whether a newer version is out. If
  there is one, an **Update to …** button appears at the bottom right. It opens the release notes of
  every version since yours and installs the update the way RadarForge was installed:
  - **Windows installer:** downloads the new setup. RadarForge closes, the update installs with only a
    progress window, and RadarForge starts again.
  - **AppImage:** downloads the new AppImage and puts it in place of the old file. The name and place
    stay the same, so shortcuts and menu entries keep working. Then **Restart now**.
  - **install.sh (Linux):** runs the new version's installer and shows its output. It finishes even if
    RadarForge is closed meanwhile. Then **Restart now**.
  - **install.bat (Windows):** RadarForge closes, a window shows the installer updating it, and
    RadarForge starts again.
  - **run.sh / run.bat:** saves the new ZIP to Downloads. A git checkout shows the notes only.
- **Help → Check for updates…** looks right away. **Skip this version** hides an update until a newer
  one comes out.
- **Settings → General → Updates** switches the daily check off.
- Every download is checked against the size and SHA-256 checksum GitHub lists for it. Only files
  from RadarForge's own GitHub releases are accepted, and only files carrying the new version number.
- Right after a release, GitHub needs a few minutes to build the Windows setup and the AppImage.
  RadarForge waits for them and checks again an hour later.
- **Number keys 1 … 6 set the number of panels**, on the number row or the number pad. Alt+1 … Alt+6
  still work. View → Panel layout shows each key. Typing numbers into a text box is unaffected.

### Installers
- The Windows setup removes the previous version's program libraries before copying the new ones, so
  nothing left over from an older version can clash.
- Started by the updater, the setup first waits for RadarForge to finish closing, closes anything
  still using its files, and starts RadarForge again when it's done.
- `install.bat` doesn't wait for a key press at the end when the updater runs it. It still waits when
  something went wrong.

## 1.10.0 – 2026-10-04

A tidy-up of the whole program: everything has one obvious home, the common switches are one click away,
and it loops, opens settings and closes faster.

> The first start after updating puts the side panels in the new default arrangement, once.
> **Panels → Reset panel layout** does the same at any time.

### New
- **Quick panel** (**F8**, or **Quick** at the top right): every on/off switch in one place as one-click
  buttons, in sections that fold away:
  - Display: smoothing, dealias, Σ trail, colour bars, linked cursor, pop-ups, velocity filter.
  - Warnings & outlooks: warnings, watches, reports (1–24 h), SPC outlook (day 1–3), SPC MDs, chasers.
  - Radar overlays: storm tracks, hail, melting layer, storm flags, range rings.
  - Satellite & lightning (channel, opacity slider, flash window), MRMS swaths (product, opacity).
  - Observations & cameras, map layers, storm tools, location alerts.
  - Each section title says how many of its switches are on. The buttons and the menus always agree.
  - It rearranges into fewer columns when the panel is narrow, so nothing is cut off.
- **Data age badge** at the bottom right: *LIVE scanning now*, or how many minutes old the newest live
  volume is (green, then amber, then red), *ARCHIVE* with the date, or *FILES*. The window title names
  the radar and the mode.
- **Loop buttons** beside the timeline: **fps** and **frames** set the loop speed and length in one click.
- **Layout button**: one toolbar button with all six panel layouts.
- **Tilt ▲ / ▼ buttons** either side of the tilt box.
- **Help → Check optional components…** shows whether lightning files, MRMS decoding, alert sounds,
  MP4 export and model soundings all work on this computer.
- **View → Reset view (Home)** and **Radar → Tilt → Lowest tilt**.
- Settings: new **Data layers** page (satellite channel and opacity, lightning window, MRMS product and
  opacity, camera keys) and **Alerts** page (sound, volume, my-location alerts, saved places).
  Dealiasing and the Σ trail are on the Display page. Every page has a short description.

### Reorganised
- **Menus**, one home per topic:
  - **File**: data in and pictures out.
  - **View**: how the radar is drawn.
  - **Radar**: which radar and when.
  - **Layers**: everything drawn on the map, in three captioned groups (warnings & outlooks, weather data,
    radar & map).
  - **Tools**: mouse tools and storm analysis.
  - **Location**, **Panels**, **Help**.
- Menus have icons, and on/off items always show their tick.
- **Right-click menu**: the panel's product and colour table first, then what you can do at that spot
  (sounding, rotation history, follow the storm, SPC discussion, nearest radar), then location.
- **Side panels**: Quick, Products and Placefiles on top; Warnings, Storm cells and Inspector below.
  The old Layers tab became the Quick panel.
- Resizing the window resizes the map; the side panels keep their width.
- The shortcuts list (**F1**) has Display and Window sections.

### Faster and steadier
- Settings opens about ten times faster.
- Lightning and street cameras draw much faster when there are thousands of them.
- Loops play without stutter: RadarForge keeps the whole loop decoded when the computer has the memory.
- Closing RadarForge is instant, even with downloads still running.
- Fixed live data sometimes switching to slow decoding for the rest of the session after the download
  cache was tidied mid-download. Cache writes now retry.
- Closing a sounding or rotation history window while it was still downloading could crash. Fixed.
- Cross sections and the 3-D view no longer pause the window while they work out beam heights.
- Network problems are explained in plain words (for example "no connection to api.weather.gov")
  instead of long error dumps.

## 1.9.3 – 2026-10-04

Includes everything from 1.9.1 and 1.9.2.

### Live data: steadier and faster
- The newest volume appears first, within a second or two; the rest of the loop fills in newest first.
- A chunk that never arrives no longer stalls the feed. It's skipped after 30 s, and the volume is
  swapped for the complete archive file when NOAA publishes it, so you don't have to switch radars to fix it.
- A volume that never finishes, or a radar that restarts its numbering, is noticed within about 90 s
  (it used to take 15 minutes).
- Holes in the loop are filled from the archive every 90 s.
- Chunks download in parallel on their own thread, failed downloads retry, and switching radars stops
  the old downloads straight away.
- **Radar → Reload live data (F5)** restarts the feed for the current radar.
- The download cache is kept under 3 GB.

### New
- **Toolbar switches** (also in the View menu):
  - **Smoothing (S)**
  - **Dealias velocity (D)** – unfolds Level II velocity and SRV, and Level III velocity and SRV.
  - **Σ Max value trail (Ctrl+T)** – each panel shows the most extreme value over the loop up to the
    frame shown: hail swaths, rotation tracks, strongest winds. On CC it shows the lowest value, which traces debris.
- **Street cameras** (Layers → Street cameras): traffic camera icons appear when you zoom in. Click
  one for its live picture.
  - California works out of the box.
  - NY, GA, ID, AK, LA, UT, WI, AZ, NV, CT, FL and Windy Webcams (worldwide) need a free key,
    added in Camera sources & keys.
- **Map style** (Settings → Map style):
  - Colours and widths for roads and borders, plus colours for radar sites and cities.
  - Fonts for city labels, radar site labels and panel titles.

### Fixed
- Archive Level III for older dates: falls back to N0Q / N0U when N0B / N0G don't exist.
- Archive loops: Level III no longer creates extra frames without radar data, so every frame has base
  reflectivity and velocity.
- The toolbar fits narrow windows: the words beside the icons are dropped first, so no buttons get hidden.

### Changed
- Warnings panel: **Only in view** is now off by default, and RadarForge remembers your choice.
- Removed the Level III mesocyclone circles and TVS triangles from the map. The Storm cells table
  still lists both.

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
