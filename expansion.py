#!/usr/bin/env python3
"""
expansion.py - PERSON B - answers "WHERE?"

Never looks at money or prices. Only the map.
The master (bot_template.py) asks:

    is_good_base_site(pos, view, me)                  -> bool         "Is this tile a good place for a base?"
    best_ore(worker, view, me, taken, want_iron=False) -> (x,y)|None  "Which deposit should this worker go mine?"
    explore_target(worker, view, me)                  -> (x,y)        "Nothing to mine - where do I scout?"

Edge plan (new):
    rush_target(worker, view, me)      -> (x,y)|None  "No edge base yet - where on the edge do we go?"
    expansion_target(worker, view, me) -> (x,y)|None  "I'm the farthest worker on my side - where's the next base?"

Extra:
    nearby_base_site(worker, view, me) -> (x,y)|None   "Full and far from home - is there a site 1-2 steps away?"

THE EDGE PLAN IN 5 STEPS
  1. Rush: until we own a base near the edge, every worker heads to a tile EDGE_MARGIN in from the
     edge nearest our starting base (mining iron only on or next to its path).
  2. First edge base: it may be built even with no ore around it.
  3. Split: each worker is given a side of the edge ("low" or "high" along the edge, measured from the
     first edge base). Sides alternate as workers appear. best_ore / explore_target stay on that side.
  4. Richer side: we keep a running total of gold seen on each side's half of the edge lane.
     From SPLIT_TURN on, about RICH_SHARE of the workers move to the richer side.
  5. Keep expanding: the farthest worker on a side, once > EXPAND_DISTANCE from every base,
     is sent to the closest good base site ahead of it on its side.

`want_iron` is a flag the master gets from economy.iron_wanted(); it only changes how much
an iron deposit is worth here. Expansion itself never reads the bank.
"""
from __future__ import annotations

from common import (
    LAST_SEEN, ORE_MEMORY, TERRAIN_MEMORY,
    can_mine_here, capacity, cheb, home_distance, in_enemy_territory, load,
    manhattan, pos_of, state,
)

# ======================= KNOBS =======================
EDGE_MARGIN = 2          # bases (and the scouting lane) stay this many tiles in from the map edge
MIN_BASE_SPACING = 4     # no base within this Manhattan distance of another of our bases
SITE_RADIUS = 2          # a base claims ore within Manhattan distance 2
MIN_SITE_VALUE = 0.5     # need at least one new iron (0.5) or gold (1.0) deposit in reach
GOLD_SITE_VALUE = 1.0
IRON_SITE_VALUE = 0.5

GOLD_VALUE = 1.0         # worth of 1 kg when choosing ore
IRON_VALUE_NEEDED = 1.0  # iron when the economy still wants iron
IRON_VALUE_EXTRA = 0.3   # iron otherwise (trades 3:1 into gold)
RETURN_WEIGHT = 0.5      # how much the walk from the ore back to a base counts

FAR_FROM_HOME = 8        # worth building an extra base when this far (turns) from the nearest base
WAYPOINT_SPACING = 6     # spacing of scouting waypoints along the edge lane
RESCAN_TURNS = 80        # re-scout a waypoint not seen for this many turns
OFF_EDGE_PENALTY = 12    # extra "distance" for waypoints not on our nearest edge

# --- edge plan ---
EDGE_BASE_DEPTH = 4      # a base this close to the edge (tiles) counts as our "edge base"
RUSH_SEARCH = 8          # look this far along the edge for a buildable first-base tile
LANE_DEPTH = 6           # "edge lane" = tiles within this distance of the edge (gold counted here)
MAX_ORE_DEPTH = 12       # after the split, ignore ore / scouting deeper than this (keeps workers off the center); None = no limit
SPLIT_TURN = 120         # from this turn, send more workers to the richer side
RICH_SHARE = 2 / 3       # share of workers on the richer side
RICHER_MARGIN = 1.1      # the other side must have 10% more gold before we switch which side is "richer"
EXPAND_DISTANCE = 6      # farthest worker this far from every base -> go build the next base
EXPAND_SEARCH = 6        # how far around that worker to look for a base site
# =====================================================

_PRIMARY_EDGE = None
_START = None            # our starting base
_ANCHOR = None           # "along" coordinate of our first edge base (splits low/high sides)
_SIDE = {}               # workerId -> "low" | "high"
_GOLD_SEEN = {"low": 0, "high": 0}
_COUNTED = set()         # gold deposits already added to _GOLD_SEEN
_RICH = None             # current richer side
_WAYPOINTS = {}
_REFRESHED = None        # view object we last refreshed for


