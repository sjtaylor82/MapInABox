"""Visual world-map panel and shared country geometry."""

import gzip
import json
import math
import os
import pickle
import time

import wx

from app_paths import CACHE_DIR, RESOURCE_DIR
from logging_utils import miab_log
from sound_engine import COUNTRY_ALIASES

GEOJSON_PATH = os.path.join(RESOURCE_DIR, "countries.geojson.gz")

GEOJSON_PROCESSED_CACHE_PATH = os.path.join(CACHE_DIR, "countries_geojson_processed.pkl")

COL_BG      = wx.Colour(10,  20,  40)

COL_OCEAN   = wx.Colour(20,  50,  90)

COL_LAND    = wx.Colour(40,  80,  55)

COL_BORDER  = wx.Colour(30,  60,  40)

COL_GRID    = wx.Colour(30,  60,  80)

COL_DOT     = wx.Colour(255, 60,  60)

COL_RING    = wx.Colour(255, 180, 50)

def _load_geojson_polygons():
    """Load and simplify country polygons from countries.geojson.
    Returns:
        rings     — flat list of (lon,lat) coordinate rings for drawing
        countries — list of dicts {name, iso2, centroid_lon, centroid_lat, rings_idx}
                    where rings_idx is list of indices into rings[]
    """
    if not os.path.exists(GEOJSON_PATH):
        return [], [], []
    source_sig = None
    try:
        source_sig = (os.path.getmtime(GEOJSON_PATH), os.path.getsize(GEOJSON_PATH))
        if os.path.exists(GEOJSON_PROCESSED_CACHE_PATH):
            with open(GEOJSON_PROCESSED_CACHE_PATH, "rb") as f:
                cached = pickle.load(f)
            if cached.get("source_sig") == source_sig:
                return (
                    cached.get("rings") or [],
                    cached.get("countries") or [],
                    cached.get("land_polygons") or [],
                )
    except Exception:
        pass
    try:
        from shapely.geometry import shape
        with gzip.open(GEOJSON_PATH, 'rt', encoding="utf-8") as f:
            data = json.load(f)
        rings     = []
        countries = []
        land_polygons = []
        for feature in data["features"]:
            props    = feature.get("properties", {})
            name     = (props.get("NAME") or props.get("name") or
                        props.get("ADMIN") or "").strip()
            iso2     = (props.get("ISO_A2") or props.get("iso_a2") or "").strip()
            if iso2 in ("-99", "-1", "", None):
                iso2 = name[:2].upper() if name else "??"

            geom  = shape(feature["geometry"])
            land_polygons.append(geom)
            polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]

            country_ring_indices = []
            all_lons, all_lats   = [], []

            for poly in polys:
                simplified = poly.simplify(0.1, preserve_topology=True)
                sub_polys  = (list(simplified.geoms)
                              if simplified.geom_type == "MultiPolygon"
                              else [simplified])
                for sub in sub_polys:
                    if sub.is_empty:
                        continue
                    coords = list(sub.exterior.coords)
                    if len(coords) < 3:
                        continue
                    lons = [c[0] for c in coords]
                    if max(lons) - min(lons) > 180:
                        continue
                    country_ring_indices.append(len(rings))
                    rings.append(coords)
                    all_lons.extend(lons)
                    all_lats.extend(c[1] for c in coords)

            if country_ring_indices and all_lons:
                centroid_lon = sum(all_lons) / len(all_lons)
                centroid_lat = sum(all_lats) / len(all_lats)
                countries.append({
                    "name":         name,
                    "iso2":         iso2,
                    "centroid_lon": centroid_lon,
                    "centroid_lat": centroid_lat,
                    "rings_idx":    country_ring_indices,
                })

        if source_sig is not None:
            try:
                with open(GEOJSON_PROCESSED_CACHE_PATH, "wb") as f:
                    pickle.dump(
                        {
                            "source_sig": source_sig,
                            "rings": rings,
                            "countries": countries,
                            "land_polygons": land_polygons,
                        },
                        f,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
            except Exception:
                pass
        return rings, countries, land_polygons
    except Exception:
        return [], [], []

_GEO_RINGS, _GEO_COUNTRIES, _GEO_LAND_POLYGONS = _load_geojson_polygons()

_ANTARCTICA = [
    (-180, -90), (-180, -60), (-150, -65), (-120, -67), (-90, -65),
    (-60, -70),  (-30, -72),  (0,   -70),  (30,  -68),  (60,  -70),
    (90,  -65),  (120, -67),  (150, -65),  (180, -60),  (180, -90),
    (-180, -90),
]

_GEO_COUNTRIES.append({
    "name": "Antarctica", "iso2": "AQ",
    "centroid_lon": 0.0, "centroid_lat": -80.0,
    "rings_idx": [len(_GEO_RINGS)],
})

_GEO_RINGS.append(_ANTARCTICA)

def _build_land_checker(polygons=None):
    """Build a fast point-in-polygon land checker from the GeoJSON."""
    polygons = polygons or []
    if not polygons and not os.path.exists(GEOJSON_PATH):
        return lambda lat, lon: False
    try:
        from shapely.geometry import Point
        if not polygons:
            from shapely.geometry import shape
            with gzip.open(GEOJSON_PATH, 'rt', encoding='utf-8') as f:
                data = json.load(f)
            for feature in data['features']:
                try:
                    polygons.append(shape(feature['geometry']))
                except Exception:
                    pass
        def is_land(lat, lon):
            pt = Point(lon, lat)
            return any(p.contains(pt) for p in polygons)
        return is_land
    except Exception as e:
        miab_log("errors", f"[Map] Land checker failed: {e}", None)
        return lambda lat, lon: False

_IS_LAND   = _build_land_checker(_GEO_LAND_POLYGONS)

class WorldMapPanel(wx.Panel):
    """Accurate world map from GeoJSON.

    World mode keeps the current ISO-2 label layer. Country mode reuses the
    same base map but swaps in a calmer, location-focused overlay.
    F8 still flashes the current country; Shift+F8 cycles the overlay mode.
    """

    _COL_LABEL      = wx.Colour(255, 220,  50)
    _COL_LABEL_OUT  = wx.Colour(0,   0,    0)
    _COL_FLASH_FILL = wx.Colour(255, 200,  0, 180)
    _LABEL_SIZE     = 11
    _FLASH_SIZE     = 28

    def AcceptsFocusFromKeyboard(self):
        return False

    def __init__(self, parent, owner=None):
        super().__init__(parent, style=wx.NO_BORDER)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(COL_BG)
        self._owner        = owner
        self.lat          = 0.0
        self.lon          = 0.0
        self.street_mode  = False
        self.street_label = ""
        self._flash_name  = ""
        self._flash_rings = []
        self._flash_cx    = 0.0
        self._flash_cy    = 0.0
        self._country_visual_generation = 0
        self._bg_bitmap   = None
        self._bg_bitmap_mode = None
        self._bg_bitmap_view_key = None
        self._view_bounds_cache_key = None
        self._view_bounds_cache_value = None
        self._country_bounds_cache = {}
        self._label_cache_size = (-1, -1)
        self._classroom_trail = []
        self._visual_assist_caption = ""
        self._visual_assist_caption_generation = 0
        self._visual_assist_pulse_timer = wx.Timer(self)
        self._visual_assist_pulse_until = 0.0
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE,  self._on_size)
        self.Bind(wx.EVT_TIMER, self._on_visual_assist_pulse,
                  self._visual_assist_pulse_timer)

    def _on_size(self, event):
        self._label_cache_size = (-1, -1)
        self._bg_bitmap = None   # invalidate background cache
        self._bg_bitmap_mode = None
        self._bg_bitmap_view_key = None
        self.Refresh()
        event.Skip()

    def set_position(self, lat, lon, street_mode=False, street_label=""):
        position_changed = (
            float(lat) != float(self.lat) or float(lon) != float(self.lon)
            or bool(street_mode) != bool(self.street_mode)
        )
        if self._classroom_mode_active() and position_changed:
            point = (float(lat), float(lon), bool(street_mode))
            if not self._classroom_trail or point[:2] != self._classroom_trail[-1][:2]:
                self._classroom_trail.append(point)
                self._classroom_trail = self._classroom_trail[-120:]
        self.lat          = lat
        self.lon          = lon
        self.street_mode  = street_mode
        self.street_label = street_label
        if self._classroom_mode_active():
            self._visual_assist_pulse_until = time.monotonic() + 0.9
            if not self._visual_assist_pulse_timer.IsRunning():
                self._visual_assist_pulse_timer.Start(140)
        if not street_mode and self._visual_zoom_factor() > 1:
            self._bg_bitmap = None
            self._bg_bitmap_view_key = None
        self.Refresh()

    def set_classroom_mode(self, enabled):
        """Start or stop a visual map session without adding focusable UI."""
        if enabled:
            self._classroom_trail = [(float(self.lat), float(self.lon), bool(self.street_mode))]
            self._visual_assist_pulse_until = time.monotonic() + 0.9
            self._visual_assist_pulse_timer.Start(140)
        else:
            self._visual_assist_pulse_timer.Stop()
            self._visual_assist_caption_generation += 1
            self._visual_assist_caption = ""
        self.Refresh()

    def show_visual_assist_caption(self, message, duration_ms=7000):
        """Show requested/spoken information temporarily in Visual Assist."""
        if not self._classroom_mode_active():
            return
        text = " ".join(str(message or "").split())
        if not text:
            return
        self._visual_assist_caption_generation += 1
        generation = self._visual_assist_caption_generation
        self._visual_assist_caption = text
        self.Refresh(False)

        def clear_caption():
            if generation != self._visual_assist_caption_generation:
                return
            self._visual_assist_caption = ""
            self.Refresh(False)

        wx.CallLater(max(1, int(duration_ms)), clear_caption)

    def _on_visual_assist_pulse(self, event):
        if (self._classroom_mode_active()
                and time.monotonic() < self._visual_assist_pulse_until
                and self.IsShownOnScreen()):
            self.Refresh(False)
        else:
            self._visual_assist_pulse_timer.Stop()
            if self._classroom_mode_active():
                self.Refresh(False)

    def _classroom_mode_active(self):
        return bool(self._owner and getattr(self._owner, "_map_fullscreen", False))

    @staticmethod
    def _coordinate_text(value, positive, negative):
        return f"{abs(float(value)):.3f}\N{DEGREE SIGN} {positive if value >= 0 else negative}"

    def _draw_classroom_trail(self, gc, w, h, geo_kwargs=None):
        if not self._classroom_mode_active() or len(self._classroom_trail) < 2:
            return
        geo_kwargs = geo_kwargs or {}
        expected_street = bool(geo_kwargs)
        points = []
        last_lon = None
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(70, 190, 205, 135)).Width(2)))
        for lat, lon, street in self._classroom_trail:
            if street != expected_street:
                points = []
                last_lon = None
                continue
            if not expected_street and last_lon is not None and abs(lon - last_lon) > 180:
                points = []
            px, py = self._geo_to_px(lon, lat, w, h, **geo_kwargs)
            if 0 <= px <= w and 0 <= py <= h:
                if points:
                    gc.StrokeLine(points[-1][0], points[-1][1], px, py)
                points.append((px, py))
            last_lon = lon

    def _draw_classroom_destination(self, gc, w, h, geo_kwargs=None):
        owner = self._owner
        destination = getattr(owner, "_map_destination", None) if owner else None
        if not self._classroom_mode_active() or not destination:
            return
        try:
            lat, lon = destination["coords"]
            px, py = self._geo_to_px(lon, lat, w, h, **(geo_kwargs or {}))
        except (KeyError, TypeError, ValueError):
            return
        if not (0 <= px <= w and 0 <= py <= h):
            return
        size = max(8, min(15, int(min(w, h) / 50)))
        path = gc.CreatePath()
        path.MoveToPoint(px, py - size)
        path.AddLineToPoint(px + size, py)
        path.AddLineToPoint(px, py + size)
        path.AddLineToPoint(px - size, py)
        path.CloseSubpath()
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(255, 210, 0))))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(20, 20, 20)).Width(3)))
        gc.DrawPath(path)

    def _draw_classroom_hud(self, gc, w, h):
        if not self._classroom_mode_active() or w < 240 or h < 160:
            return
        owner = self._owner
        if getattr(owner, "_walking_mode", False):
            mode = "Walking map"
        elif self.street_mode:
            mode = "Street map"
        elif self._map_display_mode() == "country":
            mode = "Country map"
        else:
            mode = "World map"
        mode += f"  \N{BULLET}  {self._visual_zoom_factor()}x"
        coords = (self._coordinate_text(self.lat, "N", "S") + "   " +
                  self._coordinate_text(self.lon, "E", "W"))
        font_size = max(13, min(24, int(h / 32)))
        small_size = max(10, min(17, int(font_size * .7)))
        pad = max(10, int(font_size * .65))
        band_h = int(font_size * 2.7)
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(5, 16, 30, 225))))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(0, 235, 255)).Width(2)))
        gc.DrawRoundedRectangle(pad, pad, max(1, w - pad * 2), band_h, 8)
        gc.SetFont(gc.CreateFont(
            wx.Font(font_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
            wx.Colour(255, 255, 255)))
        gc.DrawText(mode, pad * 2, pad * 1.35)
        gc.SetFont(gc.CreateFont(
            wx.Font(small_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL),
            wx.Colour(190, 245, 255)))
        gc.DrawText(coords, pad * 2, pad * 1.45 + font_size * 1.2)
        # Shape plus label means north is not communicated by colour alone.
        nx, ny = w - pad * 3, pad + band_h + pad * 2
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(255, 255, 255)).Width(3)))
        gc.StrokeLine(nx, ny + 24, nx, ny)
        gc.StrokeLine(nx, ny, nx - 7, ny + 10)
        gc.StrokeLine(nx, ny, nx + 7, ny + 10)
        gc.SetFont(gc.CreateFont(
            wx.Font(small_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
            wx.Colour(255, 255, 255)))
        gc.DrawText("N", nx - small_size / 2, ny - small_size * 1.4)

    def _draw_visual_assist_caption(self, gc, w, h):
        """Draw the temporary visual equivalent of requested speech."""
        text = self._visual_assist_caption
        if not self._classroom_mode_active() or not text or w < 240 or h < 160:
            return
        font_size = max(14, min(24, int(h / 27)))
        pad = max(12, int(font_size * .7))
        max_width = max(100, w - pad * 4)
        gc.SetFont(gc.CreateFont(
            wx.Font(font_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
            wx.Colour(255, 255, 255)))
        lines = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if current and gc.GetTextExtent(candidate)[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        max_lines = max(2, min(6, int((h * .38) / (font_size * 1.35))))
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(" .") + "…"
        line_h = int(font_size * 1.35)
        box_h = pad * 2 + line_h * len(lines)
        box_x = pad
        box_y = h - box_h - pad
        box_w = w - pad * 2
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(3, 10, 22, 238))))
        gc.SetPen(gc.CreatePen(
            wx.GraphicsPenInfo(wx.Colour(255, 215, 50)).Width(3)))
        gc.DrawRoundedRectangle(box_x, box_y, box_w, box_h, 10)
        for index, line in enumerate(lines):
            gc.DrawText(line, box_x + pad,
                        box_y + pad + index * line_h)

    def set_flash(self, name, rings_idx, centroid_lon, centroid_lat):
        self._country_visual_generation += 1
        self._country_visual_shown_at = time.time()
        self._flash_name  = name
        self._flash_rings = rings_idx
        self._flash_cx    = centroid_lon
        self._flash_cy    = centroid_lat
        self.Refresh()
        # F8 may also trigger an external visual-description utility.  Refresh
        # merely queues a paint, allowing that utility to capture the old map.
        # Update synchronously so the country silhouette exists before the key
        # handler returns and a screenshot can be taken.
        self.Update()
        return self._country_visual_generation

    def _clear_flash(self):
        self._country_visual_generation += 1
        self._flash_name  = ""
        self._flash_rings = []
        self.Refresh()

    def dismiss_country_visual(self):
        if self._flash_name:
            self._clear_flash()

    def _visual_zoom_factor(self):
        owner = self._owner_ref()
        return int(getattr(owner, "map_zoom_factor", 1)) if owner else 1

    def _view_bounds(self, w, h):
        zoom = max(1.0, float(self._visual_zoom_factor()))
        owner = self._owner_ref()
        country = getattr(owner, "last_country_found", "") if owner else ""
        explore_points = getattr(self, "_explore_path_points", None) or []
        explore_key = (
            len(explore_points),
            tuple(round(v, 5) for v in explore_points[0]) if explore_points else None,
            tuple(round(v, 5) for v in explore_points[-1]) if explore_points else None,
        )
        cache_key = (w, h, self._map_display_mode(), zoom,
                     round(self.lat, 5) if zoom > 1 else None,
                     round(self.lon, 5) if zoom > 1 else None, country,
                     explore_key)
        if cache_key == self._view_bounds_cache_key:
            return self._view_bounds_cache_value
        lon_min, lon_max, lat_min, lat_max = -180.0, 180.0, -90.0, 90.0
        if explore_points:
            lats = [point[0] for point in explore_points]
            lons = [point[1] for point in explore_points]
            lon_min, lon_max = min(lons), max(lons)
            lat_min, lat_max = min(lats), max(lats)
            lon_pad = max(0.005, (lon_max - lon_min) * 0.08)
            lat_pad = max(0.005, (lat_max - lat_min) * 0.08)
            lon_min -= lon_pad; lon_max += lon_pad
            lat_min -= lat_pad; lat_max += lat_pad
        elif self._map_display_mode() == "country":
            cached_country_bounds = self._country_bounds_cache.get(country)
            if cached_country_bounds:
                lon_min, lon_max, lat_min, lat_max = cached_country_bounds
            else:
                entry = self._country_entry_for(country)
                points = []
                centre = float(entry.get("centroid_lon", 0.0)) if entry else 0.0
                for ring_idx in (entry.get("rings_idx", []) if entry else []):
                    if not (0 <= ring_idx < len(_GEO_RINGS)):
                        continue
                    for lon, lat in _GEO_RINGS[ring_idx]:
                        while lon - centre > 180: lon -= 360
                        while lon - centre < -180: lon += 360
                        points.append((lon, lat))
                if points:
                    lon_min, lon_max = min(p[0] for p in points), max(p[0] for p in points)
                    lat_min, lat_max = min(p[1] for p in points), max(p[1] for p in points)
                    lon_pad = max(.3, (lon_max - lon_min) * .10)
                    lat_pad = max(.3, (lat_max - lat_min) * .10)
                    lon_min -= lon_pad; lon_max += lon_pad
                    lat_min -= lat_pad; lat_max += lat_pad
                    self._country_bounds_cache[country] = (
                        lon_min, lon_max, lat_min, lat_max)
        lon_span = max(.1, lon_max - lon_min)
        lat_span = max(.1, lat_max - lat_min)
        panel_ratio = max(.2, (w - 12) / max(1.0, h - 12))
        if lon_span / lat_span < panel_ratio:
            lon_span = lat_span * panel_ratio
        else:
            lat_span = lon_span / panel_ratio
        if explore_points:
            centre_lon = (lon_min + lon_max) / 2
            centre_lat = (lat_min + lat_max) / 2
            zoom = 1.0
        elif zoom > 1:
            centre_lon, centre_lat = float(self.lon), float(self.lat)
        else:
            centre_lon = (lon_min + lon_max) / 2
            centre_lat = (lat_min + lat_max) / 2
        lon_span = min(360.0, lon_span / zoom)
        lat_span = min(180.0, lat_span / zoom)
        centre_lat = max(-90 + lat_span / 2, min(90 - lat_span / 2, centre_lat))
        result = (centre_lon - lon_span / 2, centre_lon + lon_span / 2,
                  centre_lat - lat_span / 2, centre_lat + lat_span / 2)
        self._view_bounds_cache_key = cache_key
        self._view_bounds_cache_value = result
        return result

    def set_explore_path(self, points=None, index=0, travel_mode="driving"):
        """Show or clear the active Journey Planner route overlay."""
        new_points = list(points or [])
        geometry_changed = new_points != getattr(self, "_explore_path_points", [])
        self._explore_path_points = new_points
        self._explore_path_index = max(0, int(index))
        self._explore_path_mode = travel_mode or "driving"
        if geometry_changed:
            self._view_bounds_cache_key = None
            self._bg_bitmap = None
            self._bg_bitmap_view_key = None
        self.Refresh()

    def _geo_to_px(self, lon, lat, w, h, margin=6,
                   lon_min=None, lon_max=None, lat_min=None, lat_max=None):
        if None in (lon_min, lon_max, lat_min, lat_max):
            lon_min, lon_max, lat_min, lat_max = self._view_bounds(w, h)
        centre_lon = (lon_min + lon_max) / 2
        while lon - centre_lon > 180: lon -= 360
        while lon - centre_lon < -180: lon += 360
        x = margin + (lon - lon_min) / (lon_max - lon_min) * (w - 2 * margin)
        y = margin + (lat_max - lat) / (lat_max - lat_min) * (h - 2 * margin)
        return int(x), int(y)

    def px_to_geo(self, x, y):
        w, h = self.GetSize()
        margin = 6
        if w <= margin * 2 or h <= margin * 2:
            return self.lat, self.lon
        if self.street_mode:
            span = 0.02
            lon_min = self.lon - span;  lon_max = self.lon + span
            lat_min = self.lat - span;  lat_max = self.lat + span
        else:
            lon_min, lon_max, lat_min, lat_max = self._view_bounds(w, h)
        lon = lon_min + ((x - margin) / (w - 2 * margin)) * (lon_max - lon_min)
        lat = lat_max - ((y - margin) / (h - 2 * margin)) * (lat_max - lat_min)
        lat = max(-90.0, min(90.0, lat))
        lon = ((lon + 180.0) % 360.0) - 180.0
        return lat, lon

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetSize()
        if self._owner and getattr(self._owner, "_user_map_data", None):
            self._paint_user_map(dc, w, h)
            return
        if self.street_mode:
            gc = wx.GraphicsContext.Create(dc)
            if gc:
                self._paint_street(gc, w, h)
            return

        mode = self._map_display_mode()
        zoom = self._visual_zoom_factor()
        explore_points = getattr(self, "_explore_path_points", None) or []
        if explore_points:
            view_key = (
                mode, "explore", len(explore_points),
                tuple(round(v, 5) for v in explore_points[0]),
                tuple(round(v, 5) for v in explore_points[-1]),
            )
        else:
            view_key = (mode, zoom,
                        round(self.lat, 4) if zoom > 1 else None,
                        round(self.lon, 4) if zoom > 1 else None)

        # Build background bitmap once for the current map mode.
        if (not getattr(self, '_bg_bitmap', None) or
                getattr(self, '_bg_bitmap_size', None) != (w, h) or
                getattr(self, '_bg_bitmap_mode', None) != mode or
                getattr(self, '_bg_bitmap_view_key', None) != view_key):
            bmp = wx.Bitmap(w, h)
            mdc = wx.MemoryDC(bmp)
            gc2 = wx.GraphicsContext.Create(mdc)
            if gc2:
                self._paint_world_bg(gc2, w, h, include_labels=(mode == "world"))
            mdc.SelectObject(wx.NullBitmap)
            self._bg_bitmap      = bmp
            self._bg_bitmap_size = (w, h)
            self._bg_bitmap_mode = mode
            self._bg_bitmap_view_key = view_key

        # Blit cached background
        dc.DrawBitmap(self._bg_bitmap, 0, 0)

        gc = wx.GraphicsContext.Create(dc)
        if gc:
            self._draw_mode_overlay(gc, w, h)
            self._draw_explore_path(gc, w, h)
            self._draw_classroom_trail(gc, w, h)
            self._draw_classroom_destination(gc, w, h)
            px, py = self._geo_to_px(self.lon, self.lat, w, h)
            marker_size = (18 if (self._classroom_mode_active()
                                  and self._visual_zoom_factor() >= 8)
                           else 16 if self._classroom_mode_active()
                           else 12 if mode == "country" else 8)
            if self._classroom_mode_active():
                pulsing = time.monotonic() < self._visual_assist_pulse_until
                pulse = ((math.sin(time.monotonic() * 7.0) + 1.0) / 2.0
                         if pulsing else 0.0)
                halo_size = marker_size + 8 + pulse * 7
                halo_alpha = 58 if pulsing else 30
                gc.SetBrush(gc.CreateBrush(wx.Brush(
                    wx.Colour(255, 125, 35, halo_alpha))))
                gc.SetPen(gc.CreatePen(
                    wx.GraphicsPenInfo(wx.Colour(255, 205, 70, 180)).Width(3)))
                gc.DrawEllipse(px - halo_size, py - halo_size,
                               halo_size * 2, halo_size * 2)
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_RING)))
            gc.SetPen(wx.NullPen)
            gc.DrawEllipse(px - marker_size, py - marker_size,
                           marker_size * 2, marker_size * 2)
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_DOT)))
            dot_size = max(5, marker_size - 5)
            gc.DrawEllipse(px - dot_size, py - dot_size, dot_size * 2, dot_size * 2)
            self._draw_current_location_callout(gc, w, h, px, py)
            self._draw_classroom_hud(gc, w, h)
            self._draw_visual_assist_caption(gc, w, h)

    def _paint_user_map(self, dc, w, h):
        """Draw the background, author strokes, labels, and cursor of a local map."""
        owner = self._owner
        data = getattr(owner, "_user_map_floor", None) or owner._user_map_data
        dc.SetBackground(wx.Brush(wx.Colour(248, 248, 244)))
        dc.Clear()
        background = getattr(owner, "_user_map_background", "")
        if background and os.path.exists(background):
            try:
                bitmap = wx.Bitmap(background)
                if bitmap.IsOk():
                    image = bitmap.ConvertToImage().Scale(max(1, w), max(1, h), wx.IMAGE_QUALITY_HIGH)
                    dc.DrawBitmap(wx.Bitmap(image), 0, 0)
            except Exception:
                pass

        def to_px(x, y):
            return (x / data["width"] * w,
                    h - y / data["height"] * h)

        dc.SetPen(wx.Pen(wx.Colour(20, 85, 155), 5))
        for path in data.get("paths") or []:
            points = [to_px(*point) for point in path["points"]]
            for first, second in zip(points, points[1:]):
                dc.DrawLine(round(first[0]), round(first[1]),
                            round(second[0]), round(second[1]))

        explore = getattr(self, "_explore_path_points", None) or []
        if len(explore) >= 2:
            dc.SetPen(wx.Pen(wx.Colour(255, 135, 20), 7))
            route_points = [to_px(lon * 111195.0, lat * 111195.0) for lat, lon in explore]
            for first, second in zip(route_points, route_points[1:]):
                dc.DrawLine(round(first[0]), round(first[1]),
                            round(second[0]), round(second[1]))

        dc.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        for place in data.get("places") or []:
            px, py = to_px(place["x"], place["y"])
            dc.SetBrush(wx.Brush(wx.Colour(235, 75, 45)))
            dc.SetPen(wx.Pen(wx.Colour(80, 20, 10), 1))
            dc.DrawCircle(round(px), round(py), 6)
            dc.SetTextForeground(wx.Colour(15, 15, 15))
            dc.DrawText(place["name"], round(px + 9), round(py - 8))

        cursor_x, cursor_y = to_px(self.lon * 111195.0, self.lat * 111195.0)
        dc.SetBrush(wx.Brush(COL_RING))
        dc.SetPen(wx.Pen(wx.Colour(15, 15, 15), 2))
        dc.DrawCircle(round(cursor_x), round(cursor_y), 9)
        dc.SetBrush(wx.Brush(COL_DOT))
        dc.DrawCircle(round(cursor_x), round(cursor_y), 4)

    def _draw_explore_path(self, gc, w, h):
        points = getattr(self, "_explore_path_points", None) or []
        if len(points) < 2:
            return
        current = max(0, min(
            int(getattr(self, "_explore_path_index", 0)), len(points) - 1))
        mode = getattr(self, "_explore_path_mode", "driving")

        def stroke(segment, colour, width):
            if len(segment) < 2:
                return
            path = gc.CreatePath()
            path.MoveToPoint(*self._geo_to_px(segment[0][1], segment[0][0], w, h))
            for lat, lon in segment[1:]:
                path.AddLineToPoint(*self._geo_to_px(lon, lat, w, h))
            gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(colour).Width(width)))
            gc.StrokePath(path)

        remaining_colour = (wx.Colour(30, 210, 255) if mode == "walking"
                            else wx.Colour(255, 165, 35))
        # Dark underlay keeps both route colours visible over land and water.
        stroke(points, wx.Colour(10, 20, 30), 8)
        stroke(points[current:], remaining_colour, 5)
        stroke(points[:current + 1], wx.Colour(60, 230, 105), 5)

    def _draw_label(self, gc, text, cx, cy, size):
        font = wx.Font(size, wx.FONTFAMILY_SWISS,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(gc.CreateFont(font, self._COL_LABEL_OUT))
        for dx, dy in ((-1,-1),(1,-1),(-1,1),(1,1),(0,-1),(0,1),(-1,0),(1,0)):
            gc.DrawText(text, cx + dx, cy + dy)
        gc.SetFont(gc.CreateFont(font, self._COL_LABEL))
        gc.DrawText(text, cx, cy)

    def _draw_label_at_geo(self, gc, text, lon, lat, w, h, size, dx=0, dy=0):
        if not text:
            return
        px, py = self._geo_to_px(lon, lat, w, h)
        px += dx
        py += dy
        est_w = max(12, len(text) * size * 0.6)
        est_h = size + 4
        if px < -est_w or px > w + est_w or py < -est_h or py > h + est_h:
            return
        fx = max(4, min(int(px - est_w / 2), max(4, w - int(est_w) - 4)))
        fy = max(4, min(int(py - est_h / 2), max(4, h - int(est_h) - 4)))
        self._draw_label(gc, text, fx, fy, size)

    def _owner_ref(self):
        return getattr(self, "_owner", None)

    def _map_display_mode(self):
        owner = self._owner_ref()
        return getattr(owner, "map_display_mode", "world") if owner else "world"

    def _canonical_country_name(self, name):
        text = (name or "").strip()
        if not text:
            return ""
        return COUNTRY_ALIASES.get(text, text).strip().lower()

    def _country_entry_for(self, name):
        raw = (name or "").strip().lower()
        if not raw:
            return None
        for entry in _GEO_COUNTRIES:
            if str(entry.get("name", "")).strip().lower() == raw:
                return entry
        target = self._canonical_country_name(name)
        if not target:
            return None
        for entry in _GEO_COUNTRIES:
            if self._canonical_country_name(entry.get("name", "")) == target:
                return entry
        return None

    def _country_city_index_key(self, name):
        owner = self._owner_ref()
        if not owner:
            return ""
        target = self._canonical_country_name(name)
        if not target:
            return ""
        for key in getattr(owner, "_city_country_index", {}):
            if self._canonical_country_name(key) == target:
                return key
        return ""

    def _draw_ring_polygons(self, gc, ring_indices, w, h, fill, pen_colour):
        """Draw filled ring polygons (by index into _GEO_RINGS) with the given pen/fill."""
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(pen_colour).Width(1)))
        for ring_idx in ring_indices:
            if ring_idx < 0 or ring_idx >= len(_GEO_RINGS):
                continue
            ring = _GEO_RINGS[ring_idx]
            if not ring:
                continue
            gc.SetBrush(gc.CreateBrush(wx.Brush(fill)))
            pts = self._clipped_ring_pixels(ring, w, h)
            if len(pts) < 3:
                continue
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)

    def _clipped_ring_pixels(self, ring, w, h):
        """Clip a geographic polygon before scaling it into a zoomed viewport."""
        lon_min, lon_max, lat_min, lat_max = self._view_bounds(w, h)
        centre = (lon_min + lon_max) / 2
        polygon = []
        for lon, lat in ring:
            while lon - centre > 180: lon -= 360
            while lon - centre < -180: lon += 360
            polygon.append((lon, lat))

        def clip(points, inside, intersect):
            if not points:
                return []
            output = []
            previous = points[-1]
            previous_inside = inside(previous)
            for current in points:
                current_inside = inside(current)
                if current_inside != previous_inside:
                    output.append(intersect(previous, current))
                if current_inside:
                    output.append(current)
                previous, previous_inside = current, current_inside
            return output

        def vertical(boundary):
            return lambda a, b: (
                boundary,
                a[1] + (b[1] - a[1]) * (boundary - a[0]) /
                ((b[0] - a[0]) or 1e-12))

        def horizontal(boundary):
            return lambda a, b: (
                a[0] + (b[0] - a[0]) * (boundary - a[1]) /
                ((b[1] - a[1]) or 1e-12),
                boundary)

        polygon = clip(polygon, lambda p: p[0] >= lon_min, vertical(lon_min))
        polygon = clip(polygon, lambda p: p[0] <= lon_max, vertical(lon_max))
        polygon = clip(polygon, lambda p: p[1] >= lat_min, horizontal(lat_min))
        polygon = clip(polygon, lambda p: p[1] <= lat_max, horizontal(lat_max))
        return [self._geo_to_px(lon, lat, w, h) for lon, lat in polygon]

    def _draw_country_overlay(self, gc, entry, w, h, fill, outline=None):
        if not entry:
            return
        ring_indices = entry.get("rings_idx", []) or []
        if not ring_indices:
            return
        self._draw_ring_polygons(gc, ring_indices, w, h, fill,
                                  outline or wx.Colour(255, 220, 120, 220))

    def _draw_world_labels(self, gc, w, h):
        if not hasattr(self, '_label_cache') or self._label_cache_size != (w, h):
            char_w = self._LABEL_SIZE * 0.7
            char_h = self._LABEL_SIZE + 3
            self._label_cache = [
                (country["iso2"],
                 int(self._geo_to_px(country["centroid_lon"], country["centroid_lat"],
                                     w, h)[0] - len(country["iso2"]) * char_w / 2),
                 int(self._geo_to_px(country["centroid_lon"], country["centroid_lat"],
                                     w, h)[1] - char_h / 2))
                for country in _GEO_COUNTRIES
            ]
            self._label_cache_size = (w, h)
        for iso2, lx, ly in self._label_cache:
            self._draw_label(gc, iso2, lx, ly, self._LABEL_SIZE)

    def _draw_mode_overlay(self, gc, w, h):
        mode = self._map_display_mode()
        if mode == "country":
            self._draw_country_mode(gc, w, h)
        self._draw_flash_overlay(gc, w, h)

    def _draw_flash_overlay(self, gc, w, h):
        if self._flash_name:
            # Subdue the ordinary map so the queried country is unambiguous.
            gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(2, 8, 18, 175))))
            gc.SetPen(wx.NullPen)
            gc.DrawRectangle(0, 0, w, h)
        if self._flash_rings:
            self._draw_ring_polygons(gc, self._flash_rings, w, h,
                                      self._COL_FLASH_FILL,
                                      wx.Colour(255, 220, 120, 240))
        if self._flash_name:
            self._draw_label_at_geo(
                gc, self._flash_name, self._flash_cx, self._flash_cy, w, h,
                self._FLASH_SIZE)
        self._draw_country_shape_card(gc, w, h)

    def _draw_country_shape_card(self, gc, w, h):
        """Draw a large, data-derived silhouette of the F8 country."""
        if not self._flash_name or not self._flash_rings or w < 360 or h < 240:
            return
        rings = []
        for ring_idx in self._flash_rings:
            if 0 <= ring_idx < len(_GEO_RINGS):
                adjusted = []
                for lon, lat in _GEO_RINGS[ring_idx]:
                    while lon - self._flash_cx > 180:
                        lon -= 360
                    while lon - self._flash_cx < -180:
                        lon += 360
                    adjusted.append((lon, lat))
                if adjusted:
                    rings.append(adjusted)
        if not rings:
            return
        all_points = [point for ring in rings for point in ring]
        lon_min = min(point[0] for point in all_points)
        lon_max = max(point[0] for point in all_points)
        lat_min = min(point[1] for point in all_points)
        lat_max = max(point[1] for point in all_points)
        geo_w = max(.01, lon_max - lon_min)
        geo_h = max(.01, lat_max - lat_min)
        pad = 14
        card_w = max(260, min(500, int(w * .42)))
        card_h = max(220, min(430, int(h * .58)))
        x = w - card_w - pad
        y = pad
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(5, 16, 30, 242))))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(0, 235, 255)).Width(2)))
        gc.DrawRoundedRectangle(x, y, card_w, card_h, 9)
        title_h = 42
        draw_w, draw_h = card_w - 34, card_h - title_h - 26
        scale = min(draw_w / geo_w, draw_h / geo_h)
        shape_w, shape_h = geo_w * scale, geo_h * scale
        left = x + (card_w - shape_w) / 2
        top = y + title_h + (draw_h - shape_h) / 2
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(78, 190, 112))))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(255, 220, 90)).Width(2)))
        for ring in rings:
            points = [
                (left + (lon - lon_min) * scale,
                 top + (lat_max - lat) * scale)
                for lon, lat in ring
            ]
            if len(points) < 3:
                continue
            path = gc.CreatePath()
            path.MoveToPoint(*points[0])
            for point in points[1:]:
                path.AddLineToPoint(*point)
            path.CloseSubpath()
            gc.DrawPath(path)
        title_font = wx.Font(15, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
                             wx.FONTWEIGHT_BOLD)
        gc.SetFont(gc.CreateFont(title_font, wx.Colour(255, 255, 255)))
        gc.DrawText(self._flash_name[:48], x + 14, y + 12)

    def _draw_country_mode(self, gc, w, h):
        owner = self._owner_ref()
        if not owner:
            self._draw_world_labels(gc, w, h)
            return
        country_name = str(getattr(owner, "last_country_found", "") or "").strip()
        if not country_name or country_name == "Open Water":
            self._draw_world_labels(gc, w, h)
            return

        entry = self._country_entry_for(country_name)
        if entry:
            self._draw_country_overlay(
                gc, entry, w, h,
                wx.Colour(255, 200, 0, 72),
                wx.Colour(255, 220, 120, 220))
            self._draw_label_at_geo(
                gc, entry["name"], entry["centroid_lon"], entry["centroid_lat"],
                w, h, 22)
        else:
            self._draw_label_at_geo(gc, country_name, owner.lon, owner.lat, w, h, 22)

        location_text = str(getattr(owner, "last_location_str", "") or "").strip()
        if location_text and location_text.lower() not in {
                country_name.lower(), (entry["name"].lower() if entry else "")}:
            self._draw_label_at_geo(gc, location_text, owner.lon, owner.lat, w, h, 18, dy=18)

        state_name = str(getattr(owner, "last_state_found", "") or "").strip()
        country_key = self._country_city_index_key(country_name)
        country_indices = list(getattr(owner, "_city_country_index", {}).get(country_key, [])) if country_key else []

        if state_name and country_indices and self._visual_zoom_factor() == 1:
            state_indices = [idx for idx in country_indices
                             if str(owner._city_admins[idx]).strip() == state_name]
            if state_indices:
                state_anchor = max(state_indices, key=lambda idx: owner._city_pops[idx])
                self._draw_label_at_geo(
                    gc, state_name, owner._city_lons[state_anchor],
                    owner._city_lats[state_anchor], w, h, 18, dy=-18)

        if country_indices and self._visual_zoom_factor() == 1:
            # One label per admin region keeps the country view readable.
            groups = {}
            for idx in country_indices:
                city = str(owner._city_names[idx]).strip()
                admin = str(owner._city_admins[idx]).strip()
                group_key = admin or city or f"row-{idx}"
                pop = owner._city_pops[idx]
                current = groups.get(group_key)
                if current is None or pop > current[0]:
                    groups[group_key] = (pop, idx)
            seen = set()
            draw_items = sorted(
                groups.values(),
                key=lambda item: (-item[0], str(owner._city_names[item[1]]).lower())
            )
            for pop, idx in draw_items[:8]:
                city = str(owner._city_names[idx]).strip()
                admin = str(owner._city_admins[idx]).strip()
                if not city:
                    continue
                text = city if not admin or admin == city else f"{city}, {admin}"
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                size = 13 if pop < 1_000_000 else 14
                if state_name and admin == state_name:
                    size = max(size, 15)
                self._draw_label_at_geo(gc, text, owner._city_lons[idx],
                                        owner._city_lats[idx], w, h, size)
        elif country_indices:
            zoom = self._visual_zoom_factor()
            thresholds = {
                2: 500_000, 4: 100_000, 8: 20_000,
                16: 5_000, 32: 1_000, 64: 0, 128: 0,
            }
            minimum_pop = thresholds.get(zoom, 0)
            lon_min, lon_max, lat_min, lat_max = self._view_bounds(w, h)

            def visible(idx):
                lat = owner._city_lats[idx]
                lon = owner._city_lons[idx]
                centre = (lon_min + lon_max) / 2
                while lon - centre > 180: lon -= 360
                while lon - centre < -180: lon += 360
                return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max

            if zoom >= 8:
                # At close zoom, query only the 0.1-degree cells intersecting
                # the viewport instead of rescanning every locality in a large
                # country such as Australia.
                nearby_indices = []
                gy_min = int(math.floor(lat_min * 10))
                gy_max = int(math.floor(lat_max * 10))
                gx_min = int(math.floor(lon_min * 10))
                gx_max = int(math.floor(lon_max * 10))
                for gy in range(gy_min, gy_max + 1):
                    for raw_gx in range(gx_min, gx_max + 1):
                        gx = ((raw_gx + 1800) % 3600) - 1800
                        nearby_indices.extend(owner._city_grid.get((gy, gx), ()))
                country_lower = country_key.lower()
                candidate_source = [
                    idx for idx in nearby_indices
                    if str(owner._city_regions[idx][1]).strip().lower() == country_lower
                ]
            else:
                candidate_source = country_indices
            candidates = [idx for idx in candidate_source
                          if owner._city_pops[idx] >= minimum_pop and visible(idx)]
            candidates = sorted(set(candidates),
                                key=lambda idx: -owner._city_pops[idx])[:20]
            occupied = []
            for idx in candidates:
                city = str(owner._city_names[idx]).strip()
                if not city:
                    continue
                px, py = self._geo_to_px(owner._city_lons[idx],
                                         owner._city_lats[idx], w, h)
                label_w = max(50, len(city) * 8)
                if any(abs(px - ox) < (label_w + ow) / 2 and abs(py - oy) < 24
                       for ox, oy, ow in occupied):
                    continue
                occupied.append((px, py, label_w))
                self._draw_label_at_geo(gc, city, owner._city_lons[idx],
                                        owner._city_lats[idx], w, h, 14)

    def _draw_current_location_callout(self, gc, w, h, px, py):
        """Make the actual cursor unmistakable in Country/Visual Assist views."""
        if getattr(self, "_explore_path_points", None):
            return
        if (self._map_display_mode() != "country"
                and not self._classroom_mode_active()):
            return
        owner = self._owner_ref()
        if not owner:
            return
        city = str(getattr(owner, "last_city_found", "") or "").strip()
        state = str(getattr(owner, "last_state_found", "") or "").strip()
        country = str(getattr(owner, "last_country_found", "") or "").strip()
        place = city or str(getattr(owner, "last_location_str", "") or "").strip()
        parts = []
        for value in (place, state, country):
            if value and value.lower() not in {item.lower() for item in parts}:
                parts.append(value)
        text = ", ".join(parts) or "Unknown"
        if len(text) > 80:
            text = text[:77].rstrip() + "..."
        font_size = max(12, min(17, int(h / 34)))
        font = wx.Font(font_size, wx.FONTFAMILY_SWISS,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(gc.CreateFont(font, wx.Colour(255, 255, 255)))
        text_w, text_h = gc.GetTextExtent(text)
        pad = 7
        box_w = min(w - 8, int(text_w + pad * 2))
        box_h = int(text_h + pad * 2)
        box_x = px + 22 if px + 22 + box_w <= w - 4 else px - 22 - box_w
        box_x = max(4, min(box_x, w - box_w - 4))
        box_y = max(4, min(py - box_h - 18, h - box_h - 4))
        line_x = box_x if box_x > px else box_x + box_w
        line_y = box_y + box_h
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(0, 235, 255)).Width(3)))
        gc.StrokeLine(px, py, line_x, line_y)
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(5, 16, 30, 240))))
        gc.DrawRoundedRectangle(box_x, box_y, box_w, box_h, 6)
        gc.DrawText(text, box_x + pad, box_y + pad)

    def _paint_world_bg(self, gc, w, h, include_labels=True):
        # Ocean
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_OCEAN)))
        gc.SetPen(wx.NullPen)
        gc.DrawRectangle(0, 0, w, h)
        # Grid
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_GRID).Width(1)))
        for glon in range(-180, 181, 30):
            x1, y1 = self._geo_to_px(glon,  90, w, h)
            x2, y2 = self._geo_to_px(glon, -90, w, h)
            gc.StrokeLine(x1, y1, x2, y2)
        for glat in range(-90, 91, 30):
            x1, y1 = self._geo_to_px(-180, glat, w, h)
            x2, y2 = self._geo_to_px( 180, glat, w, h)
            gc.StrokeLine(x1, y1, x2, y2)
        # Land polygons
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_BORDER).Width(1)))
        for ring in _GEO_RINGS:
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_LAND)))
            pts = self._clipped_ring_pixels(ring, w, h)
            if len(pts) < 3:
                continue
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)
        if include_labels:
            self._draw_world_labels(gc, w, h)

    def _paint_street(self, gc, w, h):
        span = 0.02
        lon_min = self.lon - span;  lon_max = self.lon + span
        lat_min = self.lat - span;  lat_max = self.lat + span
        kw = dict(lon_min=lon_min, lon_max=lon_max,
                  lat_min=lat_min, lat_max=lat_max)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_OCEAN)))
        gc.SetPen(wx.NullPen)
        gc.DrawRectangle(0, 0, w, h)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_LAND)))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_BORDER).Width(1)))
        for ring in _GEO_RINGS:
            pts = [self._geo_to_px(lo, la, w, h, **kw) for lo, la in ring]
            if len(pts) < 3:
                continue
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_GRID).Width(1)))
        step = 0.005
        glon = math.floor(lon_min / step) * step
        while glon <= lon_max:
            x1, y1 = self._geo_to_px(glon, lat_max, w, h, **kw)
            x2, y2 = self._geo_to_px(glon, lat_min, w, h, **kw)
            gc.StrokeLine(x1, y1, x2, y2)
            glon += step
        glat = math.floor(lat_min / step) * step
        while glat <= lat_max:
            x1, y1 = self._geo_to_px(lon_min, glat, w, h, **kw)
            x2, y2 = self._geo_to_px(lon_max, glat, w, h, **kw)
            gc.StrokeLine(x1, y1, x2, y2)
            glat += step
        self._draw_classroom_trail(gc, w, h, kw)
        self._draw_classroom_destination(gc, w, h, kw)
        px, py = self._geo_to_px(self.lon, self.lat, w, h, **kw)
        marker_size = 17 if self._classroom_mode_active() else 12
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_RING)))
        gc.SetPen(wx.NullPen)
        gc.DrawEllipse(px - marker_size, py - marker_size,
                       marker_size * 2, marker_size * 2)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_DOT)))
        dot_size = max(7, marker_size - 5)
        gc.DrawEllipse(px - dot_size, py - dot_size, dot_size * 2, dot_size * 2)
        if self.street_label:
            gc.SetFont(gc.CreateFont(
                wx.Font(10, wx.FONTFAMILY_DEFAULT,
                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
                wx.Colour(220, 220, 220)))
            gc.DrawText("STREET  " + self.street_label, 8, 8)
        self._draw_classroom_hud(gc, w, h)
        self._draw_visual_assist_caption(gc, w, h)
