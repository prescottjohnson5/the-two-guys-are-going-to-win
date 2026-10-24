#!/usr/bin/env python3
"""
common.py - SHARED helpers (edit only together).

Holds everything both people need but neither "owns":
  * map memory that survives between turns (ore, terrain, bases, last-seen)
  * one per-turn snapshot `S` (built once by the master with begin_turn)
  * movement: step_toward / step_home (fast A* + a "distance-to-home" field)
  * territory: can_mine_here / in_enemy_territory
  * the original game helpers from the starter (path_to, is_claimed, ...)

economy.py and expansion.py call `state(view, me)` to read the snapshot.
"""
from __future__ import annotations

from collections.abc import Sequence
from heapq import heapify, heappop, heappush
from typing import Optional

# ---------------- Game constants ----------------
MAX_TURNS = 480
BASE_COST = 6
CLAIM_RADIUS = 2            # a base claims ore within Manhattan distance 2
DEFAULT_CAPACITY = 8
OBSTACLE_COST = 3           # entering a mountain/crater = 1 move + 2 cooldown turns
OBSTACLES = ("mountain", "crater")

# Assumes y grows DOWNWARD (north = y-1).
# If workers walk AWAY from their targets in the replay, swap "north"/"south" (and the diagonals) HERE only.
DIRS = {(0, -1): "north", (1, 0): "east", (0, 1): "south", (-1, 0): "west",
        (1, -1): "north-east", (1, 1): "south-east", (-1, 1): "south-west", (-1, -1): "north-west"}
PASS = {"type": "pass"}
_FAR = 10 ** 6

# ---------------- Memory (persists across turns) ----------------
ORE_MEMORY: dict[tuple[int, int], dict] = {}      # (x,y) -> {"type": "gold"|"iron", "amount": int}
TERRAIN_MEMORY: dict[tuple[int, int], str] = {}   # (x,y) -> "empty" | "mountain" | "crater" | "void"
LAST_SEEN: dict[tuple[int, int], int] = {}        # (x,y) -> last turn the tile was visible
BASE_MEMORY: dict[str, tuple[str, tuple[int, int]]] = {}  # baseId -> (teamId, (x,y))
_PENDING_MINES: dict[str, tuple[tuple[int, int], int]] = {}  # workerId -> (tile, load before mining)
_MINE_FAILS: dict[tuple[int, int], int] = {}

# ---------------- Per-turn snapshot ----------------
S: dict = {}


# ================= Small utilities =================
def worker_cost(n_built: int) -> int:
    """Iron cost of the next worker: 5, 6, 11, 17, 28, 45... (n_built = workers bought so far)."""
    a, b = 5, 6
    for _ in range(max(0, n_built)):
        a, b = b, a + b
    return a


def cheb(a, b) -> int:
    """Moves needed with 8-direction movement (ignoring obstacles)."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def manhattan(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def pos_of(entity) -> tuple[int, int]:
    p = entity["position"]
    return (p["x"], p["y"])


def inventory(worker) -> tuple[int, int]:
    """(gold, iron) carried."""
    inv = worker.get("inventory") or {}
    return (inv.get("gold", 0) or 0, inv.get("iron", 0) or 0)


def load(worker) -> int:
    g, i = inventory(worker)
    return g + i


def capacity(worker) -> int:
    return worker.get("inventoryCapacity") or DEFAULT_CAPACITY


# ================= Per-turn state =================
def state(view, me) -> dict:
    """Return this turn's snapshot, building it if needed. Safe to call from anywhere."""
    if S.get("view") is not view or S.get("me") != me:
        begin_turn(view, me)
    return S


