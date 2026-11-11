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
import re
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
    validate_links(data)
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
                text = _clean_text(" ".join(str(span["text"]).strip() for span in spans)).strip(" .")
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


def _easyocr_candidates(path):
    """[(box, text, confidence)] from EasyOCR at native size and enlarged 3x."""
    global _OCR_READER
    import easyocr
    import numpy as np
    from PIL import Image, ImageEnhance, ImageOps
    if _OCR_READER is None:
        _OCR_READER = easyocr.Reader(["en"], gpu=False, download_enabled=False)
    with Image.open(path) as image:
        pixel_width, pixel_height = image.size
        source = image.convert("RGB")
    # A second, enlarged pass makes tiny directory-map text occupy enough
    # pixels to retain its letter shapes.  It does not move the map.
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
    return [([[point[0] / scale, point[1] / scale] for point in box], raw_text, float(confidence))
            for results, scale in passes for box, raw_text, confidence in results]


# Windows' OCR reads small print once a picture is enlarged to about this
# many pixels across: the 950-pixel Capalaba Park map needed 4x (29 labels,
# against 5 at 2x), while the 2105-pixel Brisbane Airport page read 99
# labels at 2x in 1 second, against 101 at 4x in nearly 3.
_OCR_TARGET_PIXELS = 4000


def _windows_ocr_passes(longest_side):
    """[(scale, confidence)]: the enlargement that suits the picture, one step
    smaller, and its own size.  Windows' OCR gives no confidence, so larger
    passes are trusted more."""
    best = max(1, min(4, round(_OCR_TARGET_PIXELS / max(1, longest_side))))
    passes = [(best, 0.90)]
    if best > 2:
        passes.append((best - 1, 0.88))
    if best > 1:
        passes.append((1, 0.80))
    return passes


def _windows_ocr_candidates(path):
    """[(box, text, confidence)] from the OCR built into Windows 10 and 11.

    It needs no download and works offline.  The picture is read enlarged,
    in colour (see _windows_ocr_passes), since small labels are often only
    read once enlarged.  Bicubic enlargement read cleanly where Lanczos made
    the OCR think the map was tilted and misread it.
    """
    import asyncio
    import tempfile
    from PIL import Image
    try:
        # The small per-namespace winrt packages (about 0.4 MB) that ship
        # with Map in a Box; winsdk (38 MB) is the same API for developers.
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage import FileAccessMode, StorageFile
    except ImportError:
        from winsdk.windows.globalization import Language
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage import FileAccessMode, StorageFile

    engine = (OcrEngine.try_create_from_user_profile_languages()
              or OcrEngine.try_create_from_language(Language("en")))
    if engine is None:
        raise ImportError("Windows has no text recognition language installed.")
    limit = int(OcrEngine.max_image_dimension or 4096)

    async def read(image_path):
        file = await StorageFile.get_file_from_path_async(image_path)
        stream = await file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        return await engine.recognize_async(bitmap)

    with Image.open(path) as image:
        source = image.convert("RGB")
    candidates = []
    with tempfile.TemporaryDirectory() as folder:
        for scale, confidence in _windows_ocr_passes(max(source.size)):
            if max(source.size) * scale > limit:
                continue
            prepared = source if scale == 1 else source.resize(
                (source.width * scale, source.height * scale), Image.Resampling.BICUBIC)
            image_path = os.path.join(folder, f"ocr_{scale}.png")
            prepared.save(image_path)
            result = asyncio.run(read(os.path.abspath(image_path)))
            # Floor plans are drawn square.  A pass that believes the text is
            # tilted reports garbled words at rotated positions.
            if result.text_angle is not None and abs(result.text_angle) > 1.0:
                continue
            for line in result.lines:
                rects = [word.bounding_rect for word in line.words]
                if not rects:
                    continue
                left = min(r.x for r in rects) / scale
                top = min(r.y for r in rects) / scale
                right = max(r.x + r.width for r in rects) / scale
                bottom = max(r.y + r.height for r in rects) / scale
                box = [[left, top], [right, top], [right, bottom], [left, bottom]]
                candidates.append((box, line.text, confidence))
    return candidates


def _fix_shop_code(text):
    """"KIO" -> "K10": in a letter-then-number code, OCR reads 1 as I or l
    and 0 as O."""
    if (2 <= len(text) <= 5 and text[0].isalpha()
            and all(ch in "0123456789IOl" for ch in text[1:])
            and any(ch in "IOl" for ch in text[1:])):
        return text[0] + text[1:].translate(str.maketrans("IOl", "101"))
    return text


def _readable_label(text):
    """Keep text that looks like a real label, not an OCR misreading."""
    if not text or "�" in text:
        return False
    if text.isdigit():
        return True            # shop and room numbers, even "7"
    if len(text) < 2:
        return False
    plain = sum(ch.isalnum() or ch in " &'-/()" for ch in text)
    if plain / len(text) < 0.8 or not any(ch.isalpha() for ch in text):
        return False
    # Long words running lowercase letters into digits ("Wodv.ortt6") are
    # misreadings; real labels write "Level 2" or "4WD".
    return not any(len(word) >= 5 and any(c.islower() for c in word)
                   and any(c.isdigit() for c in word) for word in text.split())


