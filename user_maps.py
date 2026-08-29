"""Small, self-contained maps drawn by Map in a Box users.

The public model deliberately uses a local metre-like X/Y plane.  Creators draw
paths and place labels; this module turns those strokes into a routable graph.
It has no wx dependency so loading, validation, and routing are easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import json
import math
import os
import shutil
import tempfile
import zipfile


FORMAT_VERSION = 1
MAP_JSON = "map.json"


def suggested_save_path(source_path):
    """Return a .miabmap path beside an imported source file."""
    if not source_path:
        return ""
    return os.path.splitext(os.path.abspath(source_path))[0] + ".miabmap"
BACKGROUND_FILE = "background.jpg"


def new_map(name="Untitled map", width=500.0, height=350.0):
    return {
        "format": FORMAT_VERSION,
        "name": str(name or "Untitled map"),
        "width": max(10.0, float(width)),
        "height": max(10.0, float(height)),
        "start": "",
        "places": [],
        "paths": [],
        "background": "",
        "source_text": "",
        "source_page": 0,
        "barriers": [],
    }


def new_floor(name="Floor 1", width=500.0, height=350.0, source_page=0):
    floor = new_map(name, width, height)
    floor.pop("format", None)
    floor["source_page"] = int(source_page)
    return floor


def floors_for(data):
    """Return floor surfaces, treating old map files as a single floor."""
    floors = data.get("floors") if isinstance(data, dict) else None
    return floors if isinstance(floors, list) and floors else [data]


def validate_map(data):
    if not isinstance(data, dict):
        raise ValueError("The map file does not contain a map.")
    if int(data.get("format", 0)) != FORMAT_VERSION:
        raise ValueError("This map file uses an unsupported format version.")
    data["name"] = str(data.get("name") or "Untitled map")
    def validate_surface(surface, default_name):
        if not isinstance(surface, dict):
            raise ValueError("The map contains a damaged floor.")
        surface["name"] = str(surface.get("name") or default_name)
        surface["width"] = max(10.0, float(surface.get("width", 500.0)))
        surface["height"] = max(10.0, float(surface.get("height", 350.0)))
        surface.setdefault("start", "")
        surface.setdefault("background", "")
        surface.setdefault("source_text", "")
        surface["source_page"] = int(surface.get("source_page", 0))
        surface.setdefault("places", [])
        surface.setdefault("paths", [])
        surface.setdefault("barriers", [])
        if not isinstance(surface["places"], list) or not isinstance(surface["paths"], list):
            raise ValueError("The map's places or paths are damaged.")
        for path in surface["paths"]:
            points = path.get("points") if isinstance(path, dict) else None
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError("Every path must contain at least two points.")
            path["points"] = [[float(p[0]), float(p[1])] for p in points]
        for barrier in surface["barriers"]:
            points = barrier.get("points") if isinstance(barrier, dict) else None
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError("Every barrier must contain at least two points.")
            barrier["points"] = [[float(p[0]), float(p[1])] for p in points]
        seen = set()
        for index, place in enumerate(surface["places"]):
            if not isinstance(place, dict) or not str(place.get("name", "")).strip():
                raise ValueError("Every place must have a name.")
            place["name"] = str(place["name"]).strip()
            place["id"] = str(place.get("id") or f"place-{index + 1}")
            place["x"] = float(place["x"])
            place["y"] = float(place["y"])
            place["description"] = str(place.get("description") or "").strip()
            if place["id"] in seen:
                raise ValueError("The map contains duplicate place identifiers on a floor.")
            seen.add(place["id"])

    floors = data.get("floors")
    if floors is not None:
        if not isinstance(floors, list) or not floors:
            raise ValueError("A multi-floor map must contain at least one floor.")
        for index, floor in enumerate(floors):
            validate_surface(floor, f"Floor {index + 1}")
    else:
        validate_surface(data, data["name"])
    return data


def save_map(path, data, background_path=None):
    """Save a portable .miabmap ZIP, embedding an optional JPEG background."""
    data = validate_map(json.loads(json.dumps(data)))
    target = os.path.abspath(path)
    if not target.lower().endswith(".miabmap"):
        target += ".miabmap"
    surfaces = floors_for(data)
    if isinstance(background_path, (list, tuple)):
        backgrounds = list(background_path)
    else:
        backgrounds = [background_path]
    backgrounds.extend([None] * (len(surfaces) - len(backgrounds)))
    members = []
    for index, (surface, source) in enumerate(zip(surfaces, backgrounds)):
        member = ""
        if source:
            extension = os.path.splitext(source)[1].lower()
            filename = "background.png" if extension == ".png" else BACKGROUND_FILE
            member = f"floors/{index + 1}/{filename}" if "floors" in data else filename
        surface["background"] = member
        members.append((member, source))
    parent = os.path.dirname(target) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="miabmap-", suffix=".tmp", dir=parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MAP_JSON, json.dumps(data, indent=2, ensure_ascii=False))
            for member, source in members:
                if member and source:
                    archive.write(source, member)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return target


def load_map(path, extract_dir=None):
    """Load a map and optionally extract its background for display."""
    with zipfile.ZipFile(path, "r") as archive:
        try:
            data = json.loads(archive.read(MAP_JSON).decode("utf-8"))
        except KeyError as exc:
            raise ValueError("This is not a Map in a Box map file.") from exc
        data = validate_map(data)
        backgrounds = []
        for index, surface in enumerate(floors_for(data)):
            background = ""
            member = surface.get("background")
            if member and member in archive.namelist() and extract_dir:
                floor_dir = os.path.join(extract_dir, f"floor-{index + 1}")
                os.makedirs(floor_dir, exist_ok=True)
                background = os.path.join(floor_dir, os.path.basename(member))
                with archive.open(member) as source, open(background, "wb") as dest:
                    shutil.copyfileobj(source, dest)
            backgrounds.append(background)
    return data, backgrounds if "floors" in data else backgrounds[0]


def pdf_page_count(path):
    """Return the number of pages in a PDF used as a map background."""
    import fitz
    with fitz.open(path) as document:
        return document.page_count


def render_pdf_page(path, page_index, output_path, dpi=180):
    """Render one PDF page to PNG and return its text and pixel dimensions."""
    import fitz
    with fitz.open(path) as document:
        if not 0 <= page_index < document.page_count:
            raise ValueError("The selected PDF page does not exist.")
        page = document.load_page(page_index)
        scale = float(dpi) / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        pixmap.save(output_path)
        return page.get_text("text"), pixmap.width, pixmap.height


def detect_pdf_labels(path, page_index, map_width, map_height, id_prefix="label"):
    """Create positioned labels from a PDF page's embedded text lines."""
    import fitz
    labels = []
    with fitz.open(path) as document:
        if not 0 <= page_index < document.page_count:
            raise ValueError("The selected PDF page does not exist.")
        page = document.load_page(page_index)
        page_width = max(float(page.rect.width), 1.0)
        page_height = max(float(page.rect.height), 1.0)
        structure = page.get_text("dict")
        for block in structure.get("blocks", []):
            if block.get("type") != 0:
                continue
            line_items = []
            for line in block.get("lines", []):
                spans = [span for span in line.get("spans", [])
                         if str(span.get("text", "")).strip()]
                text = " ".join(str(span["text"]).strip() for span in spans).strip(" .")
                if not text:
                    continue
                x0 = min(float(span["bbox"][0]) for span in spans)
                y0 = min(float(span["bbox"][1]) for span in spans)
                x1 = max(float(span["bbox"][2]) for span in spans)
                y1 = max(float(span["bbox"][3]) for span in spans)
                line_items.append({
                    "text": text,
                    "bbox": (x0, y0, x1, y1),
                })

            groups = []
            for item in line_items:
                x0, y0, x1, y1 = item["bbox"]
                if groups:
                    previous = groups[-1][-1]
                    px0, py0, px1, py1 = previous["bbox"]
                    gap = y0 - py1
                    overlap = max(0.0, min(x1, px1) - max(x0, px0))
                    smaller_width = max(1.0, min(x1 - x0, px1 - px0))
                    line_height = max(y1 - y0, py1 - py0)
                    if (-line_height * 0.35 <= gap <= line_height * 0.9
                            and overlap / smaller_width >= 0.20):
                        groups[-1].append(item)
                        continue
                groups.append([item])

            for group in groups:
                text = " ".join(item["text"] for item in group).strip(" .")
                if (len(text) < 2 or text.casefold() in {"the", "to"}
                        or len(text) > 120):
                    continue
                x0 = min(item["bbox"][0] for item in group)
                y0 = min(item["bbox"][1] for item in group)
                x1 = max(item["bbox"][2] for item in group)
                y1 = max(item["bbox"][3] for item in group)
                labels.append({
                    "id": f"{id_prefix}-{len(labels) + 1}",
                    "name": text,
                    "description": "",
                    "x": ((x0 + x1) / 2.0) / page_width * map_width,
                    "y": (page_height - (y0 + y1) / 2.0) / page_height * map_height,
                })
    return labels


