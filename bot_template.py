#!/usr/bin/env python3
from __future__ import annotations

import os
import random
from heapq import heappop, heappush
from collections.abc import Sequence
from typing import Optional
from bot_client import (
    ActionsByEntity,
    BaseLocation,
    BotClient,
    BotState,
    Coordinate,
    CoordinateGrid,
    GameMap,
    Identity,
    MarsView,
    MovementState,
    OreState,
    Resource,
    TerritoryState,
    mars_view,
)

# Make sure to set your team token!
API_KEY = os.environ.get("API_KEY", "replace-with-api-key")
API_BASE_URL = os.environ.get("LUCID_API_BASE_URL", "http://localhost:5173")


# ---------------- Strategy ----------------

ORE_MEMORY = {}  # (x, y) -> "gold" | "iron"
# Assumes y increases going DOWN (north = y-1). If workers walk away from ore, swap north/south.
DIRS = {(0, -1): "north", (1, 0): "east", (0, 1): "south", (-1, 0): "west",
        (1, -1): "north-east", (1, 1): "south-east", (-1, 1): "south-west", (-1, -1): "north-west"}
MAX_TURNS = 480


def worker_cost(n_built):
    a, b = 5, 6
    for _ in range(n_built):
        a, b = b, a + b
    return a


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def calculate_actions(gameState: BotState, identity: Identity) -> ActionsByEntity:
    view = mars_view(gameState)
    if view is None:
        return {}
    me = identity["teamId"]
    turn = view["turn"]
    W, H = view["map"]["width"], view["map"]["height"]

    # Remember ore we've seen (ore under a worker shows as a unit cell)
    for i, c in enumerate(view["map"]["cells"]):
        p = (i % W, i // W)
        t = c["type"]
        if t in ("gold", "iron"):
            if c.get("amount", 0) > 0:
                ORE_MEMORY[p] = t
            else:
                ORE_MEMORY.pop(p, None)
        elif t in ("empty", "mountain", "crater"):
            ORE_MEMORY.pop(p, None)

    my_bases = [(b["position"]["x"], b["position"]["y"]) for b in view["bases"] if b["teamId"] == me]
    my_workers = [u for u in view["units"] if u["teamId"] == me]
    iron = view["teamResources"].get(me, {"iron": 0})["iron"]
    next_cost = worker_cost(len(my_workers) - 1)

    def allowed(p):
        claims = is_claimed(view, {"x": p[0], "y": p[1]})
        return not claims or me in claims

    def step(p, target):
        path = path_to(view["map"], {"x": p[0], "y": p[1]}, {"x": target[0], "y": target[1]},
                       bases=view["bases"], team_id=me)
        if path:
            nxt = (path[0]["x"], path[0]["y"])
        else:  # greedy fallback when target is hidden/unreachable
            dx = (target[0] > p[0]) - (target[0] < p[0])
            dy = (target[1] > p[1]) - (target[1] < p[1])
            nxt = (p[0] + dx, p[1] + dy)
        d = (nxt[0] - p[0], nxt[1] - p[1])
        if d in DIRS and can_move_onto(view, {"x": nxt[0], "y": nxt[1]}, me):
            return {"type": "move", "direction": DIRS[d]}
        opts = [k for k in DIRS if can_move_onto(view, {"x": p[0] + k[0], "y": p[1] + k[1]}, me)]
        return {"type": "move", "direction": DIRS[random.choice(opts)]} if opts else {"type": "pass"}

    actions, taken, spawned = {}, set(), False
    for w in my_workers:
        wid = w["unitId"]
        p = (w["position"]["x"], w["position"]["y"])
        if w.get("readyOnTurn", 0) > turn:
            actions[wid] = {"type": "pass"}
            continue
        inv = w.get("inventory") or {"gold": 0, "iron": 0}
        carried = inv["gold"] + inv["iron"]
        cap = w.get("inventoryCapacity", 8)
        home = min(my_bases, key=lambda b: cheb(b, p)) if my_bases else None
        turns_left = MAX_TURNS - turn

        # Late game: convert carried iron to gold
        if turns_left < 40 and inv["iron"] >= 3:
            actions[wid] = {"type": "trade", "from": "iron"}
            continue
        # Deposit on base
        if p in my_bases and carried > 0:
            actions[wid] = {"type": "deposit"}
            continue
        # Spawn a worker when affordable
        if p in my_bases and not spawned and iron >= next_cost and turn < 380 and len(my_workers) < 12:
            actions[wid] = {"type": "spawn"}
            spawned = True
            iron -= next_cost
            continue
        # Mine where we stand
        here = ORE_MEMORY.get(p)
        if here and carried < cap and allowed(p) and not (here == "iron" and turn > 380):
            actions[wid] = {"type": "mine"}
            taken.add(p)
            continue
        # Head home if full or game ending
        if home and (carried >= cap or (carried > 0 and turns_left <= cheb(home, p) + 3)):
            actions[wid] = step(p, home)
            continue
        # Find nearest ore (gold preferred)
        best, best_score = None, 10 ** 9
        for q, t in ORE_MEMORY.items():
            if q in taken or not allowed(q):
                continue
            if t == "iron" and turn > 380:
                continue
            s = cheb(p, q) + (0 if t == "gold" else 4)
            if s < best_score:
                best, best_score = q, s
        if best:
            taken.add(best)
            actions[wid] = step(p, best)
            continue
        if carried > 0 and home:
            actions[wid] = step(p, home)
            continue
        # Explore toward rich center, then wander
        center = (W // 2, H // 2)
        if cheb(p, center) > 3:
            actions[wid] = step(p, center)
        else:
            actions[wid] = step(p, (random.randrange(W), random.randrange(H)))
    return actions


# Game helpers. Feel free to use, discard, or modify.
# Fill in the strategy section above.

def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cell(grid: CoordinateGrid | GameMap, point: Coordinate):
    x, y = point["x"], point["y"]
    if not _is_integer(x) or not _is_integer(y):
        return None
    if not (0 <= x < grid["width"] and 0 <= y < grid["height"]):
        return None

    row_start = y * grid["width"]
    return grid["cells"][row_start + x]


def is_claimed(state: TerritoryState | MarsView, coordinate: Coordinate) -> list[str]:
    """Return all known claiming teams, including overlapping claims."""
    teams: list[str] = []

    for base in state["bases"]:
        position = base["position"]
        distance = abs(position["x"] - coordinate["x"]) + abs(position["y"] - coordinate["y"])
        if distance <= 2 and base["teamId"] not in teams:
            teams.append(base["teamId"])

    return teams


def is_base(state: TerritoryState | MarsView, coordinate: Coordinate) -> Optional[str]:
    for base in state["bases"]:
        if base["position"] == coordinate:
            return base["teamId"]
    return None


def is_ore(state: OreState | MarsView, coordinate: Coordinate) -> Optional[Resource]:
    cell = _cell(state["map"], coordinate)
    if cell and cell["type"] in ("gold", "iron") and cell.get("amount", 0) > 0:
        return cell["type"]
    return None


def can_move_onto(
    state: MovementState | MarsView,
    coordinate: Coordinate,
    team_id: Optional[str] = None,
) -> bool:
    """Unknown terrain and enemy bases are excluded; workers do not block."""
    cell = _cell(state["map"], coordinate)
    if cell is None or cell["type"] in ("hidden", "void"):
        return False

    base_owner = is_base(state, coordinate)
    return base_owner is None or base_owner == team_id


def path_to(
    grid: CoordinateGrid | GameMap,
    start: Coordinate,
    end: Coordinate,
    *,
    bases: Sequence[BaseLocation] = (),
    team_id: Optional[str] = None,
    obstacle_cost: int = 3,
) -> Optional[list[Coordinate]]:
    """Cost until ready at destination; start must be ready; None if unreachable."""
    if not _is_integer(obstacle_cost) or obstacle_cost < 1:
        return None

    state: MovementState = {"map": grid, "bases": bases}
    if not can_move_onto(state, start, team_id) or not can_move_onto(state, end, team_id):
        return None

    origin = (start["x"], start["y"])
    target = (end["x"], end["y"])
    distances = {origin: 0}
    previous: dict[tuple[int, int], tuple[int, int]] = {}
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
                point: Coordinate = {"x": x + dx, "y": y + dy}
                if not can_move_onto(state, point, team_id):
                    continue

                neighbor = (point["x"], point["y"])
                cell = _cell(grid, point)
                movement_cost = obstacle_cost if cell and cell["type"] in ("mountain", "crater") else 1
                candidate = distance + movement_cost
                if candidate < distances.get(neighbor, float("inf")):
                    distances[neighbor] = candidate
                    previous[neighbor] = position
                    heappush(queue, (candidate, neighbor))

    if target not in distances:
        return None

    path: list[Coordinate] = []
    position = target
    while position != origin:
        path.append({"x": position[0], "y": position[1]})
        position = previous[position]

    return list(reversed(path))


if __name__ == "__main__":
    print("Edit calculate_actions to choose your workers' actions.")
    if "LUCID_API_BASE_URL" not in os.environ:
        print(f"LUCID_API_BASE_URL is not set; using {API_BASE_URL}.")

    try:
        BotClient(API_KEY, API_BASE_URL).connect_and_run_bot(calculate_actions)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(error)
        raise SystemExit(1)