def detect_image_labels(path, map_width, map_height, id_prefix="label", min_confidence=0.40):
    """Create provisional positioned labels from the text printed on a picture.

    Uses the OCR built into Windows, or EasyOCR where installed.  Raises
    ImportError when neither is available, so callers can tell the creator to
    add places by hand.  Labels are suggestions for the creator to check.
    """
    from PIL import Image
    with Image.open(path) as image:
        pixel_width, pixel_height = image.size
    try:
        candidates = _windows_ocr_candidates(path)
    except Exception as windows_error:
        try:
            candidates = _easyocr_candidates(path)
        except ImportError:
            raise ImportError(f"No text recognition available: {windows_error}") from windows_error

    # Prefer the most confident reading where passes found the same text
    # region, while retaining separate nearby labels.
    labels, seen, accepted_bounds = [], set(), []
    for box, raw_text, confidence in sorted(
            candidates, key=lambda item: item[2], reverse=True):
        text = _fix_shop_code(" ".join(str(raw_text).split()).strip(" ."))
        key = text.casefold()
        if confidence < min_confidence or not _readable_label(text) or key in seen:
            continue
        left = min(point[0] for point in box)
        top = min(point[1] for point in box)
        right = max(point[0] for point in box)
        bottom = max(point[1] for point in box)
        area = max(1.0, (right - left) * (bottom - top))
        overlaps_existing = False
        centre = ((left + right) / 2, (top + bottom) / 2)
        for old_left, old_top, old_right, old_bottom, old_area in accepted_bounds:
            intersection = max(0.0, min(right, old_right) - max(left, old_left)) * max(
                0.0, min(bottom, old_bottom) - max(top, old_top))
            # A weaker reading centred on a label already kept is another
            # pass's misreading of it ("woolwoytre" for "Woolworths").
            centred_on = (old_left <= centre[0] <= old_right
                          and old_top <= centre[1] <= old_bottom)
            if intersection / min(area, old_area) >= 0.60 or centred_on:
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


def near_distance(floor):
    """How near a place must be to a path to count as on it, for the map's
    size: 8 metres on a campus, 60 centimetres in a classroom."""
    return min(8.0, max(0.3, 0.02 * max(floor.get("width", 0), floor.get("height", 0))))


def build_graph(data, snap_distance=None):
    """Build a graph from free-hand strokes, crossings, and labelled places."""
    if snap_distance is None:
        snap_distance = near_distance(data)
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


def route_directions(route, turn_threshold=30.0, unit_system="metric"):
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
            directions.append(f"After about {format_map_distance(distance_since_turn, unit_system)}, turn {turn} and head {compass_name(points[index + 1][0] - points[index][0], points[index + 1][1] - points[index][1])}.")
            distance_since_turn = 0.0
        distance_since_turn += math.hypot(points[index + 1][0] - points[index][0], points[index + 1][1] - points[index][1])
        last_bearing = next_bearing
    directions.append(f"Continue for about {format_map_distance(distance_since_turn, unit_system)} to {route.destination['name']}.")
    return directions


# ---------------------------------------------------------------------------
# Authoring helpers
#
# Map coordinates are metres: X east, Y north.  The builder speaks and accepts
# distances in the user's chosen units (metres, or feet and inches), works out
# positions from "20 metres north-east of the Library", and keeps a drawn-only
# floor just large enough for what is on it.
# ---------------------------------------------------------------------------

FEET_PER_METRE = 3.280839895
COMPASS_DIRECTIONS = ("north", "north-east", "east", "south-east",
                      "south", "south-west", "west", "north-west")
# Margin around a drawn-only floor, in metres.
FIT_MARGIN_M = 5.0


def format_map_distance(metres, unit_system="metric"):
    """Say a short indoor or campus distance.

    Metric: "2.5 metres" below 10 m, whole metres above.  Imperial: feet and
    inches below 30 feet ("8 feet 6 inches"), whole feet above.
    """
    metres = max(0.0, float(metres or 0.0))
    if unit_system == "imperial":
        total_inches = int(round(metres * FEET_PER_METRE * 12))
        feet, inches = divmod(total_inches, 12)
        if feet >= 30:
            feet = int(round(metres * FEET_PER_METRE))
            return f"{feet} feet"
        foot_text = f"{feet} {'foot' if feet == 1 else 'feet'}" if feet else ""
        inch_text = f"{inches} {'inch' if inches == 1 else 'inches'}" if inches else ""
        return " ".join(t for t in (foot_text, inch_text) if t) or "0 feet"
    if metres < 10:
        value = round(metres, 1)
        text = f"{value:g}"
        return f"{text} {'metre' if value == 1 else 'metres'}"
    value = int(round(metres))
    return f"{value} metres"


_UNIT_WORDS = {
    "m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
    "cm": 0.01, "km": 1000.0,
    "ft": 1 / FEET_PER_METRE, "foot": 1 / FEET_PER_METRE, "feet": 1 / FEET_PER_METRE,
    "'": 1 / FEET_PER_METRE,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
}


def parse_distance(text, unit_system="metric"):
    """Read a typed distance as metres, or None.

    A bare number is in the user's units (metres, or feet).  Units may be
    given and mixed: "12", "12.5 m", "40 ft", "6'4\\"", "6 feet 4 inches".
    """
    import re
    text = str(text or "").strip().lower().replace(",", ".")
    if not text:
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+|'|\")?", text)
    if not parts or re.sub(r"[\d.\s]|[a-z]+|'|\"", "", text):
        return None
    default = 1.0 if unit_system != "imperial" else 1 / FEET_PER_METRE
    total = 0.0
    for number, unit in parts:
        if unit and unit not in _UNIT_WORDS:
            return None
        total += float(number) * (_UNIT_WORDS[unit] if unit else default)
    return total


def unit_label(unit_system="metric"):
    return "feet" if unit_system == "imperial" else "metres"


def direction_vector(direction):
    """(dx, dy) for a compass direction name, e.g. "north-east"."""
    index = COMPASS_DIRECTIONS.index(direction)
    angle = math.radians(index * 45.0)
    # Rounded so "20 metres east" lands exactly east, not 1e-15 north.
    return round(math.sin(angle), 12), round(math.cos(angle), 12)


def place_by_id(floor, place_id):
    return next((p for p in floor.get("places", []) if p["id"] == place_id), None)


