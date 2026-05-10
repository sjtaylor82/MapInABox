#!/usr/local/bin/python3
"""
gnaf.cgi — GNAF street number lookup endpoint for samtaylor9.nfshost.com

Deploy:
  1. Upload gnaf.db to /home/protected/gnaf.db
  2. Upload this file to /home/public/gnaf.cgi
  3. chmod 755 /home/public/gnaf.cgi

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

DB_PATH = "/home/private/gnaf.db"

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
