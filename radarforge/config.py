"""Settings persistence and standard paths."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

APP_NAME = "RadarForge"


def _dirs():
    """(settings dir, cache dir) for this operating system."""
    home = Path.home()
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return roaming / APP_NAME, local / APP_NAME / "cache"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME, home / "Library" / "Caches" / APP_NAME
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return config / "radarforge", cache / "radarforge"


CONFIG_DIR, CACHE_DIR = _dirs()          # settings, themes, colour tables  |  downloads, log file
PALETTE_USER_DIR = CONFIG_DIR / "palettes"
PLACEFILE_CACHE = CACHE_DIR / "placefiles"
LOG_FILE = CACHE_DIR / "radarforge.log"

DEFAULTS = {
    "site": "KTLX",
    "layout": 4,
    "panels": ["REF", "VEL", "CC", "ZDR", "SRV", "KDP"],
    "tilt": 0,
    "smoothing": False,
    "storm_motion_dir": 240.0,   # degrees the storm is moving FROM
    "storm_motion_kts": 30.0,
    "srv_use_dealiased": True,
    "freezing_level_ft": 13000.0,   # ft MSL
    "minus20_level_ft": 22000.0,    # ft MSL
    "palette_overrides": {},        # product id -> .pal path
    "placefiles": [],               # [{url, enabled, title}]
    "map_layers": {"states": True, "counties": True, "roads": True, "cities": True,
                   "lakes": True, "countries": True, "range_rings": False},
    "overlays": {"warnings": True, "watches": True, "storm_tracks": True, "hail": False, "melting_layer": False, "reports": False,
                 "chasers": False, "spc_outlook": False, "spc_mcd": False,
                 "satellite": False, "mrms": False, "lightning": False, "lightning_density": False,
                 "surface_obs": False, "storm_flags": False, "cameras": False,
                 # (1.20) winter and tropical layers
                 "freezing_line": False, "snowfall": False, "tropical": False, "tropical_wind": True,
                 "tropical_spaghetti": False, "tropical_outlook": False, "buoys": False},
    "warning_types": {"TOR": True, "SVR": True, "FFW": True, "OTH": True, "WIN": True, "TRO": True},   # watches:
                                   # overlays["watches"]; WIN winter storm / blizzard / ice…, TRO hurricane / storm surge…
    "warning_colors": {},          # (1.5.0) event -> "#rrggbb"; replaced by warning_lines
    "warning_lines": {},           # code (TOR, TORR, SVRD...) -> {"color", "width", "kind"}
    "go_to_nearest_radar": True,   # going to a warning switches to the radar nearest it
    "warning_flash_s": 8,          # a newly issued (or upgraded) warning flashes on the map this long; 0 = never
    "report_hours": 3,             # live storm reports: how many hours back (1, 3, 6, 12, 24)
    "report_types": {"tornado": True, "hail": True, "wind": True, "flood": True, "winter": True, "other": False},
    "spotter_reports": True,       # add Spotter Network reports to the NWS ones (live)
    "chasers_active_only": False,  # Spotter Network: only members with 5+ accepted reports
    "chaser_names": True,
    "favorite_sites": [],
    "my_location": None,           # [lat, lon] set from the map's right-click menu
    "warn_at_location": True,      # pop up a new warning that covers my location (live)
    "notified_warnings": {},       # warning key -> [priority, expiry epoch s] already shown for my location
    "track_minutes": 60,           # storm track tool: minutes the arrow covers
    "spc_outlook_day": 1,          # SPC convective outlook shown: day 1, 2 or 3
    "satellite_channel": "ir",     # ir | vis | wv (GOES via IEM, live)
    "satellite_opacity": 0.85,
    "satellite_enhance": True,     # colour-enhanced infrared / water vapour
    "satellite_sector": "conus",   # conus | storm (a 1-minute mesoscale sector aimed at a followed storm)
    "mrms_product": "rot60",       # see features/mrms.py PRODUCTS
    "mrms_opacity": 0.8,
    "ltg_density_product": "ltg5",
    "lightning_minutes": 10,       # GLM flashes: how many minutes back
    "saved_locations": None,       # [{id, name, lat, lon, mine, enabled, warn{}, ...}] (1.9.0; None = migrate)
    "alert_sound": "chime",        # chime | siren | beep | none
    "alert_volume": 0.8,
    "learn_mode": False,           # plain-language explanations in the Inspector
    "dealias_velocity": False,     # (1.9.1) base velocity panels show dealiased velocity (toolbar button)
    "trail_mode": False,           # (1.9.1) Σ: panels show the max (CC: min) of the loop up to the frame shown
    "warnings_in_view": False,     # Warnings panel: list only warnings in the area you're looking at
    "show_welcome": False,         # the Getting started page at start-up (a fresh install shows it once)
    "hints_seen": [],              # one-time hints already shown (Models view, Layers presets…)
    # (1.20) the Models view
    "model_id": "hrrr",            # hrrr | nam | rap | gfs
    "model_run": "",               # YYYYMMDDHH of a chosen run ("" = the newest)
    "model_fh": 0,                 # forecast hour shown
    "model_fields": ["refc", "cape_sb", "srh01", "stp", "shear06", "td2m"],   # one field per panel
    "model_region": "CONUS",       # a features/models.py REGIONS name, or "radar"
    "model_opacity": 0.9,
    # (1.20) winter and tropical
    "snowfall_hours": 24,          # NOHRSC snowfall analysis: 24, 48 or 72 hours
    "tropical_models": ["OFCL", "AVNO", "HFAI", "HFBI", "CMC", "NVGM", "UKX", "TVCN", "CLP5"],   # spaghetti aids
    "layer_preset": "",            # the Layers preset last chosen (severe | winter | tropical | clean)
    "ptype_prev": "REF",           # panel 1's product before precipitation type took it over
    "quick_collapsed": ["overlays", "satellite", "display"],   # Layers panel sections folded away (new installs:
                                   # the specialist ones; 1.20 renamed the sections)
    "workspace_version": 0,        # bumped when the default panel arrangement changes (applied once)
    "camera_caltrans": True,       # street cameras: California DOT (open data, no key)
    "camera_keys": {},             # street cameras: state code (NY, GA...) or "windy" -> free API key
    "loop_frames": 10,             # (1.14: 12 -> 10) frames a radar's loop loads; more can be chosen (up to 60)
    "loop_fps": 6.0,
    "loop_dwell": 1.5,             # extra seconds on the last frame
    "live_poll_seconds": 15,
    "l2_source": "aws",            # live Level II: "aws" (NOAA's buckets) or a polling server's address
    "polling_servers": [{"name": "Iowa State (IEM)",
                         "url": "https://mesonet-nexrad.agron.iastate.edu/level2/raw/"}],
    "distance_units": "nm",       # nm | km | mi
    "height_units": "kft",        # kft | km
    "volume_cache": 4,            # decoded Level II volumes held in RAM
    "image_cache_mb": 300,        # (1.11.1: 600 -> 300, now counting everything an image holds)
    "window_geometry": None,
    "workspace": None,            # panel layout (see ui/workspace.py)
    "theme": "RadarForge Dark",
    "start_live": True,
    "show_legend": True,
    "view": None,                 # {cx, cy, scale}
    "gpu_smooth": True,
    "velocity_filter": 1,          # 0 off, 1 normal, 2 aggressive
    "settings_version": 5,
    "invert_scroll": False,
    "cursor_link": True,
    "hover_text": True,
    "maps_under_radar": True,      # roads, borders, lakes drawn beneath the radar data (False: on top)
    "l3_poll_products": ["NST", "NHI"],
    "xsection_top_kft": 60.0,
    "volume3d_levels": [30.0, 50.0, 65.0],   # (before 1.12)
    "volume3d": {},                # 3-D view: product, style, levels, opacity, height ×, cut ... (tools/volume3d.py)
    "gl_platform": None,          # remembered working OpenGL platform (see gl_setup.py)
    "gl_format": None,
    "scene_cache": True,          # reuse the drawn map while only the cursor moves
    "update_check": True,         # (1.11) look for a newer release on GitHub about once a day
    "update_last_check": 0,       # epoch s of the last automatic check
    "update_latest": "",          # newest release that check found ("" = none newer)
    "update_skip": "",            # a release the user chose to skip
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (CONFIG_DIR / "settings.json")
        self.fresh = False
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        user_version = DEFAULTS["settings_version"]      # fresh installs start on the current defaults
        try:
            with open(self.path, encoding="utf-8") as fh:
                user = json.load(fh)
            user_version = int(user.get("settings_version") or 1)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                    self.data[k].update(v)
                else:
                    self.data[k] = v
        except FileNotFoundError:
            self.fresh = True                            # a new install: the Getting started page opens once
            self.data["show_welcome"] = True
        except Exception as exc:  # corrupt file: keep defaults
            print("settings: could not read", self.path, exc)
        if user_version < 2:
            # v2: smoothing on by default (matches GR2Analyst's look)
            self.data["gpu_smooth"] = True
            self.data["settings_version"] = 2
        if user_version < 3:
            # v3 (1.11.1): the image cache now counts all the memory it uses; the old default becomes 300 MB
            if int(self.data.get("image_cache_mb") or 600) == 600:
                self.data["image_cache_mb"] = 300
            self.data["settings_version"] = 3
        if user_version < 4:
            # v4 (1.14): a radar loads 10 frames by default; a loop still on the old default of 12 becomes 10
            # (any other length someone picked stays)
            try:
                if int(self.data.get("loop_frames") or 12) == 12:
                    self.data["loop_frames"] = 10
            except (TypeError, ValueError):
                self.data["loop_frames"] = 10
            self.data["settings_version"] = 4
        if user_version < 5:
            # v5 (1.20): the Quick panel became the Layers panel, with new sections
            self.data["quick_collapsed"] = list(DEFAULTS["quick_collapsed"])
            self.data["settings_version"] = 5

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            print("settings: could not save", exc)

    def __getitem__(self, k):
        return self.data.get(k, DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)


def ensure_dirs():
    for d in (CONFIG_DIR, CACHE_DIR, PALETTE_USER_DIR, PLACEFILE_CACHE):
        d.mkdir(parents=True, exist_ok=True)