def _new_place_id(floor):
    used = {p["id"] for p in floor.get("places", [])}
    number = len(used) + 1
    while f"place-{number}" in used:
        number += 1
    return f"place-{number}"


def add_place(floor, name, x, y, description=""):
    """Add a named place at X/Y metres; the first place becomes the start."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("A place needs a name.")
    place = {"id": _new_place_id(floor), "name": name,
             "description": str(description or "").strip(),
             "x": float(x), "y": float(y)}
    floor.setdefault("places", []).append(place)
    if not floor.get("start"):
        floor["start"] = place["id"]
    return place


def position_from(floor, reference_id, distance_m, direction):
    """X/Y that is ``distance_m`` metres in ``direction`` from a place."""
    reference = place_by_id(floor, reference_id)
    if reference is None:
        raise ValueError("Choose the place to measure from.")
    dx, dy = direction_vector(direction)
    return reference["x"] + dx * float(distance_m), reference["y"] + dy * float(distance_m)


def connect_places(floor, first_id, second_id):
    """Add a straight walkable path between two places."""
    first, second = place_by_id(floor, first_id), place_by_id(floor, second_id)
    if first is None or second is None or first is second:
        raise ValueError("Choose two different places to connect.")
    path = {"points": [[first["x"], first["y"]], [second["x"], second["y"]]]}
    floor.setdefault("paths", []).append(path)
    return path


def move_place(floor, place_id, x, y):
    """Move a place, carrying the ends of paths that started or ended on it."""
    place = place_by_id(floor, place_id)
    if place is None:
        raise ValueError("That place no longer exists.")
    old = (place["x"], place["y"])
    for path in floor.get("paths", []):
        for point in (path["points"][0], path["points"][-1]):
            if math.hypot(point[0] - old[0], point[1] - old[1]) < 1e-6:
                point[0], point[1] = float(x), float(y)
    place["x"], place["y"] = float(x), float(y)


def delete_place(floor, place_id):
    floor["places"] = [p for p in floor.get("places", []) if p["id"] != place_id]
    if floor.get("start") == place_id:
        floor["start"] = floor["places"][0]["id"] if floor["places"] else ""


def fit_to_content(floor, margin=FIT_MARGIN_M, traced=False):
    """Size a drawn-only floor to its places and paths, shifting them so the
    map keeps a small margin on every side.  A floor traced over a picture is
    left alone: its coordinates belong to the picture."""
    if traced or floor.get("background"):
        return
    points = [(p["x"], p["y"]) for p in floor.get("places", [])]
    for collection in (floor.get("paths", []), floor.get("barriers", [])):
        for feature in collection:
            points.extend((x, y) for x, y in feature["points"])
    if not points:
        return
    min_x = min(x for x, _ in points)
    min_y = min(y for _, y in points)
    if floor.get("fixed_size"):
        # A floor drawn with the mouse keeps the size its creator gave, so
        # the drawing stays still; it only grows if something is placed
        # outside it, such as by a typed distance.
        max_x = max(x for x, _ in points)
        max_y = max(y for _, y in points)
        shift_x = margin - min_x if min_x < 0 else 0.0
        shift_y = margin - min_y if min_y < 0 else 0.0
        if shift_x or shift_y:
            _shift_floor(floor, shift_x, shift_y)
        if max_x + shift_x > floor["width"]:
            floor["width"] = max_x + shift_x + margin
        if max_y + shift_y > floor["height"]:
            floor["height"] = max_y + shift_y + margin
        return
    shift_x, shift_y = margin - min_x, margin - min_y
    if abs(shift_x) > 1e-9 or abs(shift_y) > 1e-9:
        for place in floor.get("places", []):
            place["x"] += shift_x
            place["y"] += shift_y
        for collection in (floor.get("paths", []), floor.get("barriers", [])):
            for feature in collection:
                for point in feature["points"]:
                    point[0] += shift_x
                    point[1] += shift_y
    floor["width"] = max(10.0, max(x for x, _ in points) + shift_x + margin)
    floor["height"] = max(10.0, max(y for _, y in points) + shift_y + margin)


def _shift_floor(floor, dx, dy):
    for place in floor.get("places", []):
        place["x"] += dx
        place["y"] += dy
    for collection in (floor.get("paths", []), floor.get("barriers", [])):
        for feature in collection:
            for point in feature["points"]:
                point[0] += dx
                point[1] += dy


def set_drawing_size(floor, width, aspect):
    """Fix a hand-drawn floor's real size: ``width`` metres across and
    ``aspect`` (height / width) to suit the drawing area.  Anything already
    on it keeps its position and the floor stays big enough to hold it."""
    floor["width"] = max(float(width), floor.get("width", 0) if floor.get("places") else 0)
    floor["height"] = max(floor["width"] * aspect,
                          floor.get("height", 0) if floor.get("places") else 0)
    floor["fixed_size"] = True


def attach_to_paths(floor, x, y, max_distance, skip_path=None):
    """The point on a path nearest X/Y, added to that path as a corner so
    other lines can meet it there, or None when no path is within
    ``max_distance``.  ``skip_path`` (a path dict) is not considered."""
    best = None
    for path in floor.get("paths", []):
        if path is skip_path:
            continue
        points = path["points"]
        for index, (a, b) in enumerate(zip(points, points[1:])):
            (qx, qy), t = _project((x, y), tuple(a), tuple(b))
            distance = math.hypot(qx - x, qy - y)
            if distance <= max_distance and (best is None or distance < best[0]):
                best = (distance, path, index, t, (qx, qy))
    if best is None:
        return None
    _distance, path, index, t, (qx, qy) = best
    if 1e-6 < t < 1 - 1e-6:
        path["points"].insert(index + 1, [qx, qy])
    else:
        qx, qy = path["points"][index if t <= 1e-6 else index + 1]
    return qx, qy


def join_place_to_paths(floor, place, max_distance):
    """Join a place to the nearest path within ``max_distance`` with a short
    path to its door.  Returns True when joined (or already on a path)."""
    point = attach_to_paths(floor, place["x"], place["y"], max_distance)
    if point is None:
        return False
    if math.hypot(point[0] - place["x"], point[1] - place["y"]) > 1e-6:
        floor["paths"].append({"points": [[place["x"], place["y"]], [point[0], point[1]]],
                               "spur": place["id"]})
    return True


def remove_spurs(floor, place_id):
    """Drop the short paths that joined a place to a walkway, before it is
    moved or deleted."""
    floor["paths"] = [p for p in floor.get("paths", []) if p.get("spur") != place_id]


def join_drawn_path(floor, path, max_distance):
    """Make a freshly drawn path meet what it was drawn to: each end goes to
    a place or path within ``max_distance``, and places beside the path are
    joined to it.  Returns the names of the places joined."""
    points = path["points"]
    joined = []
    for index in (0, -1):
        x, y = points[index]
        near = [p for p in floor.get("places", [])
                if math.hypot(p["x"] - x, p["y"] - y) <= max_distance]
        if near:
            place = min(near, key=lambda p: math.hypot(p["x"] - x, p["y"] - y))
            points[index] = [place["x"], place["y"]]
            joined.append(place["name"])
            continue
        point = attach_to_paths(floor, x, y, max_distance, skip_path=path)
        if point is not None:
            points[index] = [point[0], point[1]]
    coords, _edges, place_nodes = build_graph(floor, snap_distance=0.0)
    for place in floor.get("places", []):
        if place["id"] in place_nodes or place["name"] in joined:
            continue
        on_this = attach_to_paths({"paths": [path]}, place["x"], place["y"], max_distance)
        if on_this is not None:
            if math.hypot(on_this[0] - place["x"], on_this[1] - place["y"]) > 1e-6:
                floor["paths"].append({"points": [[place["x"], place["y"]], list(on_this)],
                                       "spur": place["id"]})
            joined.append(place["name"])
    return joined


def scale_floor(floor, factor):
    """Scale a floor's coordinates, e.g. after the creator gives its real width."""
    factor = float(factor)
    floor["width"] *= factor
    floor["height"] *= factor
    for place in floor.get("places", []):
        place["x"] *= factor
        place["y"] *= factor
    for collection in (floor.get("paths", []), floor.get("barriers", [])):
        for feature in collection:
            for point in feature["points"]:
                point[0] *= factor
                point[1] *= factor


