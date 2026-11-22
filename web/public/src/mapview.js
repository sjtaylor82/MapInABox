// Visual Assist: a large, high-contrast map so a sighted teacher or helper
// can follow, and low-vision users can see their position.

import { data } from './world.js';
import { settings } from './settings.js';

let canvas;
let g;
let caption;
let trail = [];
let pulseUntil = 0;
let pos = { lat: 0, lon: 0 };
let label = '';
let countryName = '';

export function initMapView(canvasEl, captionEl) {
  canvas = canvasEl;
  caption = captionEl;
  g = canvas.getContext('2d');
  new ResizeObserver(draw).observe(canvas);
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', draw);
}

export function updateMapView(lat, lon, text, country) {
  if (pos.lat !== lat || pos.lon !== lon) {
    trail.push({ ...pos });
    if (trail.length > 30) trail.shift();
    pulseUntil = performance.now() + 900;
  }
  pos = { lat, lon };
  if (text != null) label = text;
  if (country != null) countryName = country;
  caption.textContent = label;
  requestAnimationFrame(animate);
}

function animate() {
  draw();
  if (performance.now() < pulseUntil && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    requestAnimationFrame(animate);
  }
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function draw() {
  if (!canvas || canvas.hidden) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);

  const zoom = settings.get('zoom') || 1;
  // Equirectangular: at zoom 1 the whole world fits; above that, centre on the cursor.
  const scale = Math.min(w / 360, h / 180) * zoom;
  const cx = zoom === 1 ? 0 : pos.lon;
  const cy = zoom === 1 ? 0 : pos.lat;
  const X = (lon) => w / 2 + (lon - cx) * scale;
  const Y = (lat) => h / 2 - (lat - cy) * scale;

  g.fillStyle = css('--map-water');
  g.fillRect(0, 0, w, h);

  const viewMinLon = cx - w / 2 / scale;
  const viewMaxLon = cx + w / 2 / scale;
  const viewMinLat = cy - h / 2 / scale;
  const viewMaxLat = cy + h / 2 / scale;
  const land = css('--map-land');
  const current = css('--map-current');
  const border = css('--map-border');
  g.lineWidth = 1;
  g.strokeStyle = border;
  for (const c of data.countries) {
    const [minx, miny, maxx, maxy] = c.bbox;
    if (maxx < viewMinLon || minx > viewMaxLon || maxy < viewMinLat || miny > viewMaxLat) continue;
    g.fillStyle = c.name === countryName ? current : land;
    g.beginPath();
    for (const poly of c.polys) {
      for (const ring of poly) {
        ring.forEach(([lon, lat], i) => (i ? g.lineTo(X(lon), Y(lat)) : g.moveTo(X(lon), Y(lat))));
        g.closePath();
      }
    }
    g.fill('evenodd');
    g.stroke();
  }

  // Trail of recent movement.
  g.fillStyle = css('--map-trail');
  for (const p of trail) {
    g.beginPath();
    g.arc(X(p.lon), Y(p.lat), 3, 0, Math.PI * 2);
    g.fill();
  }

  // Cursor with a halo that pulses after a move.
  const x = X(pos.lon);
  const y = Y(pos.lat);
  const t = Math.max(0, pulseUntil - performance.now()) / 900;
  g.strokeStyle = css('--map-cursor-halo');
  g.lineWidth = 6;
  g.beginPath();
  g.arc(x, y, 14 + t * 14, 0, Math.PI * 2);
  g.stroke();
  g.fillStyle = css('--map-cursor');
  g.beginPath();
  g.arc(x, y, 7, 0, Math.PI * 2);
  g.fill();
  g.strokeStyle = css('--map-cursor-halo');
  g.lineWidth = 2;
  g.stroke();
}
