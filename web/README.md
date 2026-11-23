# Map in a Box Web

An early browser edition of Map in a Box. It has the world map mode
(arrow-key exploration, country sounds, spatial tones, jumping, country
facts, weather, time and Visual Assist) and a first street mode: moving by
metres with the nearest street or corner spoken, following streets to the
next corner, and a list of places nearby. Street data comes from
OpenFreeMap vector tiles of OpenStreetMap (© OpenMapTiles © OpenStreetMap
contributors), one zoom-14 tile of about 2 km at a time, decoded in
`public/src/vectortile.js`.

On phones and tablets you can steer by tilting (`public/src/steering.js`):
lying flat is still, and tilting the top edge down, up, or the right or left
edge down moves north, south, east or west, like the arrow keys.

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
straight to Photon and street mode straight to OpenFreeMap.

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
  and `/api/tile/14/x/y` (OpenFreeMap vector tiles of OpenStreetMap streets
  and places, via the Worker, cached for a week per tile).
- `api.open-meteo.com` and `air-quality-api.open-meteo.com` for weather,
  time zone, sunrise and air quality, when those keys are pressed.
- `en.wikipedia.org` for Wikipedia summaries, when asked.