def _path_end_place(floor, point, within=None):
    if within is None:
        within = near_distance(floor)
    best = None
    for place in floor.get("places", []):
        distance = math.hypot(place["x"] - point[0], place["y"] - point[1])
        if distance <= within and (best is None or distance < best[0]):
            best = (distance, place)
    return best[1] if best else None


def path_length(path):
    points = path["points"]
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def describe_path(floor, path, unit_system="metric"):
    """"Library to Hall, 25 metres", naming ends that sit on places."""
    others = {"paths": [p for p in floor.get("paths", []) if p is not path]}
    names = []
    for point in (path["points"][0], path["points"][-1]):
        place = _path_end_place(floor, point)
        gap = distance_to_paths(others, *point)
        if place:
            names.append(place["name"])
        elif gap is not None and gap < 1e-6:
            names.append("another path")
        else:
            names.append("open space")
    return f"{names[0]} to {names[1]}, {format_map_distance(path_length(path), unit_system)}"


def describe_place(floor, place, unit_system="metric"):
    """"Library, 20 metres north-east of Entrance" — relative to the start."""
    start = place_by_id(floor, floor.get("start"))
    text = place["name"]
    if place.get("id") == floor.get("start"):
        text += " (start)"
    elif start is not None:
        dx, dy = place["x"] - start["x"], place["y"] - start["y"]
        distance = math.hypot(dx, dy)
        if distance >= 0.05:
            text += (f", {format_map_distance(distance, unit_system)} "
                     f"{compass_name(dx, dy)} of {start['name']}")
    if place.get("description"):
        text += f". {place['description']}"
    return text


def short_list(names, limit=6):
    """"A, B, C and 12 more": long lists stay readable in speech."""
    names = list(names)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def check_floor(floor):
    """Problems that would stop students getting directions, in plain words."""
    problems = []
    places = floor.get("places", [])
    if len(places) < 2:
        problems.append("Add at least two places so students have somewhere to go.")
        return problems
    names = [p["name"].casefold() for p in places]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        problems.append(f"More than one place is called {short_list(repeated)}; "
                        "give each a different name.")
    coords, edges, place_nodes = build_graph(floor)
    off_path = [p["name"] for p in places if p["id"] not in place_nodes]
    if off_path:
        problems.append(
            f"{len(off_path)} {'place is' if len(off_path) == 1 else 'places are'} not on a "
            f"path, so directions cannot reach: {short_list(off_path)}. Join them to another "
            "place, or delete any that are not real places.")
    start = place_by_id(floor, floor.get("start")) or places[0]
    if start["id"] in place_nodes:
        reached, queue = {place_nodes[start["id"]]}, [place_nodes[start["id"]]]
        while queue:
            node = queue.pop()
            for neighbour, _weight in edges.get(node, []):
                if neighbour not in reached:
                    reached.add(neighbour)
                    queue.append(neighbour)
        cut_off = [p["name"] for p in places
                   if p["id"] in place_nodes and place_nodes[p["id"]] not in reached]
        if cut_off:
            problems.append(
                f"Paths from {start['name']} do not reach {len(cut_off)} "
                f"{'place' if len(cut_off) == 1 else 'places'}: {short_list(cut_off)}. "
                "Join them to a place that is reached.")
    return problems


