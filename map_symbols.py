"""Find the amenity symbols on a floor plan PDF from the map's own key.

Airport and shopping centre maps draw toilets, lifts, stairs and the like as
small symbols in white boxes, and list each symbol with its name in a key.
The key teaches the symbols: each one beside a name in the key is compared
with every other white box on the page, and a close match becomes a place
with that name, such as "Public Toilet".  Nothing is assumed about which
symbols a map uses.
"""

from __future__ import annotations

import numpy as np

# The page is scanned at this many pixels per PDF point.
SCAN_ZOOM = 4
# Symbol boxes are about this size on printed maps, in PDF points.
MIN_BOX_PT, MAX_BOX_PT = 5.0, 13.0
# How alike two symbols must be (normalised correlation, 1 is identical).
# Real matches score 0.8 to 1.0 on the Brisbane Airport maps.  A lower score
# still counts when it is far ahead of every other symbol in the key, as for
# a prayer room drawn a little differently; a symbol missing from the key,
# such as a men's toilet, scores about as well against two key symbols.
MATCH_SCORE = 0.8
CLEAR_MATCH_SCORE, CLEAR_LEAD = 0.6, 0.25
# A key lists at least this many symbols in a column.
MIN_KEY_ENTRIES = 3


def _white_boxes(gray, zoom):
    """Bounding boxes (x, y, w, h) of white regions the size of a symbol box."""
    mask = gray > 245
    height, width = mask.shape
    parent, boxes = [], []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    previous = []
    for y in range(height):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], mask[y].view(np.int8), [0]))))
        runs, j = [], 0
        for start, end in zip(edges[::2].tolist(), edges[1::2].tolist()):
            label = len(parent)
            parent.append(label)
            boxes.append([start, y, end, y + 1, end - start])
            # Runs are sorted, so the overlapping runs of the row above are
            # found by walking both rows together.
            while j < len(previous) and previous[j][1] <= start:
                j += 1
            k = j
            while k < len(previous) and previous[k][0] < end:
                a, b = find(label), find(previous[k][2])
                if a != b:
                    keep, drop = min(a, b), max(a, b)
                    parent[drop] = keep
                    box, other = boxes[keep], boxes[drop]
                    box[0], box[1] = min(box[0], other[0]), min(box[1], other[1])
                    box[2], box[3] = max(box[2], other[2]), max(box[3], other[3])
                    box[4] += other[4]
                k += 1
            runs.append((start, end, label))
        previous = runs
    found = []
    low, high = MIN_BOX_PT * zoom, MAX_BOX_PT * zoom
    for label, (x0, y0, x1, y1, area) in enumerate(boxes):
        if find(label) != label:
            continue
        w, h = x1 - x0, y1 - y0
        if low <= w <= high and low <= h <= high and 0.75 < w / h < 1.33 and area > 0.5 * w * h:
            found.append((x0, y0, w, h))
    return found


def _window(gray, box, size, pad):
    """The square ``size`` + 2 * ``pad`` pixels around a box's centre, scaled
    so the box is ``size`` pixels across.  None at the page edge."""
    x, y, w, h = box
    scale = size / ((w + h) / 2)
    half = (size / 2 + pad) / scale
    cx, cy = x + w / 2, y + h / 2
    x0, y0 = int(round(cx - half)), int(round(cy - half))
    x1, y1 = int(round(cx + half)), int(round(cy + half))
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
        return None
    crop = gray[y0:y1, x0:x1].astype(np.float32)
    full = size + 2 * pad
    if crop.shape != (full, full):
        from PIL import Image
        crop = np.asarray(Image.fromarray(crop).resize((full, full), Image.Resampling.BILINEAR))
    return crop


def _normalise(values):
    spread = values.std()
    return (values - values.mean()) / spread if spread > 1e-6 else None


def _similarity(window, template, pad):
    """Best correlation of a template with a window, letting the symbol sit
    up to ``pad`` pixels off centre: boxes found on the page can be a pixel
    or two out."""
    size = template.shape[0]
    best = -1.0
    for dy in range(2 * pad + 1):
        for dx in range(2 * pad + 1):
            part = _normalise(window[dy:dy + size, dx:dx + size])
            if part is not None:
                best = max(best, float((part * template).mean()))
    return best


def _text_lines(page, zoom):
    import user_maps
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = user_maps._clean_text(" ".join(span.get("text", "") for span in line["spans"]))
            if text:
                lines.append((text, [value * zoom for value in line["bbox"]]))
    return lines


