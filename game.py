"""game.py — Country Discovery Challenge for Map in a Box.

The player is given a random country name and must navigate the world map
to find it within 5 minutes.  Audio feedback:

  Map tone       — normal world-map spatial tone for current position.
  Sonar pulse    — subtle centred pulse; pitch rises as you get closer.
  Heartbeat      — low pulse that speeds up as the timer runs down,
                   adding urgency without saying anything.
  Fanfare        — on success.
  Buzzer         — on timeout.

Usage (from MapNavigator):
    self._game = ChallengeGame(self.update_ui, self._lookup)
    self._game.start(df, lat, lon)   # df = worldcities DataFrame
    self._game.stop()
    self._game.on_move(lat, lon)     # call whenever lat/lon changes
    self._game.repeat_target()       # re-announce the target country
    country = self._game.target_country  # for win-check in _lookup
"""

import math
import threading
import time

import numpy as np

# ---------------------------------------------------------------------------
# Countries large enough to realistically find on a world map.
# Anything smaller than ~50,000 km² or hard to navigate to is excluded.
# ---------------------------------------------------------------------------

PLAYABLE_COUNTRIES: frozenset = frozenset({
    "Afghanistan", "Albania", "Algeria", "Angola", "Argentina", "Armenia",
    "Australia", "Austria", "Azerbaijan", "Belarus", "Belgium", "Benin",
    "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Bulgaria",
    "Burkina Faso", "Cambodia", "Cameroon", "Canada", "Central African Republic",
    "Chad", "Chile", "China", "Colombia", "Costa Rica", "Croatia", "Cuba",
    "Czech Republic", "DR Congo", "Denmark", "Dominican Republic", "Ecuador",
    "Egypt", "El Salvador", "Eritrea", "Estonia", "Ethiopia", "Finland",
    "France", "Gabon", "Georgia", "Germany", "Ghana", "Greece", "Guatemala",
    "Guinea", "Guyana", "Honduras", "Hungary", "Iceland", "India",
    "Indonesia", "Iran", "Iraq", "Ireland", "Italy", "Ivory Coast", "Japan",
    "Jordan", "Kazakhstan", "Kenya", "Kyrgyzstan", "Laos", "Latvia",
    "Liberia", "Libya", "Lithuania", "Madagascar", "Malawi", "Malaysia",
    "Mali", "Mauritania", "Mexico", "Moldova", "Mongolia", "Morocco",
    "Mozambique", "Myanmar", "Namibia", "Netherlands", "New Zealand",
    "Nicaragua", "Niger", "Nigeria", "North Korea", "North Macedonia",
    "Norway", "Oman", "Pakistan", "Panama", "Papua New Guinea", "Paraguay",
    "Peru", "Philippines", "Poland", "Portugal", "Republic of the Congo",
    "Romania", "Russia", "Saudi Arabia", "Senegal", "Serbia",
    "Sierra Leone", "Slovakia", "Somalia", "South Africa", "South Korea",
    "South Sudan", "Spain", "Sri Lanka", "Sudan", "Suriname", "Sweden",
    "Switzerland", "Tajikistan", "Tanzania", "Thailand", "Tunisia",
    "Turkey", "Turkmenistan", "Uganda", "Ukraine", "United Arab Emirates",
    "United Kingdom", "United States", "Uruguay", "Uzbekistan", "Venezuela",
    "Vietnam", "Yemen", "Zambia", "Zimbabwe",
})


def _country_centroid(df, country: str) -> tuple[float, float]:
    """Return (lat, lon) centroid for *country* from the cities dataframe."""
    rows = df[df["country"] == country]
    if rows.empty:
        return 0.0, 0.0
    return float(rows["lat"].mean()), float(rows["lng"].mean())



# ---------------------------------------------------------------------------
# Audio helpers (pygame / numpy) — all synthesis, no files
# ---------------------------------------------------------------------------

def _play_sound_array(arr: np.ndarray, channel_idx: int = -1) -> None:
    """Play a stereo int16 numpy array on a free pygame channel."""
    import pygame
    arr = np.ascontiguousarray(arr)
    snd = pygame.sndarray.make_sound(arr)
    if channel_idx >= 0:
        pygame.mixer.Channel(channel_idx).play(snd)
    else:
        ch = pygame.mixer.find_channel()
        if ch:
            ch.play(snd)


