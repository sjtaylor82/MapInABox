"""Sound playback for Map in a Box."""

import os
import threading

import numpy as np
import pygame

from app_paths import RESOURCE_DIR, USER_DIR
from logging_utils import miab_log

SOUNDS_DIR = os.path.join(RESOURCE_DIR, "sounds")
COUNTRY_DIR = os.path.join(SOUNDS_DIR, "countries")
REGION_DIR = os.path.join(SOUNDS_DIR, "regions")
USER_SOUNDS_DIR = os.path.join(USER_DIR, "sounds")
USER_COUNTRY_DIR = os.path.join(USER_SOUNDS_DIR, "countries")
USER_REGION_DIR = os.path.join(USER_SOUNDS_DIR, "regions")

COUNTRY_ALIASES = {
    "United States":   "United States of America",
    "USA":             "United States of America",
    "United Kingdom":  "United Kingdom",
    "UK":              "United Kingdom",
    "UAE":             "United Arab Emirates",
    "United Arab Emirates": "United Arab Emirates",
    "Russia":          "Russian Federation",
    "South Korea":     "Republic of Korea",
    "North Korea":     "Democratic People's Republic of Korea",
    "Czech Republic":  "Czechia",
    "Central African Rep.": "Central African Republic",
    "Central African Rep":  "Central African Republic",
    "Ivory Coast":     "Cote d'Ivoire",
    "Syria":           "Syrian Arab Republic",
    "Iran":            "Iran",
    "Bolivia":         "Bolivia",
    "Venezuela":       "Venezuela",
    "Tanzania":        "Tanzania",
    "Moldova":         "Moldova",
    # Australian external territories
    "Norfolk Island":              "Australia",
    "Christmas Island":            "Australia",
    "Cocos (Keeling) Islands":     "Australia",
    "Cocos Islands":               "Australia",
    "Heard Island":                "Australia",
    "Heard Island and McDonald Islands": "Australia",
    "Ashmore and Cartier Islands": "Australia",
    "Coral Sea Islands":           "Australia",
    # NZ territories
    "Niue":            "New Zealand",
    "Tokelau":         "New Zealand",
    "Cook Islands":    "New Zealand",
    # UK territories
    "Falkland Islands":          "United Kingdom",
    "Gibraltar":                 "United Kingdom",
    "Bermuda":                   "United Kingdom",
    "Cayman Islands":            "United Kingdom",
    "British Virgin Islands":    "United Kingdom",
    "Turks and Caicos Islands":  "United Kingdom",
    "Saint Helena":              "United Kingdom",
    "Pitcairn":                  "United Kingdom",
    # French territories
    "French Polynesia":          "France",
    "New Caledonia":             "France",
    "Reunion":                   "France",
    "Martinique":                "France",
    "Guadeloupe":                "France",
    "Mayotte":                   "France",
    "French Guiana":             "France",
    "Saint Pierre and Miquelon": "France",
    "Wallis and Futuna":         "France",
    # US territories
    "Puerto Rico":               "United States of America",
    "Guam":                      "United States of America",
    "U.S. Virgin Islands":       "United States of America",
    "American Samoa":            "United States of America",
    "Northern Mariana Islands":  "United States of America",
}


def _safe_stem(name):
    return (name.lower()
               .replace(" ", "_")
               .replace("'", "")
               .replace("/", "_")
               .replace("&", "and")
               .replace(",", "")
               .replace(".", ""))