# ---------------- edge geometry ----------------
def _depth(p, s) -> int:
    """Tiles between p and our primary edge."""
    x, y = p
    return {"north": y, "south": s["H"] - 1 - y, "west": x, "east": s["W"] - 1 - x}[_PRIMARY_EDGE]


def _along(p) -> int:
    """Coordinate running along the primary edge."""
    return p[0] if _PRIMARY_EDGE in ("north", "south") else p[1]


def _edge_tile(along, depth, s):
    if _PRIMARY_EDGE == "north":
        return (along, depth)
    if _PRIMARY_EDGE == "south":
        return (along, s["H"] - 1 - depth)
    if _PRIMARY_EDGE == "west":
        return (depth, along)
    return (s["W"] - 1 - depth, along)


def _tile_side(p):
    a = _along(p)
    return "low" if a < _ANCHOR else "high" if a > _ANCHOR else "both"


def _on_side(p, side) -> bool:
    t = _tile_side(p)
    return t == "both" or t == side


def _out(p, side) -> int:
    """How far p is out from the edge base, in the direction of `side`."""
    return (_ANCHOR - _along(p)) if side == "low" else (_along(p) - _ANCHOR)


def _edge_base(s):
    """Our first base within EDGE_BASE_DEPTH of the edge, or None."""
    for b in s["my_bases"]:
        if _depth(b, s) <= EDGE_BASE_DEPTH:
            return b
    return None