def begin_turn(view, me) -> dict:
    """Update memory from the new view and build the snapshot. The master calls this once per turn."""
    turn = view["turn"]
    W, H = view["map"]["width"], view["map"]["height"]
    cells = view["map"]["cells"]

    # 1) Remember what we can see (ore under a worker shows as "unit", so keep old memory there).
    for i, c in enumerate(cells):
        t = c.get("type")
        if t is None or t == "hidden":
            continue
        p = (i % W, i // W)
        LAST_SEEN[p] = turn
        if t in ("gold", "iron"):
            amt = c.get("amount", 0) or 0
            if amt > 0:
                ORE_MEMORY[p] = {"type": t, "amount": amt}
                _MINE_FAILS.pop(p, None)
            else:
                ORE_MEMORY.pop(p, None)
            TERRAIN_MEMORY[p] = "empty"
        elif t != "unit":
            TERRAIN_MEMORY[p] = t
            ORE_MEMORY.pop(p, None)

    units = view.get("units") or []
    my_workers = [u for u in units if u.get("teamId") == me]

    # 2) Mining feedback: if a worker "mined" twice in a row and got nothing, the deposit is gone.
    for w in my_workers:
        rec = _PENDING_MINES.get(w["unitId"])
        if rec and pos_of(w) == rec[0]:
            if load(w) <= rec[1]:
                _MINE_FAILS[rec[0]] = _MINE_FAILS.get(rec[0], 0) + 1
                if _MINE_FAILS[rec[0]] >= 2:
                    ORE_MEMORY.pop(rec[0], None)
            else:
                _MINE_FAILS.pop(rec[0], None)
    _PENDING_MINES.clear()

    # 3) Bases (remembered forever, so hidden enemy bases still count).
    for b in view.get("bases") or []:
        bp = pos_of(b)
        BASE_MEMORY[b.get("baseId") or f"{b['teamId']}@{bp}"] = (b["teamId"], bp)
    my_bases = sorted({p for t, p in BASE_MEMORY.values() if t == me})
    enemy_bases = {p for t, p in BASE_MEMORY.values() if t != me}

    claims: dict[tuple[int, int], set] = {}
    for team, bp in BASE_MEMORY.values():
        for dx in range(-CLAIM_RADIUS, CLAIM_RADIUS + 1):
            r = CLAIM_RADIUS - abs(dx)
            for dy in range(-r, r + 1):
                claims.setdefault((bp[0] + dx, bp[1] + dy), set()).add(team)

    units_at: dict[tuple[int, int], list] = {}
    for u in units:
        units_at.setdefault(pos_of(u), []).append(u)

    bank = (view.get("teamResources") or {}).get(me) or {}
    S.clear()
    S.update(
        view=view, me=me, turn=turn, W=W, H=H, cells=cells,
        units=units, my_workers=my_workers, units_at=units_at,
        my_bases=my_bases, my_base_set=set(my_bases), enemy_bases=enemy_bases,
        claims=claims,
        bank_iron=bank.get("iron", 0) or 0, bank_gold=bank.get("gold", 0) or 0,
        planned_bases=[],        # bases ordered THIS turn (so two workers don't build side by side)
        explore_claims=set(),    # explore targets already handed out THIS turn
    )
    S["home"] = _home_field()
    return S


# ================= Terrain / movement =================
def in_bounds(p) -> bool:
    return 0 <= p[0] < S["W"] and 0 <= p[1] < S["H"]


def passable(p) -> bool:
    """For PLANNING (may cross unseen tiles, assumed plain)."""
    return in_bounds(p) and TERRAIN_MEMORY.get(p) != "void" and p not in S["enemy_bases"]


def enter_cost(p) -> int:
    return OBSTACLE_COST if TERRAIN_MEMORY.get(p) in OBSTACLES else 1


def can_step(p) -> bool:
    """For the ACTUAL move this turn (uses the live view)."""
    if not in_bounds(p) or p in S["enemy_bases"]:
        return False
    t = S["cells"][p[1] * S["W"] + p[0]].get("type")
    return t not in ("hidden", "void")


def _home_field() -> dict:
    """Multi-source Dijkstra: cost (in turns) from every tile to its closest friendly base."""
    dist = {b: 0 for b in S["my_bases"]}
    heap = [(0, b) for b in S["my_bases"]]
    heapify(heap)
    while heap:
        d, p = heappop(heap)
        if d != dist[p]:
            continue
        nd = d + enter_cost(p)  # cost for a neighbour to step INTO p
        for dx, dy in DIRS:
            q = (p[0] + dx, p[1] + dy)
            if nd < dist.get(q, _FAR) and passable(q):
                dist[q] = nd
                heappush(heap, (nd, q))
    return dist


def home_distance(p) -> int:
    """Turns needed to walk from p to the nearest friendly base."""
    d = S["home"].get(p)
    if d is not None:
        return d
    return min((cheb(p, b) for b in S["my_bases"]), default=_FAR)


def _astar_first_step(start, goal):
    if start == goal or not passable(goal):
        return None
    g = {start: 0}
    came = {}
    heap = [(cheb(start, goal), 0, start)]
    budget = 6000
    while heap and budget:
        budget -= 1
        _, gc, p = heappop(heap)
        if gc != g[p]:
            continue
        if p == goal:
            break
        for dx, dy in DIRS:
            q = (p[0] + dx, p[1] + dy)
            if not passable(q):
                continue
            nd = gc + enter_cost(q)
            if nd < g.get(q, _FAR):
                g[q] = nd
                came[q] = p
                heappush(heap, (nd + cheb(q, goal), nd, q))
    if goal not in came:
        return None
    p = goal
    while came[p] != start:
        p = came[p]
    return p


def _move(pos, nxt):
    if nxt is None:
        return dict(PASS)
    return {"type": "move", "direction": DIRS[(nxt[0] - pos[0], nxt[1] - pos[1])]}


def _greedy(pos, score, avoid):
    best, best_s = None, None
    for dx, dy in DIRS:
        q = (pos[0] + dx, pos[1] + dy)
        if q in avoid or not can_step(q):
            continue
        s = score(q) + (OBSTACLE_COST - 1 if TERRAIN_MEMORY.get(q) in OBSTACLES else 0)
        if best_s is None or s < best_s:
            best, best_s = q, s
    return best


def step_toward(pos, target, view, me, avoid=frozenset()) -> dict:
    """One move action from pos toward target (A*, falls back to greedy)."""
    state(view, me)
    if pos == target:
        return dict(PASS)
    nxt = _astar_first_step(pos, target)
    if nxt is None or nxt in avoid or not can_step(nxt):
        nxt = _greedy(pos, lambda q: cheb(q, target), avoid)
    return _move(pos, nxt)


def step_home(pos, view, me, avoid=frozenset()) -> dict:
    """One move action toward the nearest friendly base (follows the home field downhill)."""
    state(view, me)
    if pos in S["my_base_set"] or not S["my_bases"]:
        return dict(PASS)
    nxt = _greedy(pos, lambda q: S["home"].get(q, _FAR), avoid)
    if nxt is None or S["home"].get(nxt, _FAR) >= _FAR:
        return step_toward(pos, nearest_base(pos, view, me), view, me, avoid)
    return _move(pos, nxt)


def nearest_base(pos, view, me):
    state(view, me)
    return min(S["my_bases"], key=lambda b: cheb(b, pos)) if S["my_bases"] else None


# ================= Territory =================
def can_mine_here(pos, view, me) -> bool:
    """Allowed if unclaimed, or we are among the claimers (overlap = both may mine)."""
    teams = state(view, me)["claims"].get(pos)
    return not teams or me in teams


def in_enemy_territory(pos, view, me) -> bool:
    teams = state(view, me)["claims"].get(pos, ())
    return any(t != me for t in teams)


def note_mine(worker_id, pos, load_before) -> None:
    """Master calls this when it orders a mine, so memory stays accurate while we stand on the ore."""
    _PENDING_MINES[worker_id] = (pos, load_before)
    ore = ORE_MEMORY.get(pos)
    if ore:
        ore["amount"] -= 1
        if ore["amount"] <= 0:
            ORE_MEMORY.pop(pos, None)


# ================= Original starter helpers (kept for compatibility) =================
def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cell(grid, point):
    x, y = point["x"], point["y"]
    if not _is_integer(x) or not _is_integer(y):
        return None
    if not (0 <= x < grid["width"] and 0 <= y < grid["height"]):
        return None
    return grid["cells"][y * grid["width"] + x]


def is_claimed(state_, coordinate) -> list[str]:
    teams: list[str] = []
    for base in state_["bases"]:
        position = base["position"]
        distance = abs(position["x"] - coordinate["x"]) + abs(position["y"] - coordinate["y"])
        if distance <= 2 and base["teamId"] not in teams:
            teams.append(base["teamId"])
    return teams


def is_base(state_, coordinate) -> Optional[str]:
    for base in state_["bases"]:
        if base["position"] == coordinate:
            return base["teamId"]
    return None


def is_ore(state_, coordinate):
    cell = _cell(state_["map"], coordinate)
    if cell and cell["type"] in ("gold", "iron") and cell.get("amount", 0) > 0:
        return cell["type"]
    return None


def can_move_onto(state_, coordinate, team_id: Optional[str] = None) -> bool:
    cell = _cell(state_["map"], coordinate)
    if cell is None or cell["type"] in ("hidden", "void"):
        return False
    base_owner = is_base(state_, coordinate)
    return base_owner is None or base_owner == team_id


def path_to(grid, start, end, *, bases: Sequence = (), team_id: Optional[str] = None,
            obstacle_cost: int = 3):
    if not _is_integer(obstacle_cost) or obstacle_cost < 1:
        return None
    st = {"map": grid, "bases": bases}
    if not can_move_onto(st, start, team_id) or not can_move_onto(st, end, team_id):
        return None
    origin, target = (start["x"], start["y"]), (end["x"], end["y"])
    distances = {origin: 0}
    previous = {}
    queue = [(0, origin)]
    while queue:
        distance, position = heappop(queue)
        if distance != distances[position]:
            continue
        if position == target:
            break
        x, y = position
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if not dx and not dy:
                    continue
                point = {"x": x + dx, "y": y + dy}
                if not can_move_onto(st, point, team_id):
                    continue
                neighbor = (point["x"], point["y"])
                cell = _cell(grid, point)
                cost = obstacle_cost if cell and cell["type"] in ("mountain", "crater") else 1
                cand = distance + cost
                if cand < distances.get(neighbor, float("inf")):
                    distances[neighbor] = cand
                    previous[neighbor] = position
                    heappush(queue, (cand, neighbor))
    if target not in distances:
        return None
    path = []
    position = target
    while position != origin:
        path.append({"x": position[0], "y": position[1]})
        position = previous[position]
    return list(reversed(path))
