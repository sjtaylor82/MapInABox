"""Work out walkable paths from a floor plan picture, for blind map makers.

A floor plan shows corridors and concourses in their own shade, between the
shops, rooms and outside areas.  This finds that shade, thins the walkable
area to its centre lines and turns them into paths, then joins each place to
the nearest path.  The result is a map that gives directions without anyone
drawing on it; Check map and Test as a student then show where it needs help.

Pure NumPy and Pillow: nothing to install and no wx.
"""

from __future__ import annotations

import re
from collections import deque

import numpy as np

import user_maps

# Work on the picture at about this many pixels across: enough to keep
# corridors several pixels wide, small enough to stay quick.
WORK_PIXELS = 1100
# Colours within this much of each other (0-255, per channel) are one shade.
SHADE_TOLERANCE = 4
# Gaps left in the floor by icons and text, in pixels, are filled.
GAP_PIXELS = 4
# A label this close to the walkable area counts as next to it, as a share
# of the picture's width: shop numbers and room names sit inside the shop or
# room beside the corridor, often in its middle.
NEAR_SHARE = 0.12
# Dead-end spurs of the centre lines shorter than this (pixels) are noise.
SPUR_PIXELS = 12
# Walkways are narrow for their length: corridors and concourses average 1-2%
# of the picture's width across (Capalaba Park 1.0%, Brisbane Airport 1.4%),
# while page backgrounds and aprons around aircraft average 6-13%.  Wider
# regions are open space, not walkways.
MAX_MEAN_WIDTH_SHARE = 0.04
# Separate walkway networks closer than this share of the picture's width are
# joined: a doorway between a forecourt and a concourse drawn in two shades.
LINK_SHARE = 0.03


# -- picture helpers -------------------------------------------------------

def _load(path):
    from PIL import Image
    with Image.open(path) as image:
        rgb = image.convert("RGB")
    scale = min(1.0, WORK_PIXELS / max(rgb.size))
    if scale < 1.0:
        rgb = rgb.resize((round(rgb.width * scale), round(rgb.height * scale)),
                         Image.Resampling.NEAREST)
    return np.asarray(rgb).astype(np.int16), scale


def _box_sum(mask, radius):
    """Count of set pixels within a square of ``radius`` around each pixel."""
    padded = np.pad(mask.astype(np.int32), radius + 1)
    total = padded.cumsum(0).cumsum(1)
    size = 2 * radius + 1
    return (total[size:, size:] - total[:-size, size:]
            - total[size:, :-size] + total[:-size, :-size])[:mask.shape[0], :mask.shape[1]]


def _dilate(mask, radius):
    return _box_sum(mask, radius) > 0


def _erode(mask, radius):
    return _box_sum(~mask, radius) == 0


def _close(mask, radius):
    """Fill holes and gaps up to ``radius`` pixels (icons and text on floor)."""
    return _erode(_dilate(mask, radius), radius)


def _components(mask):
    """Label 4-connected regions using runs per row.  Returns (labels, count)."""
    height, width = mask.shape
    parent = []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs_prev, row_runs = [], []
    for y in range(height):
        row = mask[y]
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        runs = []
        for start, end in zip(edges[::2], edges[1::2]):
            label = len(parent)
            parent.append(label)
            for p_start, p_end, p_label in runs_prev:
                if p_start < end and start < p_end:
                    a, b = find(label), find(p_label)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
            runs.append((start, end, label))
        row_runs.append(runs)
        runs_prev = runs
    roots, count = {}, 0
    labels = np.zeros(mask.shape, dtype=np.int32)
    for y, runs in enumerate(row_runs):
        for start, end, label in runs:
            root = find(label)
            if root not in roots:
                count += 1
                roots[root] = count
            labels[y, start:end] = roots[root]
    return labels, count


# -- finding the walkable area ---------------------------------------------