_OCR_READER = None


def detect_image_labels(path, map_width, map_height, id_prefix="label", min_confidence=0.40):
    """Create provisional positioned labels using original and enhanced OCR passes."""
    global _OCR_READER
    import easyocr
    import numpy as np
    from PIL import Image, ImageEnhance, ImageOps
    if _OCR_READER is None:
        _OCR_READER = easyocr.Reader(["en"], gpu=False, download_enabled=False)
    with Image.open(path) as image:
        pixel_width, pixel_height = image.size
        source = image.convert("RGB")

    # EasyOCR handles ordinary labels well at their native resolution. A
    # second pass makes tiny directory-map text occupy enough pixels to retain
    # its letter shapes. This does not deskew or otherwise move the map.
    enhanced = ImageOps.autocontrast(ImageOps.grayscale(source))
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.8)
    enhanced = enhanced.resize(
        (pixel_width * 3, pixel_height * 3), Image.Resampling.LANCZOS)
    passes = [
        (_OCR_READER.readtext(path, detail=1, paragraph=False), 1.0),
        (_OCR_READER.readtext(
            np.asarray(enhanced), detail=1, paragraph=False,
            canvas_size=4096), 3.0),
    ]

    candidates = []
    for results, scale in passes:
        for box, raw_text, confidence in results:
            candidates.append((
                [[point[0] / scale, point[1] / scale] for point in box],
                raw_text,
                float(confidence),
            ))

    # Prefer the most confident reading where both passes found the same text
    # region, while retaining separate nearby labels.
    labels, seen, accepted_bounds = [], set(), []
    for box, raw_text, confidence in sorted(
            candidates, key=lambda item: item[2], reverse=True):
        text = " ".join(str(raw_text).split()).strip(" .")
        key = text.casefold()
        if confidence < min_confidence or len(text) < 2 or key in seen:
            continue
        left = min(point[0] for point in box)
        top = min(point[1] for point in box)
        right = max(point[0] for point in box)
        bottom = max(point[1] for point in box)
        area = max(1.0, (right - left) * (bottom - top))
        overlaps_existing = False
        for old_left, old_top, old_right, old_bottom, old_area in accepted_bounds:
            intersection = max(0.0, min(right, old_right) - max(left, old_left)) * max(
                0.0, min(bottom, old_bottom) - max(top, old_top))
            if intersection / min(area, old_area) >= 0.60:
                overlaps_existing = True
                break
        if overlaps_existing:
            continue
        seen.add(key)
        accepted_bounds.append((left, top, right, bottom, area))
        centre_x = sum(point[0] for point in box) / len(box)
        centre_y = sum(point[1] for point in box) / len(box)
        labels.append({
            "id": f"{id_prefix}-{len(labels) + 1}",
            "name": text,
            "description": "",
            "x": centre_x / pixel_width * map_width,
            "y": (pixel_height - centre_y) / pixel_height * map_height,
        })
    return labels


