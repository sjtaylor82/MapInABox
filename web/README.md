# Map in a Box Web

An early browser edition of Map in a Box. It has the world map mode
(arrow-key exploration, country sounds, spatial tones, jumping, country
facts, weather, time and Visual Assist) and a first street mode: moving by
metres with the nearest street or corner spoken, following streets to the
next corner, and a list of places nearby. Street data comes from
OpenStreetMap, one tile of about 2 km at a time.

On phones and tablets you can steer by tilting (`public/src/steering.js`):
held sideways, the angle at "Ready" is still, a gentle tilt towards a
direction goes that way, and coming back to still stops. Nothing is tapped.

It is plain JavaScript modules with no build step for the code. The map data
is generated from the desktop data files.

## Build the data

From the repository root:

```
python web/tools/build_data.py
```

This writes `web/public/data/` and copies the sounds to `web/public/sounds/`.
Both are generated and are not committed. Run it again whenever the desktop
data, sounds or lookup tables change.

## Try it locally

```
python -m http.server 8765 --directory web/public
```

Then open http://localhost:8765/. Without the Worker, online search goes
straight to Photon and street mode straight to the public Overpass servers.

To run it with the Worker as it will be hosted:

```
cd web
npx wrangler dev
```

## Host it

```
cd web
npx wrangler deploy
```

This publishes the site and the `/api/` routes as one Cloudflare Worker.
Set a custom domain in `wrangler.jsonc` so schools can allow it by name.

## Services it contacts

- Its own domain, for the map, sounds, `/api/search` (Photon, via the Worker)
  and `/api/streets` (OpenStreetMap streets and places from Overpass, via the
  Worker, cached for two weeks per tile). A tile nobody has asked for yet can
  take up to a minute when the public Overpass servers are busy.
- `api.open-meteo.com` and `air-quality-api.open-meteo.com` for weather,
  time zone, sunrise and air quality, when those keys are pressed.
- `en.wikipedia.org` for Wikipedia summaries, when asked.