class SoundEngine:
    # Volume step size and limits for Shift+F3/F4
    _VOL_STEP = 0.1
    _VOL_MIN  = 0.0
    _VOL_MAX  = 1.0

    def __init__(self):
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        pygame.mixer.set_num_channels(16)
        self._ch = pygame.mixer.Channel(0)
        self._master_volume = 0.7
        self._apply_volume()
        self._current = None

    def _apply_volume(self):
        """Set the master volume on every pygame mixer channel."""
        n = pygame.mixer.get_num_channels()
        for i in range(n):
            pygame.mixer.Channel(i).set_volume(self._master_volume)

    def volume_down(self) -> str:
        """Decrease master volume by 10%. Returns announcement string."""
        self._master_volume = max(self._VOL_MIN,
                                  round(self._master_volume - self._VOL_STEP, 2))
        self._apply_volume()
        pct = int(self._master_volume * 100)
        return f"Volume {pct}%." if pct > 0 else "Volume muted."

    def volume_up(self) -> str:
        """Increase master volume by 10%. Returns announcement string."""
        self._master_volume = min(self._VOL_MAX,
                                  round(self._master_volume + self._VOL_STEP, 2))
        self._apply_volume()
        return f"Volume {int(self._master_volume * 100)}%."

    # Maps canonical country name → existing sound stem when no direct file exists.
    # Specific country files take priority; region names are last resort.
    _SOUND_FALLBACKS = {
        # Europe
        "Albania":                  "europe",
        "Algeria":                  "africa",
        "Armenia":                  "europe",
        "Azerbaijan":               "europe",
        "Bahrain":                  "middle_east",
        "Belarus":                  "europe",
        "Bosnia and Herzegovina":   "europe",
        "Central African Republic": "africa",
        "Denmark":                  "europe",
        "Finland":                  "europe",
        "Gabon":                    "africa",
        "Georgia":                  "europe",
        "Guinea":                   "africa",
        "Guyana":                   "south_america",
        "Honduras":                 "north_america",
        "Iraq":                     "middle_east",
        "Ivory Coast":              "africa",
        "Kazakhstan":               "asia",
        "Liberia":                  "africa",
        "Libya":                    "africa",
        "Malawi":                   "africa",
        "Mauritania":               "africa",
        "Moldova":                  "europe",
        "Morocco":                  "africa",
        "Mozambique":               "africa",
        "Namibia":                  "africa",
        "North Macedonia":          "europe",
        "Paraguay":                 "south_america",
        "Ecuador":                  "south_america",
        "Poland":                   "europe",
        "Qatar":                    "middle_east",
        "Romania":                  "europe",
        "Rwanda":                   "africa",
        "Senegal":                  "gambia",
        "Slovakia":                 "europe",
        "Somalia":                  "africa",
        "South Sudan":              "africa",
        "Sudan":                    "africa",
        "Suriname":                 "south_america",
        "Venezuela":                "south_america",
        "Angola":                   "africa",
        "Eritrea":                  "africa",
        "Ethiopia":                 "africa",
        "Cote d'Ivoire":            "africa",
        # Aliases already handled by COUNTRY_ALIASES but add region safety net
        "Democratic People's Republic of Korea": "asia",
        "Republic of Korea":        "republic_of_korea",
        "Democratic Republic of the Congo": "congo_(kinshasa)",
        "Republic of the Congo":    "congo_(brazzaville)",
        "Russian Federation":       "russian_federation",
        "Syrian Arab Republic":     "syrian_arab_republic",
        "United States of America": "united_states_of_america",
    }

    def play_location_sound(self, country_name, continent=""):
        canonical = COUNTRY_ALIASES.get(country_name, country_name)

        if canonical == self._current:
            return

        orig_stem = _safe_stem(country_name)
        can_stem  = _safe_stem(canonical)

        def _candidates_for(country_dir, region_dir):
            paths = []
            # Original country name takes priority (for example,
            # new_caledonia.ogg over its canonical parent, france.ogg).
            for ext in ("ogg", "mp3"):
                paths.append(os.path.join(country_dir, f"{orig_stem}.{ext}"))
            for ext in ("ogg", "mp3"):
                paths.append(os.path.join(region_dir, f"{orig_stem}.{ext}"))

            if can_stem != orig_stem:
                for ext in ("ogg", "mp3"):
                    paths.append(os.path.join(country_dir, f"{can_stem}.{ext}"))
                for ext in ("ogg", "mp3"):
                    paths.append(os.path.join(region_dir, f"{can_stem}.{ext}"))

            fallback = self._SOUND_FALLBACKS.get(canonical)
            if fallback:
                fb_stem = _safe_stem(fallback)
                for ext in ("ogg", "mp3"):
                    for directory in (country_dir, region_dir):
                        paths.append(os.path.join(directory, f"{fb_stem}.{ext}"))

            if continent:
                cont_stem = _safe_stem(continent)
                for ext in ("ogg", "mp3"):
                    paths.append(os.path.join(region_dir, f"{cont_stem}.{ext}"))
            return paths

        # User-owned overrides always win. The bundled tree remains disposable
        # so installers can refresh it without deleting custom sounds.
        candidates = _candidates_for(USER_COUNTRY_DIR, USER_REGION_DIR)
        candidates.extend(_candidates_for(COUNTRY_DIR, REGION_DIR))

        for path in candidates:
            if os.path.exists(path):
                self._current = canonical
                self.play_file(path, loops=-1)
                return

        # No sound found — stop current sound
        self._current = canonical
        self._ch.stop()

    def play_file(self, path, loops=0):
        """Play a WAV file once (or looped if loops=-1)."""
        try:
            sound = pygame.mixer.Sound(path)
            self._ch.play(sound, loops=loops)
        except Exception as e:
            miab_log("errors", f"[SoundEngine] Cannot play {path}: {e}", getattr(self, "settings", None))

    def stop(self):
        """Stop current playback."""
        self._ch.stop()
        self._current = None

    def play_poi_tone(self, side: str):
        """Short directional beep: 'left', 'right', or 'both'."""
        def _gen():
            sr   = 44100
            t    = np.linspace(0, 0.08, int(sr * 0.08), False)
            wave = np.sin(2 * np.pi * 1760.0 * t)
            fade = int(sr * 0.02)
            wave[:fade]  *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            wave = wave * 0.6 * 32767
            if side == "left":
                l, r = wave, np.zeros_like(wave)
            elif side == "right":
                l, r = np.zeros_like(wave), wave
            else:  # both
                l, r = wave * 0.7, wave * 0.7
            stereo = np.ascontiguousarray(
                np.stack([l, r], axis=-1), dtype=np.int16)
            snd = pygame.sndarray.make_sound(stereo)
            for idx in range(1, pygame.mixer.get_num_channels()):
                ch = pygame.mixer.Channel(idx)
                if not ch.get_busy():
                    ch.play(snd)
                    return
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()

    def play_shared_address_tone(self):
        """Very short centred cue for multiple POIs at one address."""
        def _gen():
            sr = 44100
            duration = 0.055
            t = np.linspace(0, duration, int(sr * duration), False)
            wave = np.sin(2 * np.pi * 950.0 * t)
            fade = max(1, int(sr * 0.008))
            wave[:fade] *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            stereo = np.ascontiguousarray(
                np.stack((wave, wave), axis=-1) * 0.35 * 32767,
                dtype=np.int16,
            )
            snd = pygame.sndarray.make_sound(stereo)
            for idx in range(1, pygame.mixer.get_num_channels()):
                channel = pygame.mixer.Channel(idx)
                if not channel.get_busy():
                    channel.play(snd)
                    return
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()

    def play_barrier_tone(self):
        """Play a low double pulse when local-map movement crosses a barrier."""
        def _gen():
            sr = 44100
            pulse_length = 0.075
            gap_length = 0.055
            pulse_t = np.linspace(0, pulse_length, int(sr * pulse_length), False)
            pulse = np.sin(2 * np.pi * 145.0 * pulse_t)
            fade = max(1, int(sr * 0.015))
            pulse[:fade] *= np.linspace(0, 1, fade)
            pulse[-fade:] *= np.linspace(1, 0, fade)
            gap = np.zeros(int(sr * gap_length))
            wave = np.concatenate((pulse, gap, pulse)) * 0.55 * 32767
            stereo = np.ascontiguousarray(
                np.stack((wave, wave), axis=-1), dtype=np.int16)
            snd = pygame.sndarray.make_sound(stereo)
            for idx in range(1, pygame.mixer.get_num_channels()):
                channel = pygame.mixer.Channel(idx)
                if not channel.get_busy():
                    channel.play(snd)
                    return
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()

    def play_spatial_tone(self, lat, lon, bounds=None):
        """Pitch-panned navigation beep on channels 1+."""
        if bounds:
            try:
                min_lat, max_lat, min_lon, max_lon = bounds
                if (min_lon > 180.0 or max_lon > 180.0) and lon < 0.0:
                    lon += 360.0
                lat_span = max_lat - min_lat
                lon_span = max_lon - min_lon
                if lat_span > 0 and lon_span > 0:
                    lat = ((lat - min_lat) / lat_span) * 180.0 - 90.0
                    lon = ((lon - min_lon) / lon_span) * 360.0 - 180.0
            except Exception:
                pass
        def _gen():
            freq   = max(220.0, min(880.0, 440.0 + (lat / 90.0) * 440.0))
            pan    = max(-1.0,  min(1.0,   lon / 180.0))
            sr     = 44100
            t      = np.linspace(0, 0.15, int(sr * 0.15), False)
            wave   = np.sin(2 * np.pi * freq * t)
            fade   = int(sr * 0.04)
            wave[:fade]  *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            left   = wave * (1.0 - pan) / 2.0
            right  = wave * (1.0 + pan) / 2.0
            stereo = np.ascontiguousarray(
                np.stack([left, right], axis=-1) * 0.5 * 32767,
                dtype=np.int16
            )
            snd = pygame.sndarray.make_sound(stereo)
            for idx in range(1, pygame.mixer.get_num_channels()):
                ch = pygame.mixer.Channel(idx)
                if not ch.get_busy():
                    ch.play(snd)
                    return
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()