_FLOOR_KEYS = ("width", "height", "start", "places", "paths", "background",
               "source_text", "source_page", "barriers", "fixed_size")


def add_floor(data, name=""):
    """Add a blank floor the size of the last one, turning a one-floor map
    into a multi-floor one first.  Returns the new floor."""
    if not data.get("floors"):
        first = {key: data.pop(key) for key in _FLOOR_KEYS if key in data}
        first["name"] = "Floor 1"
        data["floors"] = [first]
    floors = data["floors"]
    template = floors[-1]
    floor = new_floor(name or f"Floor {len(floors) + 1}",
                      template["width"], template["height"], len(floors))
    floors.append(floor)
    return floor


# ---------------------------------------------------------------------------
# Stairs, lifts and other links between floors
#
# A link joins a place on one floor to a place on another:
#   {"id": "link-1", "kind": "stairs",
#    "a": {"floor": 0, "place": "place-3"}, "b": {"floor": 1, "place": "place-1"}}
# Routes may change floor through links, and a student standing at a linked
# place changes floor to the place it leads to.
# ---------------------------------------------------------------------------

LINK_KINDS = ("stairs", "lift", "ramp", "escalator")
# What a change of floor costs a route, in metres of walking, so a route
# does not wander up and down to save a few steps.
LINK_COST_M = {"stairs": 15.0, "lift": 20.0, "ramp": 10.0, "escalator": 12.0}


def links_for(data):
    return (data.get("links") or []) if isinstance(data, dict) else []


def validate_links(data):
    """Keep links whose floors and places exist; drop the rest quietly."""
    floors = floors_for(data)
    valid = []
    for link in data.get("links") or []:
        try:
            ends = [link["a"], link["b"]]
            if link.get("kind") not in LINK_KINDS:
                continue
            ok = True
            for end in ends:
                floor_index = int(end["floor"])
                end["floor"] = floor_index
                if not 0 <= floor_index < len(floors) or place_by_id(floors[floor_index], end["place"]) is None:
                    ok = False
            if ok and ends[0]["floor"] != ends[1]["floor"]:
                valid.append(link)
        except (KeyError, TypeError, ValueError):
            continue
    if valid or "links" in data:
        data["links"] = valid
    return data


def add_link(data, kind, floor_a, place_a, floor_b, place_b):
    """Join a place on one floor to a place on another with stairs, a lift..."""
    if kind not in LINK_KINDS:
        raise ValueError("Choose stairs, lift, ramp or escalator.")
    if floor_a == floor_b:
        raise ValueError("Stairs and lifts join two different floors.")
    floors = floors_for(data)
    if place_by_id(floors[floor_a], place_a) is None or place_by_id(floors[floor_b], place_b) is None:
        raise ValueError("Choose a place on each floor.")
    used = {link["id"] for link in links_for(data)}
    number = len(used) + 1
    while f"link-{number}" in used:
        number += 1
    link = {"id": f"link-{number}", "kind": kind,
            "a": {"floor": int(floor_a), "place": place_a},
            "b": {"floor": int(floor_b), "place": place_b}}
    data.setdefault("links", []).append(link)
    return link


def remove_links_to_place(data, floor_index, place_id):
    """Drop links that end at a deleted place."""
    if "links" in data:
        data["links"] = [
            link for link in data["links"]
            if not any(end["floor"] == floor_index and end["place"] == place_id
                       for end in (link["a"], link["b"]))]


def links_at(data, floor_index, place_id):
    """[(link, other_end)] for links touching a place."""
    found = []
    for link in links_for(data):
        for end, other in ((link["a"], link["b"]), (link["b"], link["a"])):
            if end["floor"] == floor_index and end["place"] == place_id:
                found.append((link, other))
    return found


def floor_name(data, index):
    floors = floors_for(data)
    if "floors" not in data:
        return "Floor 1"
    return floors[index].get("name") or f"Floor {index + 1}"


def describe_link(data, link):
    """"Stairs: Stairwell (Ground) to Landing (Level 1)"."""
    floors = floors_for(data)
    parts = []
    for end in (link["a"], link["b"]):
        place = place_by_id(floors[end["floor"]], end["place"])
        parts.append(f"{place['name'] if place else '?'} ({floor_name(data, end['floor'])})")
    return f"{link['kind'].capitalize()}: {parts[0]} to {parts[1]}"


def link_phrase(data, link, other):
    """What a student hears at a linked place: "Lift to Level 1"."""
    return f"{link['kind'].capitalize()} to {floor_name(data, other['floor'])}"


@dataclass
class MultiFloorRoute:
    """A route made of walking legs, one per floor, joined by links."""
    legs: list          # [(floor_index, Route)]
    changes: list       # [(link, from_floor, to_floor)] between legs
    distance: float
    origin: dict
    destination: dict


