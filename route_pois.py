"""route_pois.py — places a walker passes along one route step, in order and by side.

Pure geometry, no wx.  Given the step's path [(lat, lon), ...], the street of
each path segment and nearby POIs, work out which places are passed, how far
along, and whether they are on the walker's left or right.  Anything whose
order or side is doubtful is left out rather than guessed.
"""

import math
import re

# A place this close to the centre line could be on either footpath.
MIN_LATERAL_M = 4.0
# Businesses fronting the street; further away is likely the next street over.
MAX_LATERAL_M = 40.0
# Without a recorded street, only accept places close to this one.
MAX_LATERAL_UNKNOWN_STREET_M = 30.0
# Near a bend the left/right split is ambiguous.
BEND_DEGREES = 30.0
BEND_CLEARANCE_M = 10.0
# Places right at the start or the turn usually belong to the cross street.
END_CLEARANCE_M = 5.0
# Three or more places this close together are tenants of one building.
CLUSTER_M = 5.0
CLUSTER_MIN = 3
# Most tenants named for one building, the last slot saying how many more.
GROUP_NAMES_MAX = 10
# A name such as "First Ave Near George St" is a stop named after the street.
_STREET_NAME_LINKS = {"at", "near", "opposite", "opp", "and", "before", "after"}

_STREET_SUFFIXES = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "dr": "drive", "ct": "court", "pl": "place", "cres": "crescent",
    "cl": "close", "blvd": "boulevard", "hwy": "highway", "tce": "terrace",
    "pde": "parade", "esp": "esplanade", "ln": "lane", "gr": "grove",
    "cct": "circuit", "hwy.": "highway",
}


def street_key(name):
    words = re.sub(r"\s*\(.*?\)", "", str(name or "")).casefold()
    words = re.sub(r"[^0-9a-z ]+", " ", words).split()
    if words and words[-1] in _STREET_SUFFIXES:
        words[-1] = _STREET_SUFFIXES[words[-1]]
    return " ".join(words)


_STREET_TYPE_WORDS = set(_STREET_SUFFIXES.values())


def _names_a_street(name, street):
    """True for a place named after a street, e.g. a bus stop: "First Ave
    Near George St", or "Donald St at Beaumont St" seen from Beaumont."""
    street_words = street_key(street).split()
    words = [_STREET_SUFFIXES.get(w, w) for w in street_key(name).split()]
    if street_words and words[:len(street_words)] == street_words:
        rest = words[len(street_words):]
        if not rest or rest[0] in _STREET_NAME_LINKS:
            return True
    # Tram and bus stops: "Burwood Rd/Glenferrie Rd-Stop 73".
    if re.search(r"\bstop\s*\d+\b", name, re.I) or re.search(
            r"\b(?:rd|road|st|street|ave|avenue|hwy|highway)\s*/", name, re.I):
        return True
    # "<something> Street at <something>": a stop named for a corner.
    return any(word in _STREET_TYPE_WORDS and nxt in _STREET_NAME_LINKS - {"and"}
               for word, nxt in zip(words, words[1:]))


def _house_number(value):
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


class _StepPath:
    """The step's path in local metres (x east, y north), with along-distances."""

    def __init__(self, polyline):
        self.lat0, self.lon0 = polyline[0]
        self.kx = 111320.0 * math.cos(math.radians(self.lat0))
        self.ky = 110540.0
        self.points = [self.xy(lat, lon) for lat, lon in polyline]
        self.along = [0.0]
        for (ax, ay), (bx, by) in zip(self.points, self.points[1:]):
            self.along.append(self.along[-1] + math.hypot(bx - ax, by - ay))
        self.length = self.along[-1]
        self.bends = []
        for k in range(1, len(self.points) - 1):
            turn = abs((self._heading(k) - self._heading(k - 1) + 180.0) % 360.0 - 180.0)
            if turn > BEND_DEGREES:
                self.bends.append(self.along[k])

    def xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def _heading(self, k):
        (ax, ay), (bx, by) = self.points[k], self.points[k + 1]
        return math.degrees(math.atan2(bx - ax, by - ay))

    def project(self, lat, lon):
        """Return (segment index, along metres, signed lateral metres) or None.

        Lateral is positive on the left.  None when the place lies beyond the
        path's ends or off the outside corner of a bend, where it projects onto
        no segment's interior.
        """
        px, py = self.xy(lat, lon)
        best = None
        for k, ((ax, ay), (bx, by)) in enumerate(zip(self.points, self.points[1:])):
            dx, dy = bx - ax, by - ay
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq < 1e-6:
                continue
            t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
            if not 0.0 <= t <= 1.0:
                continue
            seg_len = math.sqrt(seg_len_sq)
            lateral = (dx * (py - ay) - dy * (px - ax)) / seg_len
            if best is None or abs(lateral) < abs(best[2]):
                best = (k, self.along[k] + t * seg_len, lateral)
        return best