def _candidate_shades(pixels, limit=12):
    """The most common light shades, merged within SHADE_TOLERANCE."""
    flat = pixels.reshape(-1, 3)
    keys = (flat // SHADE_TOLERANCE).astype(np.int32)
    packed = (keys[:, 0] << 16) | (keys[:, 1] << 8) | keys[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    shades = []
    for index in np.argsort(-counts):
        if counts[index] < flat.shape[0] * 0.01 or len(shades) >= limit:
            break
        key = values[index]
        colour = np.array([(key >> 16) & 255, (key >> 8) & 255, key & 255]) * SHADE_TOLERANCE
        colour += SHADE_TOLERANCE // 2
        # Walkways are drawn very light (233-243 on the Brisbane Airport and
        # Capalaba Park maps); the mid grey (150) there is the building shell.
        if colour.mean() >= 200:
            shades.append(colour)
    return shades


def find_walkable(pixels, label_points):
    """Boolean mask of the walkable floor, or None if no shade fits.

    Each light shade's regions are scored by how many place labels sit in or
    beside them.  Regions touching the picture's edges are the page or the
    outdoors, not the building, and are left out.
    """
    height, width = pixels.shape[:2]
    near = max(3, round(width * NEAR_SHARE))
    scored = []
    for shade in _candidate_shades(pixels):
        mask = np.abs(pixels - shade).max(axis=2) <= SHADE_TOLERANCE
        mask = _close(mask, GAP_PIXELS)
        labels, count = _components(mask)
        if not count:
            continue
        border = set(np.unique(np.concatenate(
            (labels[0], labels[-1], labels[:, 0], labels[:, -1])))) - {0}
        sizes = np.bincount(labels.ravel(), minlength=count + 1)
        for component in range(1, count + 1):
            if component in border or sizes[component] < height * width * 0.005:
                continue
            region = labels == component
            outline = region & ~_erode(region, 1)
            if 2 * sizes[component] / max(1, outline.sum()) > MAX_MEAN_WIDTH_SHARE * width:
                continue
            reach = _dilate(region, near)
            nearby = frozenset(i for i, (x, y) in enumerate(label_points)
                               if 0 <= y < height and 0 <= x < width and reach[int(y), int(x)])
            scored.append((len(nearby), int(sizes[component]), region, nearby))
    if not scored:
        return None
    # A building often has more than one walkable shade: Brisbane Airport's
    # concourse is a lighter grey than its forecourt.  After the region beside
    # the most places, add others only for places it does not reach.  Shop
    # floors are never added: their numbers already sit beside the corridor.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if scored[0][0] < 2:
        return None
    walkable = scored[0][2].copy()
    covered = set(scored[0][3])
    needed = max(3, round(len(label_points) * 0.05))
    for _score, _size, region, nearby in scored[1:]:
        if len(nearby - covered) >= needed:
            walkable |= region
            covered |= nearby
    return walkable


# -- centre lines ----------------------------------------------------------

def _thin(mask):
    """Zhang-Suen thinning: shrink a region to one-pixel centre lines."""
    image = np.pad(mask.astype(np.uint8), 1)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            p2, p3, p4 = image[:-2, 1:-1], image[:-2, 2:], image[1:-1, 2:]
            p5, p6, p7 = image[2:, 2:], image[2:, 1:-1], image[2:, :-2]
            p8, p9 = image[1:-1, :-2], image[:-2, :-2]
            centre = image[1:-1, 1:-1]
            neighbours = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            ring = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            transitions = sum(((a == 0) & (b == 1)).astype(np.uint8)
                              for a, b in zip(ring, ring[1:]))
            if step == 0:
                c1, c2 = p2 * p4 * p6, p4 * p6 * p8
            else:
                c1, c2 = p2 * p4 * p8, p2 * p6 * p8
            remove = ((centre == 1) & (neighbours >= 2) & (neighbours <= 6)
                      & (transitions == 1) & (c1 == 0) & (c2 == 0))
            if remove.any():
                image[1:-1, 1:-1][remove] = 0
                changed = True
    return image[1:-1, 1:-1].astype(bool)


_STEPS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _neighbours(skeleton, y, x):
    height, width = skeleton.shape
    return [(y + dy, x + dx) for dy, dx in _STEPS
            if 0 <= y + dy < height and 0 <= x + dx < width and skeleton[y + dy, x + dx]]


def _prune_spurs(skeleton, length=SPUR_PIXELS):
    """Remove short dead ends left by bumps in the walkable outline."""
    skeleton = skeleton.copy()
    for _round in range(3):
        removed = False
        ends = [(y, x) for y, x in zip(*np.nonzero(skeleton))
                if len(_neighbours(skeleton, y, x)) == 1]
        for start in ends:
            trail, current, previous = [start], start, None
            while len(trail) <= length:
                nxt = [p for p in _neighbours(skeleton, *current) if p != previous]
                if len(nxt) != 1:
                    break
                previous, current = current, nxt[0]
                trail.append(current)
            if len(trail) <= length and len(_neighbours(skeleton, *current)) >= 3:
                for y, x in trail[:-1]:
                    skeleton[y, x] = False
                removed = True
        if not removed:
            break
    return skeleton


def skeleton_paths(skeleton):
    """Split centre lines into polylines (lists of (y, x)) between junctions.

    A junction on a one-pixel centre line is often a small cluster of
    touching branch pixels.  Each cluster becomes one shared point, so the
    corridors meeting there join in the route graph instead of ending a
    pixel apart.
    """
    degree = {}
    for y, x in zip(*np.nonzero(skeleton)):
        degree[(int(y), int(x))] = len(_neighbours(skeleton, y, x))
    nodes = {p for p, d in degree.items() if d != 2}
    cluster_of, centres = {}, []
    for node in nodes:
        if node in cluster_of:
            continue
        members, queue = [node], deque([node])
        cluster_of[node] = len(centres)
        while queue:
            for p in _neighbours(skeleton, *queue.popleft()):
                if p in nodes and p not in cluster_of:
                    cluster_of[p] = len(centres)
                    members.append(p)
                    queue.append(p)
        centres.append((sum(p[0] for p in members) / len(members),
                        sum(p[1] for p in members) / len(members)))
    paths, used = [], set()
    for node in nodes:
        here = cluster_of[node]
        for first in _neighbours(skeleton, *node):
            if first in nodes or (node, first) in used:
                continue
            line, previous, current = [centres[here], first], node, first
            used.add((node, first))
            while current not in nodes:
                nxt = [p for p in _neighbours(skeleton, *current)
                       if p != previous and (p not in nodes or cluster_of[p] != here or len(line) > 2)]
                if not nxt:
                    break
                # Prefer stepping into a junction over running alongside it.
                nxt.sort(key=lambda p: p not in nodes)
                previous, current = current, nxt[0]
                line.append(current)
            if current in nodes:
                used.add((current, previous))
                line[-1] = centres[cluster_of[current]]
            if len(line) >= 2:
                paths.append(line)
    # Rings with no junction at all, e.g. a loop corridor.
    loose = set(degree) - nodes - {p for line in paths for p in line}
    while loose:
        start = loose.pop()
        line, previous, current = [start], None, start
        while True:
            nxt = [p for p in _neighbours(skeleton, *current) if p in loose and p != previous]
            if not nxt:
                break
            loose.discard(nxt[0])
            previous, current = current, nxt[0]
            line.append(current)
        if len(line) >= 2:
            paths.append(line + [start])
    return paths


def _link_networks(skeleton, reach):
    """Straight links between separate centre-line networks that come within
    ``reach`` pixels of each other, nearest first, so every network that can
    be joined to the rest is."""
    labels, count = _components(_dilate(skeleton, 1))
    groups = []
    for component in range(1, count + 1):
        pixels = np.argwhere(skeleton & (labels == component))
        if len(pixels) >= SPUR_PIXELS:
            groups.append(pixels)
    links, joined = [], {0}
    while len(joined) < len(groups):
        best = None
        for a in joined:
            for b in range(len(groups)):
                if b in joined:
                    continue
                pa, pb = groups[a][::2], groups[b][::2]
                distance = np.hypot(pa[:, None, 0] - pb[None, :, 0], pa[:, None, 1] - pb[None, :, 1])
                index = np.unravel_index(np.argmin(distance), distance.shape)
                if best is None or distance[index] < best[0]:
                    best = (distance[index], b, tuple(pa[index[0]]), tuple(pb[index[1]]))
        if best is None or best[0] > reach:
            # The nearest unjoined network is too far; try linking the rest
            # among themselves from a new starting network.
            rest = [i for i in range(len(groups)) if i not in joined]
            if not rest:
                break
            joined.add(rest[0])
            continue
        joined.add(best[1])
        links.append([best[2], best[3]])
    return links


# -- the whole step ----------------------------------------------------------

def add_paths_automatically(floor, picture_path, max_link_share=0.16):
    """Add walkable paths to a traced floor from its picture.

    Places are joined to the nearest centre line; a place further than
    ``max_link_share`` of the floor's width from any is left unjoined (it is
    probably outside the building) so Check map names it.  Returns a short
    summary dict, or None when the picture has no recognisable walkway.
    """
    pixels, _scale = _load(picture_path)
    height, width = pixels.shape[:2]
    to_px = lambda x, y: (x / floor["width"] * width, (floor["height"] - y) / floor["height"] * height)
    to_map = lambda px, py: (px / width * floor["width"], floor["height"] - py / height * floor["height"])
    places = floor.get("places", [])
    walkable = find_walkable(pixels, [to_px(p["x"], p["y"]) for p in places])
    if walkable is None:
        return None
    skeleton = _prune_spurs(_thin(walkable))
    lines = skeleton_paths(skeleton) + _link_networks(skeleton, round(width * LINK_SHARE))
    # Smooth the pixel staircase: a turn every few centimetres would be read
    # out as directions.
    tolerance = max(0.8, floor["width"] / 150)
    added = 0
    for line in lines:
        points = [to_map(x + 0.5, y + 0.5) for y, x in line]
        points = user_maps.simplify_points(points, tolerance)
        if len(points) >= 2:
            floor.setdefault("paths", []).append({"points": [[x, y] for x, y in points]})
            added += 1
    # Join each place to the nearest point on the smoothed paths, adding that
    # point to the path so the two really meet.  (Joining to a centre-line
    # pixel left joins stopping short of paths smoothed away from it.)
    walk_paths = floor.get("paths", [])[-added:] if added else []
    joined, left_out, left_out_ids, named_left_out, named_left_out_ids = 0, [], [], [], []
    limit = max_link_share * floor["width"]
    # A place well away from the paths is joined only when it stands alone,
    # like a department store's name in the middle of the store.  Packed
    # labels far from any path are a legend or title block: they are left
    # for the creator to delete.
    near_limit = limit / 2
    crowd_radius = 0.08 * floor["width"]

    def from_legend(place):
        # A place named from the legend, "Mi Casa (50)", is a real shop even
        # when it sits among others or off the walkways found.
        return bool(place.get("key") or place.get("symbol")) or bool(re.search(r"\((?:key )?\d{1,3}\)", place["name"]))

    def crowded(place):
        if from_legend(place):
            return False
        return sum(1 for other in places if other is not place
                   and ((other["x"] - place["x"]) ** 2
                        + (other["y"] - place["y"]) ** 2) ** 0.5 <= crowd_radius) >= 3

    for place in places:
        best = None
        for path in walk_paths:
            points = path["points"]
            for index, (a, b) in enumerate(zip(points, points[1:])):
                (qx, qy), t = user_maps._project((place["x"], place["y"]), tuple(a), tuple(b))
                distance = ((qx - place["x"]) ** 2 + (qy - place["y"]) ** 2) ** 0.5
                if best is None or distance < best[0]:
                    best = (distance, path, index, t, (qx, qy))
        if best is None or best[0] > limit or (best[0] > near_limit and crowded(place)):
            if from_legend(place):
                named_left_out.append(place["name"])
                named_left_out_ids.append(place["id"])
            else:
                left_out.append(place["name"])
                left_out_ids.append(place["id"])
            continue
        distance, path, index, t, (qx, qy) = best
        if 1e-6 < t < 1 - 1e-6:
            path["points"].insert(index + 1, [qx, qy])
        else:
            qx, qy = path["points"][index if t <= 1e-6 else index + 1]
        if distance > 1e-6:
            floor["paths"].append({"points": [[place["x"], place["y"]], [qx, qy]]})
        joined += 1
    return {"paths": added, "joined": joined, "left_out": left_out,
            "left_out_ids": left_out_ids, "named_left_out": named_left_out,
            "named_left_out_ids": named_left_out_ids,
            "walkable_share": float(walkable.mean())}