def find_route_between_floors(data, origin, destination):
    """Route from (floor, place_id) to (floor, place_id), changing floor at links.

    Same-floor trips behave exactly like find_route.  Raises ValueError with a
    plain explanation when no route exists.
    """
    floors = floors_for(data)
    graphs = [build_graph(floor) for floor in floors]
    (start_floor, start_place), (goal_floor, goal_place) = origin, destination
    for floor_index, place_id in (origin, destination):
        if place_id not in graphs[floor_index][2]:
            place = place_by_id(floors[floor_index], place_id)
            raise ValueError(f"{place['name'] if place else 'That place'} is not on a path.")
    start = (start_floor, graphs[start_floor][2][start_place])
    goal = (goal_floor, graphs[goal_floor][2][goal_place])
    # Link edges between the graph nodes of their two places.
    hops = {}
    for link in links_for(data):
        ends = []
        for end in (link["a"], link["b"]):
            node = graphs[end["floor"]][2].get(end["place"])
            if node is None:
                break
            ends.append((end["floor"], node))
        if len(ends) == 2:
            cost = LINK_COST_M.get(link["kind"], 15.0)
            hops.setdefault(ends[0], []).append((ends[1], cost, link))
            hops.setdefault(ends[1], []).append((ends[0], cost, link))
    distances, previous = {start: 0.0}, {}
    queue = [(0.0, start)]
    while queue:
        distance, state = heapq.heappop(queue)
        if distance != distances.get(state):
            continue
        if state == goal:
            break
        floor_index, node = state
        steps = [((floor_index, n), w, None) for n, w in graphs[floor_index][1].get(node, [])]
        steps += hops.get(state, [])
        for neighbour, weight, link in steps:
            candidate = distance + weight
            if candidate < distances.get(neighbour, float("inf")):
                distances[neighbour] = candidate
                previous[neighbour] = (state, link)
                heapq.heappush(queue, (candidate, neighbour))
    if goal not in distances:
        raise ValueError("Those places are not connected by the drawn paths, stairs or lifts.")
    chain, state = [goal], goal
    while state != start:
        state, _link = previous[state]
        chain.append(state)
    chain.reverse()
    legs, changes, current = [], [], [chain[0]]
    for state in chain[1:]:
        link = previous[state][1]
        if link is not None:
            legs.append(current)
            changes.append((link, current[-1][0], state[0]))
            current = [state]
        else:
            current.append(state)
    legs.append(current)
    origin_place = place_by_id(floors[start_floor], start_place)
    goal_place_data = place_by_id(floors[goal_floor], goal_place)
    route_legs, walked = [], 0.0
    for leg in legs:
        floor_index = leg[0][0]
        coords = graphs[floor_index][0]
        points = [coords[node] for _f, node in leg]
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))
        walked += length
        first = _place_at(floors[floor_index], points[0]) or origin_place
        last = _place_at(floors[floor_index], points[-1]) or goal_place_data
        route_legs.append((floor_index, Route(points, length, first, last)))
    return MultiFloorRoute(route_legs, changes, walked, origin_place, goal_place_data)


def _place_at(floor, point, within=8.0):
    best = None
    for place in floor.get("places", []):
        distance = math.hypot(place["x"] - point[0], place["y"] - point[1])
        if distance <= within and (best is None or distance < best[0]):
            best = (distance, place)
    return best[1] if best else None


def multi_floor_directions(data, route, unit_system="metric"):
    """Directions for a route that may change floor."""
    lines = []
    for index, (floor_index, leg) in enumerate(route.legs):
        if len(route.legs) > 1:
            lines.append(f"On {floor_name(data, floor_index)}:")
        if len(leg.points) >= 2 or index == len(route.legs) - 1:
            lines.extend(route_directions(leg, unit_system=unit_system))
        if index < len(route.changes):
            link, _from, to_floor = route.changes[index]
            lines.append(f"Take the {link['kind']} to {floor_name(data, to_floor)}.")
    return lines


def check_links(data):
    """Map-wide problems with floors that are not joined."""
    floors = floors_for(data)
    if len(floors) < 2:
        return []
    joined = {0}
    changed = True
    while changed:
        changed = False
        for link in links_for(data):
            ends = {link["a"]["floor"], link["b"]["floor"]}
            if ends & joined and not ends <= joined:
                joined |= ends
                changed = True
    missing = [floor_name(data, i) for i in range(len(floors)) if i not in joined]
    if missing:
        return [f"No stairs or lift reach {', '.join(missing)}. Add stairs or a lift so "
                "directions can go between floors."]
    return []


def merge_labels(primary, extra, map_width, id_prefix="label", near_ratio=0.02):
    """Add labels from ``extra`` that ``primary`` does not already have.

    A PDF's own text is exact; OCR of the same page adds what was drawn as
    artwork ("Train Station", "Taxi" on the Brisbane Airport map).  An extra
    label is skipped when its text is already present or it sits on a
    primary label.
    """
    merged = list(primary)
    names = {label["name"].casefold() for label in primary}
    near = map_width * near_ratio
    for label in extra:
        if label["name"].casefold() in names:
            continue
        if any(math.hypot(label["x"] - p["x"], label["y"] - p["y"]) <= near for p in primary):
            continue
        names.add(label["name"].casefold())
        merged.append(dict(label, id=f"{id_prefix}-{len(merged) + 1}"))
    return merged


# ---------------------------------------------------------------------------
# Map legends
#
# Many venue maps mark shops and services with numbers and name them in a
# legend: "50. Mi Casa", "56. Yo! Sushi".  Reading the legend names the
# numbered places and removes the legend's own text from the places list.
# ---------------------------------------------------------------------------

_LEGEND_LINE = re.compile(r"^(\d{1,3})\.\s*(.*)$")
_LEGEND_INLINE = re.compile(r"(?<![\d.])(\d{1,3})\.\s+(.+?)(?=\s+\d{1,3}\.\s|$)")
_LEGEND_IN_NAME = re.compile(r"(?<![\d.])\d{1,3}\.\s+\S")


def _clean_text(text):
    text = re.sub(r"[\x00-\x1f\xa0]", " ", str(text or ""))
    return " ".join(text.split())