# ---------------- per-turn bookkeeping ----------------
def _refresh(s, me) -> None:
    """Once per turn: edge, anchor, side assignments and gold totals."""
    global _PRIMARY_EDGE, _START, _ANCHOR, _RICH, _REFRESHED
    if _REFRESHED is s["view"]:
        return
    _REFRESHED = s["view"]
    if _START is None and s["my_bases"]:
        _START = s["my_bases"][0]
        x, y = _START
        gaps = {"west": x, "east": s["W"] - 1 - x, "north": y, "south": s["H"] - 1 - y}
        _PRIMARY_EDGE = min(gaps, key=gaps.get)
    if _PRIMARY_EDGE is None:
        return
    if _ANCHOR is None:
        eb = _edge_base(s)
        if eb is None:
            return
        _ANCHOR = _along(eb)

    # Running total of gold seen on each side's half of the edge lane
    for q, ore in ORE_MEMORY.items():
        if ore["type"] != "gold" or q in _COUNTED or _depth(q, s) > LANE_DEPTH:
            continue
        _COUNTED.add(q)
        side = _tile_side(q)
        if side == "both":
            _GOLD_SEEN["low"] += ore["amount"] / 2
            _GOLD_SEEN["high"] += ore["amount"] / 2
        else:
            _GOLD_SEEN[side] += ore["amount"]

    # Which side is richer (with a margin so it doesn't flip back and forth)
    lo, hi = _GOLD_SEEN["low"], _GOLD_SEEN["high"]
    if _RICH is None:
        if lo != hi:
            _RICH = "low" if lo > hi else "high"
    elif _GOLD_SEEN[_other(_RICH)] > _GOLD_SEEN[_RICH] * RICHER_MARGIN:
        _RICH = _other(_RICH)

    workers = sorted(s["my_workers"], key=lambda w: w["unitId"])
    n = len(workers)
    if s["turn"] >= SPLIT_TURN and _RICH is not None:
        target = {_RICH: round(n * RICH_SHARE)}
        target[_other(_RICH)] = n - target[_RICH]
    else:
        target = {"low": (n + 1) // 2, "high": n // 2}

    # New workers: alternate onto whichever side is short
    for w in workers:
        if w["unitId"] not in _SIDE:
            count = {"low": 0, "high": 0}
            for v in _SIDE.values():
                count[v] += 1
            short = "low" if count["low"] - target["low"] <= count["high"] - target["high"] else "high"
            _SIDE[w["unitId"]] = short

    # Rebalance: move the workers closest to the edge base over to the side that needs them
    ids = {w["unitId"] for w in workers}
    for side in ("low", "high"):
        other = _other(side)
        members = [w for w in workers if _SIDE.get(w["unitId"]) == other]
        need = target[side] - sum(1 for i in ids if _SIDE.get(i) == side)
        members.sort(key=lambda w: (abs(_along(pos_of(w)) - _ANCHOR), w["unitId"]))
        for w in members[:max(0, need)]:
            _SIDE[w["unitId"]] = side


def _other(side):
    return "high" if side == "low" else "low"


def _split_active() -> bool:
    return _ANCHOR is not None


# ---------------- base sites ----------------
def _site_value(pos, s, me) -> float:
    """Value of the ore a base on `pos` would ADD (deposits we don't already claim)."""
    value = 0.0
    claims = s["claims"]
    for q, ore in ORE_MEMORY.items():
        if manhattan(q, pos) <= SITE_RADIUS and me not in claims.get(q, ()):
            value += GOLD_SITE_VALUE if ore["type"] == "gold" else IRON_SITE_VALUE
    return value


def is_good_base_site(pos, view, me) -> bool:
    """
    YES when ALL hold:
      1. at least EDGE_MARGIN tiles from every map edge
      2. plain empty tile: no deposit, no mountain/crater/void, no base
      3. not inside enemy territory (the server would refuse)
      4. more than MIN_BASE_SPACING from our bases (incl. ones ordered this turn)
      5. new ore within Manhattan 2 worth >= MIN_SITE_VALUE
         EXCEPT our first edge base: near the edge it may be built with no ore at all.
    """
    s = state(view, me)
    _refresh(s, me)
    x, y = pos
    if not (EDGE_MARGIN <= x < s["W"] - EDGE_MARGIN and EDGE_MARGIN <= y < s["H"] - EDGE_MARGIN):
        return False
    if pos in ORE_MEMORY or TERRAIN_MEMORY.get(pos, "empty") != "empty":
        return False
    if pos in s["my_base_set"] or pos in s["enemy_bases"]:
        return False
    if in_enemy_territory(pos, view, me):
        return False
    for b in s["my_bases"] + s["planned_bases"]:
        if manhattan(b, pos) <= MIN_BASE_SPACING:
            return False
    if _PRIMARY_EDGE and not _split_active() and _depth(pos, s) == EDGE_MARGIN:
        return True                               # first edge base (on the rush lane): no ore needed
    return _site_value(pos, s, me) >= MIN_SITE_VALUE


def rush_target(worker, view, me):
    """
    Until we own a base within EDGE_BASE_DEPTH of the edge: the buildable tile EDGE_MARGIN in from
    the edge nearest our starting base (searching up to RUSH_SEARCH along the edge for a plain tile).
    Returns None once the edge base exists. All workers get the same tile.
    """
    s = state(view, me)
    _refresh(s, me)
    if _PRIMARY_EDGE is None or _split_active():
        return None
    a0 = _along(_START)
    length = s["W"] if _PRIMARY_EDGE in ("north", "south") else s["H"]
    best, best_key = None, None
    for off in range(-RUSH_SEARCH, RUSH_SEARCH + 1):
        a = a0 + off
        if not (EDGE_MARGIN <= a < length - EDGE_MARGIN):
            continue
        q = _edge_tile(a, EDGE_MARGIN, s)
        if q in s["planned_bases"] or not is_good_base_site(q, view, me):
            continue
        key = (abs(off), off)
        if best_key is None or key < best_key:
            best, best_key = q, key
    if best is None:
        best = _edge_tile(min(max(a0, EDGE_MARGIN), length - 1 - EDGE_MARGIN), EDGE_MARGIN, s)
    return best


def expansion_target(worker, view, me):
    """
    Only for the worker farthest out on its side: once it is more than EXPAND_DISTANCE turns from
    every base, the closest good base site ahead of it (or level with it) on its side.
    """
    s = state(view, me)
    _refresh(s, me)
    if not _split_active():
        return None
    side = _SIDE.get(worker["unitId"])
    if side is None:
        return None
    members = [w for w in s["my_workers"] if _SIDE.get(w["unitId"]) == side]
    farthest = max(members, key=lambda w: (_out(pos_of(w), side), w["unitId"]))
    if farthest["unitId"] != worker["unitId"]:
        return None
    p = pos_of(worker)
    if home_distance(p) <= EXPAND_DISTANCE:
        return None
    best, best_key = None, None
    for dx in range(-EXPAND_SEARCH, EXPAND_SEARCH + 1):
        for dy in range(-EXPAND_SEARCH, EXPAND_SEARCH + 1):
            q = (p[0] + dx, p[1] + dy)
            if q in s["planned_bases"] or not _on_side(q, side) or not is_good_base_site(q, view, me):
                continue
            ahead = 0 if _out(q, side) >= _out(p, side) - 1 else 1
            key = (ahead, cheb(p, q), -_site_value(q, s, me))
            if best_key is None or key < best_key:
                best, best_key = q, key
    return best


def nearby_base_site(worker, view, me):
    """
    For a worker that is far (> FAR_FROM_HOME turns) from any base: the best good site
    within 2 steps, or None. Saves a long walk home and claims the ore it is mining.
    """
    s = state(view, me)
    p = pos_of(worker)
    if home_distance(p) <= FAR_FROM_HOME:
        return None
    best, best_key = None, None
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            q = (p[0] + dx, p[1] + dy)
            if q in s["planned_bases"] or not is_good_base_site(q, view, me):
                continue
            key = (-_site_value(q, s, me), cheb(p, q))
            if best_key is None or key < best_key:
                best, best_key = q, key
    return best


# ---------------- ore ----------------
def best_ore(worker, view, me, taken, want_iron=False):
    """
    The deposit with the best kg-per-turn rate:
        rate = kg * value / (walk_there + kg + RETURN_WEIGHT * walk_back_to_base)
    kg = min(deposit amount, free space). Gold value 1.0; iron 1.0 if want_iron else 0.3.
    Skips: tiles in `taken` (other workers already going there), tiles an enemy is standing on,
    and enemy-only territory. After the split it only looks on the worker's own side and no deeper
    than MAX_ORE_DEPTH from the edge. The worker's own tile is always allowed, so if it returns the
    worker's position, the answer is "mine right here".
    """
    s = state(view, me)
    _refresh(s, me)
    p = pos_of(worker)
    free = capacity(worker) - load(worker)
    if free <= 0:
        return None
    side = _SIDE.get(worker["unitId"]) if _split_active() else None
    enemy_tiles = {q for q, us in s["units_at"].items() if any(u.get("teamId") != me for u in us)}
    best, best_rate = None, 0.0
    for q, ore in ORE_MEMORY.items():
        if q != p:
            if q in taken or q in enemy_tiles:
                continue
            if side is not None:
                if not _on_side(q, side):
                    continue
                if MAX_ORE_DEPTH is not None and _depth(q, s) > MAX_ORE_DEPTH:
                    continue
        if not can_mine_here(q, view, me):
            continue
        if ore["type"] == "gold":
            value = GOLD_VALUE
        else:
            value = IRON_VALUE_NEEDED if want_iron else IRON_VALUE_EXTRA
        kg = min(ore["amount"], free)
        trip = cheb(p, q) + kg + RETURN_WEIGHT * home_distance(q)
        rate = kg * value / max(trip, 1)
        if rate > best_rate:
            best, best_rate = q, rate
    return best


# ---------------- scouting ----------------
def _waypoints(W, H):
    key = (W, H)
    if key not in _WAYPOINTS:
        m = EDGE_MARGIN
        pts = []
        for x in range(m, W - m, WAYPOINT_SPACING):
            pts.append(((x, m), "north"))
            pts.append(((x, H - 1 - m), "south"))
        for y in range(m, H - m, WAYPOINT_SPACING):
            pts.append(((m, y), "west"))
            pts.append(((W - 1 - m, y), "east"))
        _WAYPOINTS[key] = pts
    return _WAYPOINTS[key]


def explore_target(worker, view, me):
    """
    1. Nearest stale waypoint on the lane EDGE_MARGIN tiles inside our NEAREST edge
       (after the split: only on this worker's side, so the two halves sweep away from each other).
    2. Then the other edges' lanes on that side (around the corner).
    3. Then the nearest tile not seen for RESCAN_TURNS (on that side, no deeper than MAX_ORE_DEPTH).
    4. Finally the far end of the worker's side of the edge (or the map center before the split).
    Each waypoint is handed to only one worker per turn so scouts spread out.
    """
    s = state(view, me)
    _refresh(s, me)
    p = pos_of(worker)
    turn = s["turn"]
    primary = _PRIMARY_EDGE
    claims = s["explore_claims"]
    side = _SIDE.get(worker["unitId"]) if _split_active() else None

    def stale(q):
        seen = LAST_SEEN.get(q)
        return seen is None or turn - seen >= RESCAN_TURNS

    def allowed(q):
        if side is None:
            return True
        if not _on_side(q, side):
            return False
        return MAX_ORE_DEPTH is None or _depth(q, s) <= MAX_ORE_DEPTH

    best, best_score = None, None
    for q, edge in _waypoints(s["W"], s["H"]):
        if q in claims or not stale(q) or not allowed(q):
            continue
        score = cheb(p, q) + (0 if edge == primary else OFF_EDGE_PENALTY)
        if best_score is None or score < best_score:
            best, best_score = q, score
    if best is None:
        for x in range(0, s["W"], 2):
            for y in range(0, s["H"], 2):
                q = (x, y)
                if q in claims or not stale(q) or not allowed(q):
                    continue
                d = cheb(p, q)
                if best_score is None or d < best_score:
                    best, best_score = q, d
    if best is None:
        if side is not None:
            length = s["W"] if primary in ("north", "south") else s["H"]
            a = EDGE_MARGIN if side == "low" else length - 1 - EDGE_MARGIN
            best = _edge_tile(a, EDGE_MARGIN, s)
        else:
            best = (s["W"] // 2, s["H"] // 2)
    claims.add(best)
    return best
