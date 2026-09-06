"""Street-level and satellite imagery UI for Map in a Box."""

import io
import os
import threading
import webbrowser

from PIL import Image
import wx

from app_paths import CACHE_DIR
from logging_utils import miab_log

try:
    from satellite import lookup_satellite_description
except ImportError:
    lookup_satellite_description = None

try:
    from streetview import lookup_streetview_description
except ImportError:
    lookup_streetview_description = None


class ImageryMixin:
    def _poi_lat_lon_if_focused(self) -> tuple[float, float]:
        """Return the selected POI's lat/lon when the POI list is open and focused,
        otherwise return the current cursor position."""
        poi_list_open = bool(getattr(self, '_poi_list', []))
        listbox = getattr(self, 'listbox', None)
        is_listbox_focused = listbox is not None and listbox.HasFocus()
        if poi_list_open and is_listbox_focused:
            self._sync_poi_selection_from_listbox()
            idx = getattr(self, '_poi_index', -1)
            pois = getattr(self, '_poi_list', [])
            if 0 <= idx < len(pois):
                poi = pois[idx]
                plat = poi.get('lat')
                plon = poi.get('lon')
                if plat is not None and plon is not None:
                    return float(plat), float(plon)
        return self.lat, self.lon

    def _streetview_at_location(self, lat: float, lon: float):
        """Fetch and display Street View imagery + description at (lat, lon).
        Falls back to satellite if no Street View coverage exists, or an
        open street-level viewer if Google isn't configured."""
        google_key = self.settings.get("google_api_key", "").strip()
        visual_source = self.settings.get("visual_mapping_source", "auto")
        if visual_source == "auto":
            visual_source = "google" if google_key else "mapillary"
        if visual_source == "mapillary":
            self._announce_transient_then_return(
                "Opening Mapillary street-level imagery in your browser.")
            wx.CallLater(2000, self._open_mapillary_view, lat, lon)
            return
        if not google_key:
            # Defensive fallback for settings files edited outside the app.
            self._announce_transient_then_return(
                "Google Street View needs an API key. Opening Mapillary instead.")
            wx.CallLater(2000, self._open_mapillary_view, lat, lon)
            return
        if not lookup_streetview_description:
            self._announce_transient_then_return("Street View module not available.")
            return

        self._status_update("Fetching Street View...", force=True)

        def fetch_and_display():
            try:
                # Pass current travel heading so both images have meaningful
                # direction labels.  _walk_heading is set in walking mode;
                # street mode uses _road_heading if available, else None (→ N/S).
                heading = None
                if getattr(self, '_walking_mode', False):
                    heading = getattr(self, '_walk_heading', None)

                result = lookup_streetview_description(
                    lat, lon,
                    google_api_key=google_key,
                    mistral_client=self._mistral,
                    street_heading=heading,
                    cache_path=os.path.join(CACHE_DIR, "streetview_cache.json"),
                )

                if not result:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No Street View coverage here. Showing satellite instead.")
                    wx.CallAfter(self._schedule_satellite_view, lat, lon)
                    return

                image_bytes_list, description = result
                wx.CallAfter(
                    self._show_image_dialog,
                    "Street View", image_bytes_list, description, lat, lon,
                    dialog_size=(920, 700), single_img_size=(640, 480),
                    multi_img_size=(420, 480), text_min_size=(880, 130))

            except Exception as e:
                miab_log("error", f"Street View lookup failed: {e}", self.settings)
                wx.CallAfter(
                    self._announce_transient_then_return, f"Error: {str(e)[:50]}")

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _open_mapillary_view(self, lat: float, lon: float) -> None:
        """Open an open street-level viewer as a fallback."""
        try:
            import webbrowser
            url = f"https://www.mapillary.com/app/?lat={lat:.6f}&lng={lon:.6f}&z=17"
            webbrowser.open(url)
        except Exception as exc:
            self._announce_transient_then_return(
                f"Could not open open street-level viewer: {exc}")

    def _schedule_satellite_view(self, lat: float, lon: float, delay_ms: int = 2000) -> None:
        """Queue satellite view on the UI thread after a short delay."""
        wx.CallLater(delay_ms, self._satellite_view_at_location, lat, lon)

    def _satellite_view_at_location(self, lat: float, lon: float):
        """Fetch and display satellite image + description at location."""
        google_key = self.settings.get("google_api_key", "").strip()
        if not google_key:
            self._announce_transient_then_return(
                "Satellite view uses Google imagery and needs a Google API key.")
            return
        self._status_update("Fetching satellite image...", force=True)

        def fetch_and_display():
            try:
                if not lookup_satellite_description:
                    wx.CallAfter(self._announce_transient_then_return, "Satellite module not available.")
                    return
                result = lookup_satellite_description(
                    lat, lon, zoom=15,
                    google_api_key=self.settings.get("google_api_key", ""),
                    mistral_client=self._mistral,
                    cache_path=os.path.join(CACHE_DIR, "satellite_cache.json")
                )

                if not result:
                    wx.CallAfter(self._announce_transient_then_return, "Satellite image unavailable at this location.")
                    return

                image_bytes, description = result
                wx.CallAfter(
                    self._show_image_dialog,
                    "Satellite View", [image_bytes], description, lat, lon,
                    return_focus=True)

            except Exception as e:
                miab_log("error", f"Satellite lookup failed: {e}", self.settings)
                wx.CallAfter(self._announce_transient_then_return, f"Error: {str(e)[:50]}")

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _show_image_dialog(self, title, image_bytes_list, description, lat, lon,
                            dialog_size=(900, 700), single_img_size=(600, 600),
                            multi_img_size=(420, 480), text_min_size=(850, 150),
                            return_focus=False):
        """Display one or more images with a description in a modal dialog.
        Used for both Street View (may pass 1-2 images) and Satellite View
        (always 1 image)."""
        dlg = wx.Dialog(
            self, title=f"{title} ({lat:.4f}, {lon:.4f})",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=dialog_size,
        )
        vs = wx.BoxSizer(wx.VERTICAL)

        multi = len(image_bytes_list) > 1
        img_w, img_h = multi_img_size if multi else single_img_size

        image_panel = wx.ScrolledWindow(dlg, style=wx.HSCROLL | wx.VSCROLL)
        image_panel.SetScrollRate(20, 20)
        img_sizer = wx.BoxSizer(wx.HORIZONTAL)
        source_images = []
        image_controls = []
        for img_bytes in image_bytes_list:
            try:
                pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                source_images.append(pil.copy())
                preview = pil.copy()
                preview.thumbnail((img_w, img_h), Image.Resampling.LANCZOS)
                wx_img = wx.Image(preview.width, preview.height)
                wx_img.SetData(preview.tobytes())
                bmp = wx.StaticBitmap(image_panel, bitmap=wx.Bitmap(wx_img))
                image_controls.append(bmp)
                img_sizer.Add(bmp, 0, wx.ALL, 6)
            except Exception as e:
                miab_log("errors", f"[UI] {title} image display failed: {e}", getattr(self, "settings", None))
        image_panel.SetSizer(img_sizer)
        image_panel.SetMinSize((-1, min(img_h + 20, 500)))
        vs.Add(image_panel, 1, wx.ALL | wx.EXPAND, 4)

        zoom = [1.0]
        zoom_label = wx.StaticText(dlg, label="Zoom: 100%")

        def apply_image_zoom(factor):
            zoom[0] = max(0.5, min(3.0, factor))
            for source, control in zip(source_images, image_controls):
                base = source.copy()
                base.thumbnail((img_w, img_h), Image.Resampling.LANCZOS)
                width = max(1, round(base.width * zoom[0]))
                height = max(1, round(base.height * zoom[0]))
                scaled = base.resize((width, height), Image.Resampling.LANCZOS)
                wx_img = wx.Image(width, height)
                wx_img.SetData(scaled.tobytes())
                control.SetBitmap(wx.Bitmap(wx_img))
            zoom_label.SetLabel(f"Zoom: {round(zoom[0] * 100):d}%")
            img_sizer.Layout()
            image_panel.FitInside()
            image_panel.Layout()

        zoom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        zoom_out = wx.Button(dlg, label="Zoom out")
        zoom_reset = wx.Button(dlg, label="Reset zoom")
        zoom_in = wx.Button(dlg, label="Zoom in")
        zoom_out.Bind(wx.EVT_BUTTON, lambda e: apply_image_zoom(zoom[0] - 0.25))
        zoom_reset.Bind(wx.EVT_BUTTON, lambda e: apply_image_zoom(1.0))
        zoom_in.Bind(wx.EVT_BUTTON, lambda e: apply_image_zoom(zoom[0] + 0.25))
        for control in (zoom_out, zoom_reset, zoom_in, zoom_label):
            zoom_sizer.Add(control, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        vs.Add(zoom_sizer, 0, wx.CENTER, 2)

        txt = wx.TextCtrl(
            dlg, value=description,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        txt.SetMinSize(text_min_size)
        vs.Add(txt, 0, wx.ALL | wx.EXPAND, 10)

        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        dlg.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        vs.Add(btn, 0, wx.ALL | wx.CENTER, 10)

        dlg.SetSizer(vs)
        dlg.ShowModal()
        dlg.Destroy()

        if return_focus:
            self.listbox.SetFocus()