def find_symbols(pdf_path, page_index=0):
    """Symbols on a PDF page named from its key.

    Returns (symbols, key) where ``symbols`` is a list of (name, fx, fy): the
    symbol's centre as fractions of the page's width and height from the top
    left; and ``key`` is a list of the key's names.  Both are empty when the
    page has no key of symbols.
    """
    import fitz
    with fitz.open(pdf_path) as document:
        page = document.load_page(page_index)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(SCAN_ZOOM, SCAN_ZOOM), alpha=False)
        rgb = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.h, pixmap.w, pixmap.n)
        gray = rgb[:, :, :3].mean(axis=2).astype(np.uint8)
        lines = _text_lines(page, SCAN_ZOOM)
    boxes = _white_boxes(gray, SCAN_ZOOM)

    def holds_text(box):
        x, y, w, h = box
        return any(a >= x - 2 and c <= x + w + 2 and b >= y - 2 and d <= y + h + 2
                   for _text, (a, b, c, d) in lines)

    # A key entry is a symbol with its name just to the right, such as
    # "[symbol] Public Toilet".  Map signs look the same ("[symbol] Exit &
    # Baggage claim"), so only columns of several entries count as the key.
    entries = []
    for box in boxes:
        if holds_text(box):
            continue
        x, y, w, h = box
        for text, (a, b, c, d) in lines:
            if (0 <= a - (x + w) <= 4 * SCAN_ZOOM and abs((b + d) / 2 - (y + h / 2)) < h / 2
                    and text[0].isalpha()):
                entries.append((text, box))
                break
    # A long name wraps onto the next line: "Passenger pick-up/" then
    # "drop-off".
    joined = []
    for text, box in entries:
        if text.endswith(("/", "-", "&")):
            x, y, w, h = box
            below = [t for t, (a, b, c, d) in lines
                     if abs(a - (x + w) - 2 * SCAN_ZOOM) <= 4 * SCAN_ZOOM and 0 <= b - (y + h) <= h]
            if below:
                text = text + below[0] if text.endswith(("/", "-")) else f"{text} {below[0]}"
        joined.append((text, box))
    entries = joined
    columns = {}
    for text, box in entries:
        columns.setdefault(round(box[0] / (2 * SCAN_ZOOM)), []).append((text, box))
    key = [entry for column in columns.values() if len(column) >= MIN_KEY_ENTRIES
           for entry in column]
    if not key:
        return [], []
    # Compare at the key's own symbol size, which is the map's too.
    size = int(round(sorted((box[2] + box[3]) / 2 for _text, box in key)[len(key) // 2]))
    pad = 2
    templates = []
    for text, box in key:
        window = _window(gray, box, size, 0)
        template = _normalise(window) if window is not None else None
        if template is not None:
            templates.append((text, template))
    if not templates:
        return [], []
    key_boxes = {box for _text, box in key}
    symbols = []
    for box in boxes:
        if box in key_boxes or holds_text(box):
            continue
        window = _window(gray, box, size, pad)
        if window is None:
            continue
        ranked = sorted(((_similarity(window, template, pad), text)
                         for text, template in templates), reverse=True)
        score, name = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
        if score >= MATCH_SCORE or (score >= CLEAR_MATCH_SCORE
                                    and score - runner_up >= CLEAR_LEAD):
            x, y, w, h = box
            symbols.append((name, (x + w / 2) / gray.shape[1], (y + h / 2) / gray.shape[0]))
    return symbols, [text for text, _box in key]


def symbol_places(pdf_path, page_index, map_width, map_height, id_prefix="symbol"):
    """Places for the symbols on a PDF page, in the same units as its labels.

    Returns (places, key names)."""
    symbols, key = find_symbols(pdf_path, page_index)
    places = [{"id": f"{id_prefix}-{index}", "name": name, "description": "",
               "x": fx * map_width, "y": (1.0 - fy) * map_height, "symbol": name}
              for index, (name, fx, fy) in enumerate(symbols, 1)]
    return places, key


# Words that take no plural "s": "3 luggage lockers" but "2 oversize baggage".
_NO_PLURAL = {"baggage", "luggage", "parking", "information", "equipment", "seating"}


def _counted(name, count):
    """"4 lifts", "2 sets of stairs", "1 changing places": a key's names are
    pluralised only where that is safe."""
    word = name if name.isupper() else name.lower()
    if word == "stairs":
        return f"{count} {'set' if count == 1 else 'sets'} of stairs"
    last = word.split()[-1] if word.split() else word
    if count > 1 and word.isupper() and word.isalpha():
        return f"{count} {word}s"          # "2 ATMs"
    if (count > 1 and last.isalpha() and last.islower()
            and not last.endswith("s") and last not in _NO_PLURAL):
        word += "s"
    return f"{count} {word}"


def describe_counts(names):
    """"5 accessible toilets, 4 lifts and 2 sets of stairs", most first."""
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    parts = [_counted(name, count) for name, count in
             sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""
