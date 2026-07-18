#!/usr/local/bin/python3
"""
gnaf.cgi — GNAF street number lookup endpoint for samtaylor9.nfshost.com

Deploy:
  1. Upload gnaf.db to /home/protected/gnaf.db
  2. Upload this file to /home/public/gnaf.cgi
  3. chmod 755 /home/public/gnaf.cgi
  4. Ensure /home/private/ is writable by the CGI process — a
     gnaf_ratelimit.db file is created there automatically on first
     request to track per-IP and daily request limits.

Query:
  GET https://samtaylor9.nfshost.com/gnaf.cgi?lat=-27.47&lon=153.02&street=Queen+Street&radius=60

Response (JSON):
  {"number": "123"}          — found
  {"number": null}           — not found
  {"error": "..."}           — bad request
"""

import cgi
import json
import math
import os
import sqlite3
import sys
import time

DB_PATH = "/home/private/gnaf.db"

# --- Rate limiting -----------------------------------------------------
# Cheap-hosting protection: no auth on this endpoint, so we cap both
# per-IP burst rate and total daily volume to avoid the account being
# throttled/suspended by the host if the URL ever leaks or gets scraped.
RATE_DB_PATH   = "/home/private/gnaf_ratelimit.db"
PER_IP_LIMIT   = 30      # max requests per IP within PER_IP_WINDOW seconds
PER_IP_WINDOW  = 60
DAILY_LIMIT    = 5000    # max total requests (all IPs) per calendar day (UTC)


def check_rate_limit():
    """Returns (data, status) tuple if the request should be blocked,
    or None if it's allowed to proceed. Fails open (allows the request)
    if the rate-limit DB itself has a problem, so a local hiccup here
    never takes down the whole GNAF lookup service."""
    ip = os.environ.get("REMOTE_ADDR", "unknown")
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.gmtime(now))

    try:
        con = sqlite3.connect(RATE_DB_PATH, timeout=5)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                ip TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_counts (
                day TEXT PRIMARY KEY,
                count INTEGER NOT NULL
            )
        """)

        # Drop per-IP entries outside the burst window.
        cur.execute("DELETE FROM requests WHERE ts < ?", (now - PER_IP_WINDOW,))

        cur.execute(
            "SELECT COUNT(*) FROM requests WHERE ip = ? AND ts >= ?",
            (ip, now - PER_IP_WINDOW),
        )
        if cur.fetchone()[0] >= PER_IP_LIMIT:
            con.close()
            return {"error": "rate limit exceeded, try again shortly"}, "429 Too Many Requests"

        cur.execute("SELECT count FROM daily_counts WHERE day = ?", (today,))
        row = cur.fetchone()
        daily_count = row[0] if row else 0
        if daily_count >= DAILY_LIMIT:
            con.close()
            return {"error": "daily request limit reached, try again tomorrow"}, "503 Service Unavailable"

        cur.execute("INSERT INTO requests (ip, ts) VALUES (?, ?)", (ip, now))
        if row:
            cur.execute("UPDATE daily_counts SET count = count + 1 WHERE day = ?", (today,))
        else:
            cur.execute("INSERT INTO daily_counts (day, count) VALUES (?, 1)", (today,))

        # Keep the daily_counts table small (7-day retention).
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - 7 * 86400))
        cur.execute("DELETE FROM daily_counts WHERE day < ?", (cutoff,))

        con.commit()
        con.close()
        return None
    except Exception:
        return None


def cors_headers():
    print("Access-Control-Allow-Origin: *")
    print("Access-Control-Allow-Methods: GET")

def respond(data, status="200 OK"):
    print(f"Status: {status}")
    print("Content-Type: application/json")
    cors_headers()
    print()
    print(json.dumps(data))

def main():
    blocked = check_rate_limit()
    if blocked:
        data, status = blocked
        respond(data, status)
        return

    form = cgi.FieldStorage()

    # Bulk bbox query: ?mode=bbox&lat=...&lon=...&radius=...
    # Returns all addresses in bounding box as JSON array
    if form.getvalue("mode") == "bbox":
        try:
            lat    = float(form.getvalue("lat", ""))
            lon    = float(form.getvalue("lon", ""))
            radius = min(float(form.getvalue("radius", "1000")), 2000)
        except (TypeError, ValueError):
            respond({"error": "lat and lon required"}, "400 Bad Request")
            return
        if not os.path.exists(DB_PATH):
            respond({"error": "database not found"}, "500 Internal Server Error")
            return
        lat_deg = radius / 111000.0
        lon_deg = radius / (111000.0 * math.cos(math.radians(lat)))
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                                  check_same_thread=False)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("""
                SELECT street_number, street_name, street_type, lat, lon
                FROM addresses
                WHERE lat BETWEEN ? AND ?
                  AND lon BETWEEN ? AND ?
                LIMIT 5000
            """, (lat - lat_deg, lat + lat_deg,
                  lon - lon_deg, lon + lon_deg))
            rows = cur.fetchall()
            con.close()
            result = [{"number": r["street_number"],
                       "street": r["street_name"] + " " + r["street_type"],
                       "lat": r["lat"], "lon": r["lon"]} for r in rows]
            respond({"addresses": result})
        except Exception as e:
            respond({"error": str(e)}, "500 Internal Server Error")
        return

    try:
        lat    = float(form.getvalue("lat", ""))
        lon    = float(form.getvalue("lon", ""))
        street = (form.getvalue("street") or "").strip()
        radius = float(form.getvalue("radius", "60"))
    except (TypeError, ValueError):
        respond({"error": "lat, lon, and street are required"}, "400 Bad Request")
        return

    if not street:
        respond({"error": "street is required"}, "400 Bad Request")
        return

    if not os.path.exists(DB_PATH):
        respond({"error": "database not found"}, "500 Internal Server Error")
        return

    # Use a generous search bbox — parcel centroids can sit well off the road.
    # We search a fixed 500m box and then return the closest on the street.
    search_m = max(radius, 500)
    lat_deg = search_m / 111000.0
    lon_deg = search_m / (111000.0 * math.cos(math.radians(lat)))

    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # Find candidates within bounding box on this street.
        # DB stores street_name and street_type separately (e.g. QUEEN / STREET).
        # Split incoming street param and match both columns.
        parts = street.rsplit(None, 1)
        if len(parts) == 2:
            s_name, s_type = parts
        else:
            s_name, s_type = street, ""

        cur.execute("""
            SELECT street_number, lat, lon
            FROM addresses
            WHERE street_name = ? COLLATE NOCASE
              AND street_type = ? COLLATE NOCASE
              AND lat BETWEEN ? AND ?
              AND lon BETWEEN ? AND ?
        """, (
            s_name, s_type,
            lat - lat_deg, lat + lat_deg,
            lon - lon_deg, lon + lon_deg,
        ))
        rows = cur.fetchall()
        con.close()

        if not rows:
            respond({"number": None})
            return

        # Find nearest by actual distance
        best_num  = None
        best_dist = float("inf")
        for row in rows:
            dy = (row["lat"] - lat) * 111000.0
            dx = (row["lon"] - lon) * 111000.0 * math.cos(math.radians(lat))
            d  = math.sqrt(dx*dx + dy*dy)
            if d < best_dist:
                best_dist = d
                best_num  = row["street_number"]

        respond({"number": best_num})

    except Exception as e:
        respond({"error": str(e)}, "500 Internal Server Error")

main()