def _poi_street(poi):
    tags = poi.get("tags") if isinstance(poi.get("tags"), dict) else {}
    return poi.get("street") or tags.get("addr:street") or ""


def _poi_number(poi):
    tags = poi.get("tags") if isinstance(poi.get("tags"), dict) else {}
    return _house_number(poi.get("number") or tags.get("addr:housenumber"))


def _poi_number_text(poi):
    tags = poi.get("tags") if isinstance(poi.get("tags"), dict) else {}
    return str(poi.get("number") or tags.get("addr:housenumber") or "").strip()


def _poi_name(poi):
    return str(poi.get("label") or poi.get("name") or "").split(",")[0].strip()


def _parity_sides(path, segment_streets, address_points):
    """Learn, per street, which side odd and even numbers are on along this step.

    Returns {street_key: {0 or 1: "left"/"right"}} for parities where at least
    three addresses agree at least 85% of the time.
    """
    step_streets = {street_key(s) for s in segment_streets if s}
    votes = {}
    for address in address_points or ():
        key = street_key(address.get("street"))
        number = _house_number(address.get("number"))
        if key not in step_streets or number is None:
            continue
        try:
            hit = path.project(float(address["lat"]), float(address["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if hit is None or street_key(segment_streets[hit[0]]) != key:
            continue
        if not 3.0 <= abs(hit[2]) <= 60.0:
            continue
        side = "left" if hit[2] > 0 else "right"
        votes.setdefault(key, {}).setdefault(number % 2, []).append(side)
    learned = {}
    for key, by_parity in votes.items():
        for parity, sides in by_parity.items():
            for side in ("left", "right"):
                if len(sides) >= 3 and sides.count(side) / len(sides) >= 0.85:
                    learned.setdefault(key, {})[parity] = side
    return learned


_EATERY_KINDS = {
    "restaurant", "cafe", "fast food", "bakery", "bar", "pub",
    "ice cream", "food court", "biergarten",
}
# Name words that mark an otherwise uncategorised place as somewhere to eat,
# with the noun to describe it by ("" names it without guessing a type).
_EATERY_NAME_WORDS = {
    "espresso": "café", "coffee": "café", "cafe": "café", "café": "café",
    "bakery": "bakery", "patisserie": "bakery", "bistro": "",
    "eatery": "", "sushi": "", "pizza": "", "pizzeria": "",
    "burger": "", "burgers": "", "noodle": "", "noodles": "", "dumpling": "",
    "kebab": "", "grill": "", "ramen": "", "taqueria": "",
}
_EATERY_NOUNS = {
    "restaurant": "restaurant", "cafe": "café", "bakery": "bakery",
    "bar": "bar", "pub": "pub", "ice cream": "ice cream shop",
    "fast food": "takeaway",
}


def _cuisine(poi):
    tags = poi.get("tags") if isinstance(poi.get("tags"), dict) else {}
    cuisine = poi.get("cuisine") or tags.get("cuisine") or ""
    # OSM lists several as "thai;asian": the first is the main one.
    cuisine = str(cuisine).split(";")[0].replace("_", " ").strip()
    if cuisine.casefold() in ("international", "other", "various", "regional"):
        return ""
    return cuisine.title() if cuisine else ""


def eatery_noun(poi):
    """For somewhere to eat or drink, the noun to describe it by ("" when it
    is an eatery of no clear type); None for anything else."""
    kind = str(poi.get("kind") or "").lower()
    if kind in _EATERY_KINDS:
        return _EATERY_NOUNS.get(kind, "")
    words = re.findall(r"[a-zé]+", _poi_name(poi).casefold())
    for word in words:
        if word in _EATERY_NAME_WORDS:
            return _EATERY_NAME_WORDS[word]
    return None


# A name already saying what the place is needs no "the restaurant" before it.
_SELF_DESCRIBING_WORDS = {
    "hotel", "tavern", "pub", "bar", "inn", "cafe", "café", "restaurant",
    "bistro", "eatery", "espresso", "coffee", "bakery", "kitchen", "sushi",
    "pizza", "pizzeria", "grill", "brewery", "diner", "canteen",
}
# A tenant whose name says it is the building leads its group.
_BUILDING_WORDS = {"hotel", "tavern", "centre", "center", "plaza", "arcade",
                   "house", "chambers", "building", "inn", "mall"}
_PLURALS = {"café": "cafés", "bakery": "bakeries", "takeaway": "takeaways",
            "ice cream shop": "ice cream shops"}


def _words(name):
    return set(re.findall(r"[a-zé]+", name.casefold()))


def _descriptor(poi):
    """("Thai restaurant", name) for an eatery worth describing, else ("", name)."""
    name = _poi_name(poi)
    noun = eatery_noun(poi)
    if not noun or _words(name) & _SELF_DESCRIBING_WORDS:
        return "", name
    cuisine = _cuisine(poi)
    if cuisine and cuisine.casefold() in name.casefold():
        cuisine = ""
    if noun in ("takeaway", "restaurant") and not cuisine:
        # "Depot" and "KFC" are enough; "the restaurant" before every one
        # only adds words.  A cuisine still earns its "Thai restaurant".
        return "", name
    return f"{cuisine + ' ' if cuisine else ''}{noun}", name


def place_phrase(poi):
    """How to say a place: "the Thai restaurant Samila Gaeng" for eateries,
    the plain name otherwise."""
    desc, name = _descriptor(poi)
    return f"the {desc} {name}" if desc else name


def passed_places(polyline, segment_streets, pois, address_points=()):
    """Places confidently passed along one step, in walking order.

    Returns dicts with ``along`` (metres), ``side``, ``name``, ``eatery`` and
    either ``desc`` (an eatery's "Thai restaurant", "" otherwise) for a
    single place or ``phrase`` for a building group such as "the Northern
    Star Hotel, with Finn McCools Eatery and Super Cellars".
    """
    if len(polyline) < 2:
        return []
    path = _StepPath(polyline)
    if path.length < 2 * END_CLEARANCE_M:
        return []
    parity_sides = _parity_sides(path, segment_streets, address_points)
    passed = []
    for poi in pois:
        name = _poi_name(poi)
        if not name:
            continue
        try:
            hit = path.project(float(poi["lat"]), float(poi["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if hit is None:
            continue
        segment, along, lateral = hit
        if not END_CLEARANCE_M <= along <= path.length - END_CLEARANCE_M:
            continue
        if any(abs(along - bend) < BEND_CLEARANCE_M for bend in path.bends):
            continue
        if not MIN_LATERAL_M <= abs(lateral) <= MAX_LATERAL_M:
            continue
        segment_street = street_key(segment_streets[segment])
        if _names_a_street(name, segment_streets[segment]):
            continue
        poi_street = street_key(_poi_street(poi))
        if poi_street and segment_street:
            if poi_street != segment_street:
                continue
        elif abs(lateral) > MAX_LATERAL_UNKNOWN_STREET_M:
            continue
        side = "left" if lateral > 0 else "right"
        number = _poi_number(poi)
        expected = parity_sides.get(segment_street, {}).get(
            number % 2 if number is not None else None)
        if expected and expected != side:
            continue
        # A house number is only spoken when the place is recorded on this
        # street: a corner shop numbered on the cross street would mislead.
        on_this_street = bool(poi_street and segment_street and poi_street == segment_street)
        passed.append({
            "along": along, "side": side, "name": name, "poi": poi,
            "xy": path.xy(float(poi["lat"]), float(poi["lon"])),
            "number": _poi_number_text(poi), "street": segment_streets[segment],
            "own_number": _poi_number_text(poi) if on_this_street else "",
        })
    passed.sort(key=lambda item: (item["along"], item["name"].casefold()))
    return _collapse_buildings(_drop_variants(passed))


# Words that do not tell two places apart ("The Kent" / "The Kent Hotel").
_GENERIC_NAME_WORDS = {
    "the", "and", "of", "at", "on", "hotel", "pub", "bar", "cafe", "café",
    "restaurant", "pizza", "pharmacy", "store", "shop", "co", "pty", "ltd",
}


def core_words(name):
    return {w for w in re.findall(r"[0-9a-zé]+", name.casefold().replace("'", ""))
            if w not in _GENERIC_NAME_WORDS}


def _drop_variants(passed, within_m=30.0):
    """Drop a place whose name is another nearby place's name plus or minus
    generic words: OSM and HERE often both list one business ("Crust Pizza"
    and "Crust", "Rice's Bookshop" and "Rices Bookshop").  The longer name
    is kept."""
    kept = []
    for entry in sorted(passed, key=lambda e: -len(e["name"])):
        core = core_words(entry["name"])
        duplicate = core and any(
            entry["side"] == other["side"]
            and math.hypot(entry["xy"][0] - other["xy"][0],
                           entry["xy"][1] - other["xy"][1]) <= within_m
            and (core <= core_words(other["name"]) or core_words(other["name"]) <= core)
            for other in kept)
        if not duplicate:
            kept.append(entry)
    kept.sort(key=lambda item: (item["along"], item["name"].casefold()))
    return kept


def pois_along(polyline, segment_streets, pois, address_points=()):
    """Return [(along_m, side, name)] for places confidently passed, in order."""
    return [(p["along"], p["side"], p["name"])
            for p in passed_places(polyline, segment_streets, pois, address_points)]


def _collapse_buildings(passed):
    """Announce a building full of tenants once, naming them in place.

    Places on the same side group together when they share a street number
    (within 30 m) or stand within CLUSTER_M of each other.  The group is led
    by a tenant that is the building ("the Northern Star Hotel, with Finn
    McCools Eatery and Super Cellars"), else by the shared address ("23
    George Street, with the café Nourish Espresso & Catering, ..."), else by
    its first place.  Eateries are named first.
    """
    groups = []
    for entry in passed:
        x, y = entry["xy"]
        for group in groups:
            seed = group[0]
            gap = math.hypot(seed["xy"][0] - x, seed["xy"][1] - y)
            if seed["side"] == entry["side"] and (
                    gap <= CLUSTER_M
                    or (entry["number"] and seed["number"] == entry["number"] and gap <= 30.0)):
                group.append(entry)
                break
        else:
            groups.append([entry])
    out = []
    for group in groups:
        unique, seen = [], set()
        for item in group:
            if item["name"].casefold() not in seen:
                seen.add(item["name"].casefold())
                unique.append(item)
        if len(unique) < CLUSTER_MIN:
            for item in unique:
                desc, _name = _descriptor(item["poi"])
                out.append({"along": item["along"], "side": item["side"],
                            "name": item["name"], "desc": desc,
                            "say": _with_number(item["name"], item["own_number"]),
                            "eatery": eatery_noun(item["poi"]) is not None})
            continue
        seed = group[0]
        number, street = seed["number"], seed["street"]
        building = next((item for item in unique
                         if _words(item["name"]) & _BUILDING_WORDS), None)
        tenants = [item for item in unique if item is not building]
        tenants.sort(key=lambda item: eatery_noun(item["poi"]) is None)   # eateries first
        names = [place_phrase(item["poi"]) for item in tenants]
        if len(names) > GROUP_NAMES_MAX:
            rest = len(names) - GROUP_NAMES_MAX + 1
            names = names[:GROUP_NAMES_MAX - 1] + [f"{rest} more"]
        if building is not None:
            lead = building["name"]
            if not lead.casefold().startswith("the "):
                lead = "the " + lead
            own = building["own_number"] or next(
                (item["own_number"] for item in unique if item["own_number"]), "")
            lead = _with_number(lead, own)
        elif number and street and all(item["number"] == number for item in unique):
            lead = f"{number} {street}"
        else:
            lead, names = names[0], names[1:]
        name = (building["name"] if building is not None
                else f"{len(unique)} businesses at {number} {street}"
                if number and street and lead == f"{number} {street}"
                else lead)
        out.append({"along": seed["along"], "side": seed["side"], "name": name,
                    "eatery": any(eatery_noun(item["poi"]) is not None for item in unique),
                    "phrase": f"{lead}, with {join_names(names)}" if names else lead})
    out.sort(key=lambda item: (item["along"], item["name"].casefold()))
    return out


def _with_number(name, number):
    """'Dosa Cafe (35)' when the place's house number on this street is known."""
    return f"{name} ({number})" if number else name


def join_names(names):
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _choose(places, maximum):
    """Up to ``maximum`` places, eateries before anything else, returned in
    walking order, with the number of eateries that did not fit."""
    seen, unique = set(), []
    for place in places:
        if place["name"].casefold() not in seen:
            seen.add(place["name"].casefold())
            unique.append(place)
    ranked = sorted(range(len(unique)), key=lambda i: (not unique[i]["eatery"], i))
    keep = set(ranked[:maximum])
    left_out_eateries = sum(1 for i in ranked[maximum:] if unique[i]["eatery"])
    return [p for i, p in enumerate(unique) if i in keep], left_out_eateries


def _plural(desc):
    noun = next((n for n in _PLURALS if desc.endswith(n)), None)
    if noun:
        return desc[:-len(noun)] + _PLURALS[noun]
    return desc + "s"


def _say_run(places):
    """Say consecutive places on one side, in order.

    Neighbouring eateries of one type share it: "the restaurants Broaster
    Chicken and Depot".  A building group carries its own commas, so items
    are then separated by "then" rather than listed.
    """
    items = []
    for place in places:
        desc = place.get("desc", "")
        say = place.get("say", place["name"])
        if desc and items and items[-1][0] == desc:
            items[-1][1].append(say)
        elif "phrase" in place:
            items.append((None, [place["phrase"]]))
        else:
            items.append((desc, [say]))
    spoken = []
    for desc, names in items:
        if desc is None or not desc:
            spoken.append(names[0] if len(names) == 1 else join_names(names))
        elif len(names) == 1:
            spoken.append(f"the {desc} {names[0]}")
        else:
            spoken.append(f"the {_plural(desc)} {join_names(names)}")
    if any(desc is None for desc, _names in items):
        return ", then ".join(spoken)
    if len(spoken) > 1 and any(" and " in item for item in spoken):
        # "the restaurants Depot and Broaster Chicken, and Sushi Revolution"
        return ", ".join(spoken[:-1]) + ", and " + spoken[-1]
    return join_names(spoken)


def _more_eateries(count):
    if not count:
        return ""
    noun = "place to eat" if count == 1 else "places to eat"
    return f"  {count} more {noun} along here."


def describe_pois_along(polyline, segment_streets, pois, address_points=(), maximum=6):
    """"On your left, you'll pass KFC.  On your right, the Thai restaurant
    Samila Gaeng." — or "" when no place is confidently placed.

    Each side is said once, in walking order, side first so it is not lost
    after a long list; the side reached first comes first.  Up to
    ``maximum`` places are named, eateries before anything else, and any
    further eateries are counted.
    """
    chosen, more = _choose(passed_places(polyline, segment_streets, pois, address_points),
                           maximum)
    sides = []
    for place in chosen:
        if place["side"] not in sides:
            sides.append(place["side"])
    sentences = []
    for index, side in enumerate(sides):
        run = _say_run([p for p in chosen if p["side"] == side])
        lead = f"On your {side}, you'll pass" if index == 0 else f"On your {side},"
        sentences.append(f"{lead} {run}.")
    return "  ".join(sentences) + _more_eateries(more) if sentences else ""


def describe_sides(polyline, segment_streets, pois, address_points=(), maximum=20):
    """For a stretch summary: what is on each side, in walking order.

    "On your left: KFC.  On your right: Moon and Pearl Bridal Sunshine Coast,
    then 23 George Street, with the café Nourish Espresso & Catering, ..."
    Empty when nothing is confidently placed.
    """
    places = passed_places(polyline, segment_streets, pois, address_points)
    sentences = []
    for side in ("left", "right"):
        chosen, _more = _choose([p for p in places if p["side"] == side], maximum)
        if chosen:
            sentences.append(f"On your {side}: {_say_run(chosen)}.")
    return "  ".join(sentences)