def legend_names(source_text="", places=()):
    """{number: name} from a map's legend, where each number has one name.

    Legend lines come from the PDF's text ("50.\tMi Casa"), with a name
    that wraps onto the next line joined back up ("8. Newstead" then
    "Brewing Co."), and from labels holding several entries in one line
    ("1. The Coffee Club 2. Trader & Co").
    """
    found = {}
    current, carry = None, False
    for raw in str(source_text or "").splitlines():
        line = _clean_text(raw)
        match = _LEGEND_LINE.match(line)
        if match:
            current = match.group(1)
            found.setdefault(current, []).append(match.group(2).strip())
            carry = raw.endswith((" ", "\t", "\xa0")) or not match.group(2).strip()
        elif current and carry and line and not line.isdigit():
            found[current][-1] = f"{found[current][-1]} {line}".strip()
            carry = raw.endswith((" ", "\t", "\xa0"))
        else:
            current, carry = None, False
    for place in places:
        for number, name in _LEGEND_INLINE.findall(_clean_text(place.get("name"))):
            if number not in found:
                found[number] = [name.strip()]
    legend = {}
    for number, names in found.items():
        names = {n.strip(" .") for n in names if n.strip(" .")}
        if len(names) == 1:
            legend[number] = names.pop()
    return legend


def _tell_repeats_apart(places):
    """A chain with several outlets shares one legend number, such as three
    news@bne (20) shops.  Name each by its nearest uniquely named place:
    "news@bne by Mi Casa"."""
    names = [place["name"] for place in places]
    unique = [place for place in places if names.count(place["name"]) == 1]
    if not unique:
        return
    for place in places:
        if names.count(place["name"]) > 1:
            nearest = min(unique, key=lambda other: math.hypot(other["x"] - place["x"],
                                                                other["y"] - place["y"]))
            place["name"] = f"{place['name']} by {nearest['name']}"


def has_legend(floor, source_text=None):
    """True when the floor's map numbers its places and lists them in a key."""
    text = floor.get("source_text", "") if source_text is None else source_text
    return len(legend_names(text, floor.get("places", []))) >= 3


def apply_legend(floor, source_text=None, show_keys=False):
    """Name numbered places from the legend and drop the legend's own text.

    Each named place keeps its key number in ``place["key"]``; with
    ``show_keys`` the name says it too, "Mi Casa (key 50)", for a creator who
    wants to match places to the paper map.  Most listeners find the numbers
    distracting beside gate numbers, so they are left out by default.

    Returns (named, removed).  Nothing changes when fewer than three legend
    entries are found: a few numbered lines are not a legend.
    """
    places = floor.get("places", [])
    legend = legend_names(floor.get("source_text", "") if source_text is None else source_text,
                          places)
    if len(legend) < 3:
        return 0, 0
    legend_words = [name.casefold() for name in legend.values()]
    named, kept = 0, []
    for place in places:
        name = _clean_text(place["name"])
        if name.isdigit() and name in legend:
            place["name"] = f"{legend[name]} (key {name})" if show_keys else legend[name]
            place["key"] = name
            named += 1
            kept.append(place)
            continue
        folded = name.casefold().strip(" .")
        is_legend_text = bool(_LEGEND_IN_NAME.search(name)) or (
            len(folded) >= 4 and not name.isdigit()
            and any(folded in entry for entry in legend_words))
        if not is_legend_text:
            kept.append(place)
    removed = len(places) - len(kept)
    floor["places"] = kept
    _tell_repeats_apart(kept)
    if floor.get("start") and not place_by_id(floor, floor["start"]):
        floor["start"] = kept[0]["id"] if kept else ""
    return named, removed


# -- guessing a new map's name and size ------------------------------------------

# Neighbouring labels on a shopping centre, airport or campus plan are about
# this far apart in real life: a shop front, a gate lounge, a classroom.
TYPICAL_LABEL_SPACING_M = 12.0
_GENERIC_FILE_WORDS = {"scan", "image", "img", "document", "doc", "download", "untitled",
                       "map", "copy", "final", "pdf"}


def _title_words(text):
    return [w for w in re.split(r"[\s_\-]+", text) if w]


def title_from_file(path):
    """"Brisbane-Airport-Map_Level2.pdf" becomes "Brisbane Airport Map Level2"."""
    words = _title_words(os.path.splitext(os.path.basename(str(path or "")))[0])
    if not words or all(re.sub(r"[^a-z]", "", w.casefold()) in _GENERIC_FILE_WORDS | {""}
                        for w in words):
        return ""
    # "capalaba park map" reads better as "Capalaba Park Map"; "BCEC" stays.
    return " ".join(w[0].upper() + w[1:] for w in words)