def _make_beep(freq: float, dur: float, pan_right: float,
               proximity: float = 0.0, sr: int = 44100) -> np.ndarray:
    """Sawtooth beep whose pitch, volume and harshness all rise with proximity.

    Far:   short, quiet, low pitch, soft timbre (few harmonics)
    Close: longer, loud, high pitch, harsh saw timbre (many harmonics)
    """
    t = np.linspace(0, dur, int(sr * dur), False)

    # Sawtooth via additive synthesis — more harmonics = harsher as you get closer
    n_harmonics = int(3 + proximity * 7)   # 3 far → 10 close
    wave = np.zeros_like(t)
    for n in range(1, n_harmonics + 1):
        wave += (1.0 / n) * np.sin(2 * np.pi * freq * n * t)

    # Amplitude envelope
    fade_in  = int(sr * 0.01)
    fade_out = int(sr * dur * 0.4)
    env = np.ones(len(t))
    env[:fade_in]   *= np.linspace(0, 1, fade_in)
    env[-fade_out:] *= np.linspace(1, 0, fade_out)
    wave *= env

    # Volume: audible when far, loud when close (6dB headroom for screen reader)
    amplitude = 4000 + proximity * 9000

    pan_left = 1.0 - pan_right
    stereo = np.vstack([wave * pan_left, wave * pan_right]).T
    peak = np.max(np.abs(stereo)) or 1.0
    return (stereo * (amplitude / peak)).astype(np.int16)


def _make_sonar_pulse(proximity: float = 0.0, sr: int = 44100) -> np.ndarray:
    """Subtle centred pulse; pitch rises as the target gets closer."""
    dur = 0.20
    t = np.linspace(0, dur, int(sr * dur), False)
    proximity = max(0.0, min(1.0, proximity))
    freq = 360.0 * (7.0 ** proximity)
    vibrato = 1.0 + 0.007 * np.sin(2 * np.pi * 5.2 * t)
    phase = 2 * np.pi * np.cumsum(np.full_like(t, freq) * vibrato) / sr
    wave = (
        np.sin(phase) * 0.94
        + np.sin(phase * 2.0) * 0.045
        + np.sin(phase * 3.0) * 0.015
    )

    attack = max(1, int(sr * 0.055))
    env = np.exp(-2.15 * t / dur)
    env[:attack] *= np.linspace(0, 1, attack)
    wave *= env

    amplitude = 900 + proximity * 1500
    stereo = np.vstack([wave, wave]).T
    peak = np.max(np.abs(stereo)) or 1.0
    return (stereo * (amplitude / peak)).astype(np.int16)


def _make_heartbeat(sr: int = 44100) -> np.ndarray:
    """Synthesise a single low heartbeat thump."""
    t = np.linspace(0, 0.06, int(sr * 0.06), False)
    wave = (np.sin(2 * np.pi * 80 * t) * np.linspace(1, 0, len(t))
            + np.sin(2 * np.pi * 160 * t) * np.linspace(0.5, 0, len(t)))
    audio = (wave * 24000).astype(np.int16)
    return np.ascontiguousarray(np.stack([audio, audio], axis=-1))


def _make_fanfare(sr: int = 44100) -> np.ndarray:
    """Synthesise a short victory fanfare."""
    segments = []
    for freq, dur in [(523, 0.15), (659, 0.15), (784, 0.15), (1047, 0.4)]:
        t = np.linspace(0, dur, int(sr * dur), False)
        wave = np.sin(2 * np.pi * freq * t) * np.linspace(1, 0.3, len(t))
        segments.append(wave)
    full = np.concatenate(segments)
    audio = (full * 10000).astype(np.int16)
    return np.ascontiguousarray(np.stack([audio, audio], axis=-1))


def _make_buzzer(sr: int = 44100) -> np.ndarray:
    """Synthesise a timeout buzzer."""
    t = np.linspace(0, 1.5, int(sr * 1.5), False)
    wave = np.sin(2 * np.pi * 120 * t) * np.linspace(1, 0, len(t))
    audio = (wave * 10000).astype(np.int16)
    return np.ascontiguousarray(np.stack([audio, audio], axis=-1))


# ---------------------------------------------------------------------------
# ChallengeGame
# ---------------------------------------------------------------------------

