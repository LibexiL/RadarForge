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
    "overlays": {"warnings": True, "watches": True, "storm_tracks": True, "meso": True,
                 "tvs": True, "hail": False, "melting_layer": False, "reports": False,
                 "chasers": False, "spc_outlook": False, "spc_mcd": False, "satellite": False, "lightning": False},
    "warning_types": {"TOR": True, "SVR": True, "FFW": True, "OTH": True},   # watches: overlays["watches"]
    "warning_colors": {},          # (1.5.0) event -> "#rrggbb"; replaced by warning_lines
    "warning_lines": {},           # code (TOR, TORR, SVRD...) -> {"color", "width", "kind"}
    "warning_sort": "severity",    # Warnings panel order: severity | time | distance
    "go_to_nearest_radar": True,   # going to a warning switches to the radar nearest it
    "report_hours": 3,             # live storm reports: how many hours back (1, 3, 6, 12, 24)
    "report_types": {"tornado": True, "hail": True, "wind": True, "flood": True, "other": False},
    "spotter_reports": True,       # add Spotter Network reports to the NWS ones (live)
    "chasers_active_only": False,  # Spotter Network: only members with 5+ accepted reports
    "chaser_names": True,
    "satellite_channel": "ir",     # ir | wv | swir | vis (see data/goes.py)
    "satellite_sat": "auto",       # auto | east | west
    "satellite_opacity": 0.8,
    "lightning_minutes": 10,       # minutes of GLM flashes shown (5, 10, 15, 30)
    "favorite_sites": [],
    "my_location": None,           # [lat, lon] of the first saved location (kept for older versions)
    "locations": [],               # saved locations: [{id, name, lat, lon, rules}] (services/locations.py)
    "alert_options": {"popup": True, "sound": True, "notify": True},
    "warn_at_location": True,      # alerts for the saved locations are on (live data)
    "notified_warnings": {},       # warning key -> [priority, expiry epoch s] already shown for my location
    "track_minutes": 60,           # storm track tool: minutes the arrow covers
    "loop_frames": 12,
    "loop_fps": 6.0,
    "loop_dwell": 1.5,             # extra seconds on the last frame
    "export": {},                  # last choices in the Export loop dialog
    "bookmarks": [],               # saved views (services/views.py)
    "named_workspaces": {},        # your own layouts: name -> workspace
    "live_poll_seconds": 15,
    "distance_units": "nm",       # nm | km | mi
    "height_units": "kft",        # kft | km
    "volume_cache": 4,            # decoded Level II volumes held in RAM
    "image_cache_mb": 600,
    "window_geometry": None,
    "workspace": None,            # panel layout (see ui/workspace.py)
    "theme": "RadarForge Dark",
    "start_live": True,
    "show_legend": True,
    "view": None,                 # {cx, cy, scale}
    "gpu_smooth": True,
    "velocity_filter": 1,          # 0 off, 1 normal, 2 aggressive
    "settings_version": 2,
    "invert_scroll": False,
    "cursor_link": True,
    "hover_text": True,
    "l3_poll_products": ["NST", "NMD", "NTV", "NHI"],
    "xsection_top_kft": 60.0,
    "volume3d_levels": [30.0, 50.0, 65.0],
    "gl_platform": None,          # remembered working OpenGL platform (see gl_setup.py)
    "gl_format": None,
    "scene_cache": True,          # reuse the drawn map while only the cursor moves
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (CONFIG_DIR / "settings.json")
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        user_version = 2           # fresh installs start on the current defaults
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
            pass
        except Exception as exc:  # corrupt file: keep defaults
            print("settings: could not read", self.path, exc)
        if user_version < 2:
            # v2: smoothing on by default (matches GR2Analyst's look)
            self.data["gpu_smooth"] = True
            self.data["settings_version"] = 2

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
