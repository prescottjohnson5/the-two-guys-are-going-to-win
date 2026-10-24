#!/usr/bin/env python3
"""
expansion.py - PERSON B - answers "WHERE?"

Never looks at money or prices. Only the map.
The master (bot_template.py) asks:

    is_good_base_site(pos, view, me)                  -> bool         "Is this tile a good place for a base?"
    best_ore(worker, view, me, taken, want_iron=False) -> (x,y)|None  "Which deposit should this worker go mine?"
    explore_target(worker, view, me)                  -> (x,y)        "Nothing to mine - where do I scout?"

Extra:
    nearby_base_site(worker, view, me) -> (x,y)|None   "Full and far from home - is there a site 1-2 steps away?"

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
EDGE_MARGIN = 3          # bases (and the scouting lane) stay this many tiles in from the map edge
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
# =====================================================

_PRIMARY_EDGE = None
_WAYPOINTS = {}


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
    """
    s = state(view, me)
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
    return _site_value(pos, s, me) >= MIN_SITE_VALUE


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


def best_ore(worker, view, me, taken, want_iron=False):
    """
    The deposit with the best kg-per-turn rate:
        rate = kg * value / (walk_there + kg + RETURN_WEIGHT * walk_back_to_base)
    kg = min(deposit amount, free space). Gold value 1.0; iron 1.0 if want_iron else 0.3.
    Skips: tiles in `taken` (other workers already going there), tiles an enemy is standing on,
    and enemy-only territory. The worker's own tile is always allowed, so if it returns the
    worker's position, the answer is "mine right here".
    """
    s = state(view, me)
    p = pos_of(worker)
    free = capacity(worker) - load(worker)
    if free <= 0:
        return None
    enemy_tiles = {q for q, us in s["units_at"].items() if any(u.get("teamId") != me for u in us)}
    best, best_rate = None, 0.0
    for q, ore in ORE_MEMORY.items():
        if q != p and (q in taken or q in enemy_tiles):
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


def _primary_edge(s):
    global _PRIMARY_EDGE
    if _PRIMARY_EDGE is None and s["my_bases"]:
        x, y = s["my_bases"][0]
        gaps = {"west": x, "east": s["W"] - 1 - x, "north": y, "south": s["H"] - 1 - y}
        _PRIMARY_EDGE = min(gaps, key=gaps.get)
    return _PRIMARY_EDGE


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
    1. Nearest stale waypoint on the lane EDGE_MARGIN tiles inside our NEAREST edge.
    2. Then the other edges' lanes (sweeping along the edge and around the corners).
    3. Then the nearest tile not seen for RESCAN_TURNS anywhere; finally the map center.
    Each waypoint is handed to only one worker per turn so scouts spread out.
    """
    s = state(view, me)
    p = pos_of(worker)
    turn = s["turn"]
    primary = _primary_edge(s)
    claims = s["explore_claims"]

    def stale(q):
        seen = LAST_SEEN.get(q)
        return seen is None or turn - seen >= RESCAN_TURNS

    best, best_score = None, None
    for q, edge in _waypoints(s["W"], s["H"]):
        if q in claims or not stale(q):
            continue
        score = cheb(p, q) + (0 if edge == primary else OFF_EDGE_PENALTY)
        if best_score is None or score < best_score:
            best, best_score = q, score
    if best is None:
        for x in range(0, s["W"], 2):
            for y in range(0, s["H"], 2):
                q = (x, y)
                if q in claims or not stale(q):
                    continue
                d = cheb(p, q)
                if best_score is None or d < best_score:
                    best, best_score = q, d
    if best is None:
        best = (s["W"] // 2, s["H"] // 2)
    claims.add(best)
    return best