class ChallengeGame:
    """Country discovery challenge game.

    Parameters
    ----------
    announce_cb:
        Callable(str) — used to send messages to the screen reader / UI.
        Should be wx-safe (call via wx.CallAfter if needed).
    lookup_cb:
        Callable() — triggers a fresh location lookup so the win condition
        is checked immediately after a move.  Optional.
    time_limit:
        Seconds the player has to find the country.  Default 300 (5 min).
    """

    TIME_LIMIT = 180   # seconds (3 minutes)

    def __init__(
        self,
        announce_cb,
        lookup_cb=None,
        time_limit: int = TIME_LIMIT,
        timeout_cb=None,
        direction_mode_cb=None,
        position_tone_cb=None,
        log_cb=None,
    ) -> None:
        self._announce       = announce_cb
        self._lookup_cb      = lookup_cb
        self._time_limit     = time_limit
        self._timeout_cb     = timeout_cb
        self._direction_mode_cb = direction_mode_cb or (lambda: "map")
        self._position_tone_cb = position_tone_cb
        self._log_cb         = log_cb or (lambda msg: None)

        self.active          = False
        self.target_country  = ""
        self._target_lat     = 0.0
        self._target_lon     = 0.0
        self._start_time     = 0.0
        self._generation     = 0      # incremented on each start to cancel stale callbacks
        self._lat            = 0.0
        self._lon            = 0.0
        self._last_logged_dist    = None
        # Milestone scoring
        self.target_continent     = ""
        self.target_subregion     = ""
        self._milestone_continent = False
        self._milestone_region    = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self, df, lat: float, lon: float) -> None:
        """Pick a random playable country and begin the challenge."""
        self.stop(silent=True)

        # Filter df to playable countries that actually exist in the data
        available = df[df["country"].isin(PLAYABLE_COUNTRIES)]["country"].unique()
        if len(available) == 0:
            self._announce("No playable countries found in data.")
            return

        rng = np.random.default_rng()
        country = str(rng.choice(available))

        self.target_country = country
        self._target_lat, self._target_lon = _country_centroid(df, country)
        self._lat  = lat
        self._lon  = lon
        self.active = True
        self._start_time = time.time()
        self._generation += 1
        self._last_logged_dist    = None
        self._milestone_continent = False
        self._milestone_region    = False
        self.target_continent     = ""
        self.target_subregion     = ""

        # Fetch target continent/subregion in background for milestone scoring
        def _fetch_region(country=country):
            try:
                import urllib.request, json, urllib.parse
                query = urllib.parse.quote(country)
                url   = f"https://restcountries.com/v3.1/name/{query}?fields=region,subregion"
                req   = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode())
                if data and isinstance(data, list):
                    self.target_continent = data[0].get("region", "")
                    self.target_subregion = data[0].get("subregion", "")
            except Exception:
                pass
        threading.Thread(target=_fetch_region, daemon=True).start()

        mins, secs = divmod(int(self._time_limit), 60)
        if secs:
            limit_text = f"{mins} minute{'s' if mins != 1 else ''} and {secs} second{'s' if secs != 1 else ''}"
        else:
            limit_text = f"{mins} minute{'s' if mins != 1 else ''}"
        self._announce(
            f"Challenge! You have {limit_text} to find {country}. "
            f"Listen to the beeps for direction. Good luck!"
        )

        self._schedule_heartbeat(self._generation)
        self._play_feedback()

    def stop(self, silent: bool = False) -> None:
        """Cancel the current challenge."""
        if not self.active and silent:
            return
        self.active = False
        self._generation += 1   # cancels pending heartbeat callbacks
        self.target_country = ""
        if not silent:
            self._announce("Challenge ended. Back to free roam.")

    def on_move(self, lat: float, lon: float) -> None:
        """Call whenever the player moves. Plays a hot/cold beep."""
        self._lat = lat
        self._lon = lon
        if self.active:
            self._play_feedback()
            # Movement trace — only log if distance to target changed by 50km+
            d_lat = self._target_lat - lat
            d_lon = self._delta_lon_to_target(lon)
            dist_km = round(math.sqrt(d_lat**2 + d_lon**2) * 111)
            last_dist = getattr(self, '_last_logged_dist', None)
            if last_dist is None or abs(dist_km - last_dist) >= 50:
                self._last_logged_dist = dist_km
                elapsed = round(time.time() - self._start_time, 1)
                self._log_cb(
                    f"move: target={self.target_country} "
                    f"lat={lat:.2f} lon={lon:.2f} "
                    f"dist={dist_km}km elapsed={elapsed}s"
                )

            # Milestone: continent
            if not self._milestone_continent and self.target_continent:
                if getattr(self, '_current_continent_cb', None):
                    cur_continent = self._current_continent_cb()
                    if cur_continent and cur_continent == self.target_continent:
                        self._milestone_continent = True
                        self._announce(f"You're in the right continent — {cur_continent}!")

            # Milestone: subregion
            if self._milestone_continent and not self._milestone_region \
                    and self.target_subregion:
                if getattr(self, '_current_subregion_cb', None):
                    cur_subregion = self._current_subregion_cb()
                    if cur_subregion and cur_subregion == self.target_subregion:
                        self._milestone_region = True
                        self._announce(f"You're in the right region — {cur_subregion}!")

    def _milestone_score(self, elapsed: float) -> tuple[int, str]:
        """Calculate milestone-based score and breakdown string."""
        continent_pts = 25 if self._milestone_continent else 0
        region_pts    = 25 if self._milestone_region    else 0
        # Time bonus for finding the country
        if elapsed < 30:
            time_pts = 100
        elif elapsed < 60:
            time_pts = 75
        elif elapsed < 120:
            time_pts = 50
        else:
            time_pts = 25
        total = continent_pts + region_pts + time_pts
        parts = []
        if continent_pts: parts.append(f"continent +{continent_pts}")
        if region_pts:    parts.append(f"region +{region_pts}")
        parts.append(f"time +{time_pts}")
        breakdown = ", ".join(parts)
        return total, breakdown

    def _timeout_milestone_score(self) -> tuple[int, str]:
        """Score for a timeout — milestones only, no time bonus."""
        continent_pts = 25 if self._milestone_continent else 0
        region_pts    = 25 if self._milestone_region    else 0
        total = continent_pts + region_pts
        parts = []
        if continent_pts: parts.append(f"continent +{continent_pts}")
        if region_pts:    parts.append(f"region +{region_pts}")
        breakdown = ", ".join(parts) if parts else "no milestones reached"
        return total, breakdown

    def on_win(self) -> None:
        """Call from _lookup when the player lands in the target country."""
        if not self.active:
            return
        country = self.target_country
        elapsed = int(time.time() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        score, breakdown = self._milestone_score(elapsed)
        self.active = False
        self._generation += 1
        self._announce(
            f"You found {country}! "
            f"Time: {mins} minute{'s' if mins != 1 else ''} "
            f"and {secs} second{'s' if secs != 1 else ''}. "
            f"Score: {score} points ({breakdown}). Well done!"
        )
        threading.Thread(target=lambda: _play_sound_array(_make_fanfare(), 1),
                         daemon=True).start()

    def repeat_target(self) -> None:
        """Re-announce the target country (Shift+F10)."""
        if not self.active:
            self._announce("No challenge currently active.")
            return
        elapsed = int(time.time() - self._start_time)
        remaining = max(0, self._time_limit - elapsed)
        mins, secs = divmod(remaining, 60)
        self._announce(
            f"Find {self.target_country}. "
            f"{mins}:{secs:02d} remaining."
        )

    # ------------------------------------------------------------------
    # Audio
    # ------------------------------------------------------------------

    def _direction_mode(self) -> str:
        try:
            mode = str(self._direction_mode_cb() or "map").lower()
        except Exception:
            mode = "map"
        return "globe" if mode in ("globe", "shortest") else "map"

    def _delta_lon_to_target(self, lon: float) -> float:
        d_lon = self._target_lon - lon
        if self._direction_mode() == "globe":
            if d_lon >  180:
                d_lon -= 360
            if d_lon < -180:
                d_lon += 360
        return d_lon

    def _play_feedback(self) -> None:
        """Map-position tone plus a subtle centred sonar pulse for closeness."""
        if not self.active:
            return

        if self._position_tone_cb:
            try:
                self._position_tone_cb(self._lat, self._lon)
            except Exception:
                pass

        d_lat = self._target_lat - self._lat
        d_lon = self._delta_lon_to_target(self._lon)

        dist = math.sqrt(d_lat ** 2 + d_lon ** 2)   # degrees

        # In map-learning mode, east/west distance can span the whole map.
        max_dist = math.sqrt(180.0 ** 2 + 360.0 ** 2) if self._direction_mode() == "map" else 180.0
        proximity = max(0.0, 1.0 - (dist / max_dist))

        def _play():
            # Leave the map-position tone room to speak first.
            time.sleep(0.10)
            _play_sound_array(_make_sonar_pulse(proximity), channel_idx=3)

        threading.Thread(target=_play, daemon=True).start()

    def _schedule_heartbeat(self, generation: int) -> None:
        """Schedule the next heartbeat tick via wx.CallLater."""
        import wx
        if not self.active or generation != self._generation:
            return
        elapsed  = time.time() - self._start_time
        remaining = self._time_limit - elapsed

        if remaining <= 0:
            self._timeout()
            return

        # Interval shrinks from 2000ms at start to 200ms at end
        interval = int(max(200, 2000 - 1800 * (elapsed / self._time_limit)))
        wx.CallLater(interval, self._heartbeat_tick, generation)

    def _heartbeat_tick(self, generation: int) -> None:
        if not self.active or generation != self._generation:
            return
        threading.Thread(
            target=lambda: _play_sound_array(_make_heartbeat()),
            daemon=True,
        ).start()
        self._schedule_heartbeat(generation)

    def _timeout(self) -> None:
        country = self.target_country
        self.active = False
        self._generation += 1
        threading.Thread(
            target=lambda: _play_sound_array(_make_buzzer(), 1),
            daemon=True,
        ).start()
        if self._timeout_cb:
            wx.CallAfter(self._timeout_cb)
        else:
            score, breakdown = self._timeout_milestone_score()
            if score > 0:
                result = (f"Time's up! The answer was {country}. "
                          f"You scored {score} points ({breakdown}).")
            else:
                result = f"Time's up! The answer was {country}. Better luck next time!"
            wx.CallAfter(self._announce, result)
            if hasattr(self, '_log_cb') and self._log_cb:
                self._log_cb(f"Solo timeout: country={country} score={score}")


# ---------------------------------------------------------------------------
# ChallengeSession — multi-round, multi-player wrapper around ChallengeGame
# ---------------------------------------------------------------------------

class ChallengeSession:
    """Manages a scored multi-round challenge session for 1 or 2 players.

    Parameters
    ----------
    game:
        The ChallengeGame instance (handles hot/cold beeps, timer, win/loss).
    announce_cb:
        Callable(str) — wx-safe announcement function.
    players:
        List of player name strings, e.g. ["Alice", "Bob"] or ["Alice"].
    rounds:
        Number of rounds each player plays.
    on_complete:
        Called with no args when the session finishes.
    wait_cb:
        Callable(str) — used to show the "pass keyboard / press Space" message.
    """

    def __init__(self, game, announce_cb, players, rounds,
                 on_complete=None, wait_cb=None, stop_sound_cb=None, log_cb=None):
        self._game         = game
        self._announce     = announce_cb
        self._players      = players
        self._rounds       = rounds
        self._on_complete  = on_complete
        self._wait_cb      = wait_cb or announce_cb
        self._stop_sound   = stop_sound_cb or (lambda: None)
        self._log          = log_cb or (lambda msg: None)

        self._scores       = {p: 0 for p in players}
        self._round_scores = {p: [] for p in players}
        self._current_turn = 0   # index into _turn_order
        self._turn_order   = []  # flat list: p1r1, p2r1, p1r2, p2r2, ...
        self.active        = False
        self._waiting      = False  # between turns, waiting for Space

        # Build turn order: interleaved rounds
        for r in range(rounds):
            for p in players:
                self._turn_order.append(p)

    # ------------------------------------------------------------------
    # Public interface (called from core.py)
    # ------------------------------------------------------------------

    def start(self, df, lat, lon):
        """Begin the session at the first turn."""
        self.active    = True
        self._waiting  = False
        self._current_turn = 0
        self._scores       = {p: 0 for p in self._players}
        self._round_scores = {p: [] for p in self._players}
        self._log(
            f"session_start players={','.join(self._players)} "
            f"rounds={self._rounds}"
        )
        self._start_turn(df, lat, lon)

    def on_win(self, elapsed: float, df, lat, lon):
        """Called by core when the current player finds their country."""
        if not self.active:
            return
        import wx
        score, breakdown = self._game._milestone_score(elapsed)
        player  = self._turn_order[self._current_turn]
        country = self._game.target_country
        self._record_score(score)
        self._log(
            f"player={player} country={country} "
            f"time={elapsed:.1f}s score={score} total={self._scores[player]}"
        )
        mins, secs = divmod(int(elapsed), 60)
        self._announce(
            f"{player} found {country}!  "
            f"Time: {mins} minute{'s' if mins != 1 else ''} "
            f"and {secs} second{'s' if secs != 1 else ''}.  "
            f"Score: {score} points ({breakdown}).  Total: {self._scores[player]}."
        )
        threading.Thread(
            target=lambda: _play_sound_array(_make_fanfare(), 1),
            daemon=True,
        ).start()
        wx.CallLater(3000, self._advance, df, lat, lon)

    def on_timeout(self, df, lat, lon):
        """Called by core when the timer expires."""
        if not self.active:
            return
        player  = self._turn_order[self._current_turn]
        country = self._game.target_country
        score, breakdown = self._game._timeout_milestone_score()
        self._record_score(score)
        self._log(
            f"player={player} country={country} "
            f"time=timeout score={score} total={self._scores[player]}"
        )
        if score > 0:
            self._announce(
                f"Time's up, {player}.  The answer was {country}.  "
                f"{score} points for milestones reached ({breakdown}).")
        else:
            self._announce(
                f"Time's up, {player}.  The answer was {country}.  No points this round.")
        self._advance(df, lat, lon)

    def on_space(self, df, lat, lon):
        """Called when Space is pressed during the between-turn wait."""
        if not self.active or not self._waiting:
            return False
        self._waiting = False
        self._start_turn(df, lat, lon)
        return True

    def stop(self):
        """Abort the session."""
        self.active   = False
        self._waiting = False
        self._game.stop(silent=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record_score(self, score):
        player = self._turn_order[self._current_turn]
        self._scores[player] += score
        self._round_scores[player].append(score)

    def _advance(self, df, lat, lon):
        self._current_turn += 1
        if self._current_turn >= len(self._turn_order):
            self._finish()
            return

        # After each complete round (all players have played), announce standings
        if len(self._players) > 1 and self._current_turn % len(self._players) == 0:
            completed_round = self._current_turn // len(self._players)
            standings = ".  ".join(
                f"{p}: {self._scores[p]}" for p in self._players)
            self._announce(
                f"End of round {completed_round}.  Scores — {standings}."
            )

        next_player = self._turn_order[self._current_turn]
        if len(self._players) > 1:
            self._waiting = True
            self._wait_cb(
                f"Pass the keyboard to {next_player}.  "
                f"Press Space when ready."
            )
        else:
            import wx
            round_num = self._current_turn + 1
            self._announce(f"Round {round_num}.  Get ready...")
            wx.CallLater(2000, self._start_turn, df, lat, lon)

    def _start_turn(self, df, lat, lon):
        if not self.active:
            return
        player    = self._turn_order[self._current_turn]
        round_num = self._round_scores[player].__len__() + 1
        self._stop_sound()
        self._announce(
            f"{player}'s turn — round {round_num} of {self._rounds}."
        )
        import wx
        wx.CallLater(1500, self._game.start, df, lat, lon)

    def _finish(self):
        self.active = False
        parts = []
        for p in self._players:
            parts.append(f"{p}: {self._scores[p]} points")
        scores_str = ".  ".join(parts)

        if len(self._players) > 1:
            winner = max(self._players, key=lambda p: self._scores[p])
            tied   = len(set(self._scores.values())) == 1
            if tied:
                result = "It's a tie!"
            else:
                result = f"{winner} wins!"
            if self._rounds == 1:
                # Single round — skip "scores" since each player only has one
                msg = f"Game over!  {result}  {scores_str}."
            else:
                msg = f"Game over!  Final scores — {scores_str}.  {result}"
        else:
            player = self._players[0]
            total  = self._scores[player]
            if self._rounds == 1:
                msg = f"Game over!  {player} scored {total} points."
            else:
                msg = f"Game over!  {player} — total score: {total} points."

        self._announce(msg)
        self._log(f"session_complete players={','.join(self._players)} "
                  f"final_scores={dict(self._scores)}")
        if self._on_complete:
            self._on_complete()