def pdf_title(path, page_index=0):
    """A PDF map's title: its document title, or the largest text on the page,
    in reading order ("Domestic Terminal Level 2 Qantas Area Retail and
    Services Map").  Empty when there is none."""
    import fitz
    with fitz.open(path) as document:
        title = _clean_text((document.metadata or {}).get("title", ""))
        if (len(title.split()) >= 2 and not title.lower().startswith("microsoft")
                and not title.lower().endswith((".doc", ".docx", ".pdf", ".indd"))):
            return title
        if not 0 <= page_index < document.page_count:
            return ""
        spans = []
        for block in document.load_page(page_index).get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = _clean_text(span.get("text"))
                    if text:
                        spans.append((float(span.get("size", 0)), span["bbox"], text))
    if not spans:
        return ""
    sizes = sorted(size for size, _box, _text in spans)
    biggest = sizes[-1]
    if biggest < 1.3 * sizes[(len(sizes) - 1) // 2]:
        return ""       # no text stands out as a title
    lines = sorted((box[1], box[0], text) for size, box, text in spans
                   if size >= biggest * 0.95)
    return " ".join(text for _y, _x, text in lines)[:100].strip()


def suggest_map_name(path, page_index=0):
    """A name for a map made from a PDF or picture.  The PDF's title, with the
    place the file name adds in front ("Brisbane Airport" from the file
    name before "Domestic Terminal..." from the page), or the file name."""
    from_file = title_from_file(path)
    title = ""
    if str(path).lower().endswith(".pdf"):
        try:
            title = pdf_title(path, page_index)
        except Exception:
            title = ""
    if not title:
        return from_file
    title_words = {w.casefold().rstrip("s") for w in _title_words(title)}
    lead = []
    for word in _title_words(from_file):
        if word.casefold().rstrip("s") in title_words:
            break
        lead.append(word)
    if lead and len(lead) < len(_title_words(from_file)):
        return " ".join(lead + [title])
    return title


def pdf_scale_width(path, page_index=0):
    """The real width in metres of a PDF page printed to a stated scale
    ("Scale 1:500"), or None."""
    import fitz
    with fitz.open(path) as document:
        if not 0 <= page_index < document.page_count:
            return None
        page = document.load_page(page_index)
        match = re.search(r"\b1\s*:\s*(\d{2,3}(?:[ ,]?\d{3})*|\d{2,6})\b", page.get_text("text"))
        if not match:
            return None
        ratio = int(re.sub(r"[ ,]", "", match.group(1)))
        if not 50 <= ratio <= 50000:
            return None
        return page.rect.width / 72 * 0.0254 * ratio


def estimate_width_from_labels(floor, min_places=8):
    """Guess a traced floor's real width in metres from how closely its labels
    sit: neighbours are usually about TYPICAL_LABEL_SPACING_M apart.  None
    when there are too few labels to judge."""
    places = [p for p in floor.get("places", []) if len(p.get("name", "")) <= 40]
    if len(places) < min_places or not floor.get("width"):
        return None
    gaps = sorted(min(math.hypot(p["x"] - q["x"], p["y"] - q["y"])
                      for q in places if q is not p) for p in places)
    gap = gaps[len(gaps) // 2]
    if gap <= 0:
        return None
    return round_estimate(TYPICAL_LABEL_SPACING_M * floor["width"] / gap)


def round_estimate(metres):
    """Round a guessed width to a number that sounds like a guess: 274 is
    "270", 517 is "500"."""
    step = 10 if metres < 200 else 50 if metres < 2000 else 500
    return max(step, round(metres / step) * step)


# -- describing a student's position --------------------------------------------

def nearest_places(floor, x, y, count=1, exclude=()):
    """The ``count`` places nearest X/Y, nearest first."""
    places = [p for p in floor.get("places", []) if p["id"] not in exclude]
    return sorted(places, key=lambda p: math.hypot(p["x"] - x, p["y"] - y))[:count]


def distance_to_paths(floor, x, y):
    """How far X/Y is from the nearest drawn path, or None without paths."""
    best = None
    for path in floor.get("paths", []):
        points = path.get("points", [])
        for a, b in zip(points, points[1:]):
            (qx, qy), _t = _project((x, y), tuple(a), tuple(b))
            distance = math.hypot(qx - x, qy - y)
            if best is None or distance < best:
                best = distance
    return best


def place_bearing(place, x, y, unit_system="metric"):
    """"Mi Casa (50), 15 metres north-east" from X/Y."""
    dx, dy = place["x"] - x, place["y"] - y
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return f"{place['name']}, here"
    return (f"{place['name']}, {format_map_distance(distance, unit_system)} "
            f"{compass_name(dx, dy)}")


def describe_empty_spot(floor, x, y, cell_size, unit_system="metric"):
    """What a student hears on a square with no label: the nearest place,
    and whether they are on a walkway, so every arrow press says something
    useful ("Walkway. Near Mi Casa (50), 15 metres north-east.")."""
    parts = []
    to_path = distance_to_paths(floor, x, y)
    if to_path is not None:
        parts.append("Walkway." if to_path <= cell_size * 0.75 else "Off the walkways.")
    nearest = nearest_places(floor, x, y)
    if nearest:
        parts.append(f"Near {place_bearing(nearest[0], x, y, unit_system)}.")
    return " ".join(parts)


def describe_position(data, floor_index, x, y, cell_size, unit_system="metric"):
    """Where am I, on a map file: its name, the floor, what is here or near,
    and the next places around."""
    floor = floors_for(data)[floor_index]
    parts = [f"{data.get('name') or 'Map'}, {floor_name(data, floor_index)}."]
    here = places_in_grid_cell(floor, x, y, cell_size)
    if here:
        parts.append("At " + short_list([p["name"] for p in here], 3) + ".")
    else:
        parts.append(describe_empty_spot(floor, x, y, cell_size, unit_system))
    others = nearest_places(floor, x, y, count=4, exclude={p["id"] for p in here})
    others = others[1:] if not here else others[:3]
    if others:
        parts.append("Also near: " + "; ".join(place_bearing(p, x, y, unit_system)
                                               for p in others) + ".")
    return " ".join(part for part in parts if part)


def _good_landmark(name):
    """A name worth saying after "by": short, clean and not itself told apart
    by another place.  Misread labels such as "ooaoo" have no capital."""
    return (3 <= len(name) <= 32 and " by " not in name and name == _clean_text(name)
            and any(c.isupper() for c in name) and re.search(r"[A-Za-z]{3}", name) is not None)


def tell_symbols_apart(floor):
    """Name repeated symbols, such as three Public Toilets, by the nearest
    shop or gate: "Public Toilet by LEGO".  When two would still share a
    name, the second says which side: "Public Toilet north of LEGO"."""
    places = floor.get("places", [])
    names = [place["name"] for place in places]
    landmarks = [p for p in places if not p.get("symbol") and names.count(p["name"]) == 1
                 and _good_landmark(p["name"])]
    if not landmarks:
        return
    used = set(names)
    for place in places:
        if not place.get("symbol") or names.count(place["name"]) < 2:
            continue
        nearest = min(landmarks, key=lambda other: math.hypot(other["x"] - place["x"],
                                                               other["y"] - place["y"]))
        name = f"{place['symbol']} by {nearest['name']}"
        if name in used:
            side = compass_name(place["x"] - nearest["x"], place["y"] - nearest["y"])
            name = f"{place['symbol']} {side} of {nearest['name']}"
        used.add(name)
        place["name"] = name