def nearest_place(data, x, y):
    places = data.get("places") or []
    if not places:
        return None, float("inf")
    place = min(places, key=lambda p: math.hypot(p["x"] - x, p["y"] - y))
    return place, math.hypot(place["x"] - x, place["y"] - y)


def places_in_grid_cell(data, x, y, cell_size):
    """Return labels occupying the same exploration grid cell as X/Y."""
    if cell_size <= 0:
        return []
    column = int(x // cell_size)
    row = int(y // cell_size)
    return [place for place in data.get("places", [])
            if int(place["x"] // cell_size) == column
            and int(place["y"] // cell_size) == row]


def places_in_grid_row(data, y, cell_size):
    """Return all labels occupying the exploration grid row containing Y."""
    if cell_size <= 0:
        return []
    row = int(y // cell_size)
    return [place for place in data.get("places", [])
            if int(place["y"] // cell_size) == row]


def exploration_bounds(data, padding_ratio=0.05):
    """Return padded bounds around useful local-map labels and geometry."""
    width = float(data["width"])
    height = float(data["height"])
    points = [
        (float(place["x"]), float(place["y"]))
        for place in data.get("places", [])
    ]
    for collection in (data.get("paths", []), data.get("barriers", [])):
        for feature in collection:
            points.extend((float(x), float(y)) for x, y in feature.get("points", []))
    if len(points) < 2:
        return 0.0, width, 0.0, height
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    pad_x = max(width * padding_ratio, (max_x - min_x) * padding_ratio)
    pad_y = max(height * padding_ratio, (max_y - min_y) * padding_ratio)
    left = max(0.0, min_x - pad_x)
    right = min(width, max_x + pad_x)
    bottom = max(0.0, min_y - pad_y)
    top = min(height, max_y + pad_y)
    if right - left <= 1e-6 or top - bottom <= 1e-6:
        return 0.0, width, 0.0, height
    return left, right, bottom, top


def next_place_in_direction(data, x, y, dx, dy, cell_size=None):
    """Find the nearest label strictly along the current grid row or column."""
    choices = []
    for place in data.get("places", []):
        vx, vy = place["x"] - x, place["y"] - y
        forward = vx * dx + vy * dy
        if forward <= 1e-6:
            continue
        lateral = abs(vx * dy - vy * dx)
        if cell_size and cell_size > 0:
            if dx and int(place["y"] // cell_size) != int(y // cell_size):
                continue
            if dy and int(place["x"] // cell_size) != int(x // cell_size):
                continue
        elif lateral > 1e-6:
            continue
        choices.append((forward, lateral, place))
    return min(choices, default=(None, None, None), key=lambda item: item[:2])[2]


def crosses_barrier(data, start, end):
    """Return True when a movement segment crosses an explicitly mapped barrier."""
    for barrier in data.get("barriers", []):
        points = [tuple(point) for point in barrier.get("points", [])]
        for first, second in zip(points, points[1:]):
            crossing = _intersection(start, end, first, second)
            if crossing:
                point, move_t, barrier_t = crossing
                if 1e-6 < move_t <= 1.0 and -1e-6 <= barrier_t <= 1.0 + 1e-6:
                    return True
    return False


def compass_name(dx, dy):
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return "here"
    bearing = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    names = ("north", "north-east", "east", "south-east",
             "south", "south-west", "west", "north-west")
    return names[round(bearing / 45.0) % 8]


def simplify_points(points, tolerance=3.0):
    """Reduce free-hand wobble while retaining the meaningful path shape."""
    points = [tuple(point) for point in points]
    if len(points) <= 2:
        return points

    def distance_to_line(point, start, end):
        projected, _fraction = _project(point, start, end)
        return math.hypot(point[0] - projected[0], point[1] - projected[1])

    start, end = points[0], points[-1]
    index, distance = max(
        ((i, distance_to_line(point, start, end))
         for i, point in enumerate(points[1:-1], 1)),
        key=lambda item: item[1],
    )
    if distance <= tolerance:
        return [start, end]
    left = simplify_points(points[:index + 1], tolerance)
    right = simplify_points(points[index:], tolerance)
    return left[:-1] + right


def snap_drawn_endpoints(existing_paths, points, snap_distance=8.0):
    """Snap a newly drawn start/end onto an existing path when deliberately close."""
    result = [tuple(point) for point in points]
    if len(result) < 2:
        return result
    segments = []
    for path in existing_paths:
        path_points = [tuple(point) for point in path.get("points", [])]
        segments.extend(zip(path_points, path_points[1:]))
    for index in (0, -1):
        endpoint = result[index]
        best = None
        for a, b in segments:
            projected, _fraction = _project(endpoint, a, b)
            distance = math.hypot(projected[0] - endpoint[0], projected[1] - endpoint[1])
            if distance <= snap_distance and (best is None or distance < best[0]):
                best = (distance, projected)
        if best:
            result[index] = best[1]
    return result


def _project(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    length2 = vx * vx + vy * vy
    if length2 <= 1e-12:
        return a, 0.0
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / length2))
    return (a[0] + t * vx, a[1] + t * vy), t


def _intersection(a, b, c, d):
    """Return (point, first fraction, second fraction) for segment crossing."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    cross = r[0] * s[1] - r[1] * s[0]
    if abs(cross) < 1e-9:
        return None
    q = (c[0] - a[0], c[1] - a[1])
    t = (q[0] * s[1] - q[1] * s[0]) / cross
    u = (q[0] * r[1] - q[1] * r[0]) / cross
    if -1e-8 <= t <= 1.0 + 1e-8 and -1e-8 <= u <= 1.0 + 1e-8:
        return (a[0] + t * r[0], a[1] + t * r[1]), t, u
    return None


@dataclass
class Route:
    points: list
    distance: float
    origin: dict
    destination: dict


def build_graph(data, snap_distance=8.0):
    """Build a graph from free-hand strokes, crossings, and labelled places."""
    segments = []
    for path_index, path in enumerate(data.get("paths") or []):
        points = [tuple(p) for p in path["points"]]
        for segment_index, (a, b) in enumerate(zip(points, points[1:])):
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-6:
                segments.append({"a": a, "b": b, "cuts": [(0.0, a), (1.0, b)],
                                 "path": path_index, "index": segment_index})

    for i, first in enumerate(segments):
        for second in segments[i + 1:]:
            crossing = _intersection(first["a"], first["b"], second["a"], second["b"])
            if crossing:
                point, t, u = crossing
                first["cuts"].append((t, point))
                second["cuts"].append((u, point))

    place_points = {}
    for place in data.get("places") or []:
        p = (place["x"], place["y"])
        best = None
        for segment in segments:
            projected, t = _project(p, segment["a"], segment["b"])
            distance = math.hypot(projected[0] - p[0], projected[1] - p[1])
            if best is None or distance < best[0]:
                best = (distance, segment, projected, t)
        if best and best[0] <= snap_distance:
            best[1]["cuts"].append((best[3], best[2]))
            place_points[place["id"]] = best[2]

    nodes, coords, edges = {}, {}, {}

    def node_for(point):
        key = (round(point[0], 4), round(point[1], 4))
        if key not in nodes:
            nid = len(nodes)
            nodes[key] = nid
            coords[nid] = point
            edges[nid] = []
        return nodes[key]

    for segment in segments:
        cuts = sorted(segment["cuts"], key=lambda item: item[0])
        unique = []
        for _, point in cuts:
            if not unique or math.hypot(point[0] - unique[-1][0], point[1] - unique[-1][1]) > 1e-5:
                unique.append(point)
        for a, b in zip(unique, unique[1:]):
            na, nb = node_for(a), node_for(b)
            weight = math.hypot(b[0] - a[0], b[1] - a[1])
            edges[na].append((nb, weight))
            edges[nb].append((na, weight))

    place_nodes = {pid: node_for(point) for pid, point in place_points.items()}
    return coords, edges, place_nodes


def find_route(data, origin_id, destination_id):
    coords, edges, place_nodes = build_graph(data)
    if origin_id not in place_nodes or destination_id not in place_nodes:
        raise ValueError("Both places must be close to a drawn path.")
    start, goal = place_nodes[origin_id], place_nodes[destination_id]
    distances, previous = {start: 0.0}, {}
    queue = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances.get(node):
            continue
        if node == goal:
            break
        for neighbour, weight in edges.get(node, []):
            candidate = distance + weight
            if candidate < distances.get(neighbour, float("inf")):
                distances[neighbour] = candidate
                previous[neighbour] = node
                heapq.heappush(queue, (candidate, neighbour))
    if goal not in distances:
        raise ValueError("Those places are not connected by the drawn paths.")
    node, ordered = goal, [goal]
    while node != start:
        node = previous[node]
        ordered.append(node)
    ordered.reverse()
    places = {p["id"]: p for p in data["places"]}
    return Route([coords[n] for n in ordered], distances[goal],
                 places[origin_id], places[destination_id])


def connected_step(coords, edges, node, dx, dy, max_turn=67.5):
    """Choose one directly connected graph edge in the requested direction."""
    if node not in coords or not edges.get(node):
        return None
    requested = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    choices = []
    x, y = coords[node]
    for neighbour, distance in edges[node]:
        nx, ny = coords[neighbour]
        bearing = (math.degrees(math.atan2(nx - x, ny - y)) + 360.0) % 360.0
        difference = abs((bearing - requested + 180.0) % 360.0 - 180.0)
        if difference <= max_turn:
            choices.append((difference, distance, neighbour, bearing))
    return min(choices, default=None)


def connected_directions(coords, edges, node):
    """Return compact compass directions for paths leaving a graph node."""
    if node not in coords:
        return []
    x, y = coords[node]
    names = []
    for neighbour, _distance in edges.get(node, []):
        nx, ny = coords[neighbour]
        name = compass_name(nx - x, ny - y)
        if name not in names:
            names.append(name)
    return names


def route_directions(route, turn_threshold=30.0):
    points = route.points
    if len(points) < 2:
        return [f"You are at {route.destination['name']}."]

    def bearing(a, b):
        return (math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) + 360.0) % 360.0

    directions = [f"From {route.origin['name']}, head {compass_name(points[1][0] - points[0][0], points[1][1] - points[0][1])}."]
    last_bearing = bearing(points[0], points[1])
    distance_since_turn = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
    for index in range(1, len(points) - 1):
        next_bearing = bearing(points[index], points[index + 1])
        delta = (next_bearing - last_bearing + 540.0) % 360.0 - 180.0
        if abs(delta) >= turn_threshold:
            turn = "right" if delta > 0 else "left"
            directions.append(f"After about {round(distance_since_turn):.0f} metres, turn {turn} and head {compass_name(points[index + 1][0] - points[index][0], points[index + 1][1] - points[index][1])}.")
            distance_since_turn = 0.0
        distance_since_turn += math.hypot(points[index + 1][0] - points[index][0], points[index + 1][1] - points[index][1])
        last_bearing = next_bearing
    directions.append(f"Continue for about {round(distance_since_turn):.0f} metres to {route.destination['name']}.")
    return directions
