#!/usr/bin/env python3
"""
bot_template.py - MASTER (shared, edit only together).

Asks the questions, never answers them:
    economy.py   (Person A) -> "SHOULD we?"   money + time
    expansion.py (Person B) -> "WHERE?"       map
    common.py    (shared)   -> memory, movement, territory helpers

Every worker runs the checklist top to bottom each turn; the first "yes" is its action.
"""
from __future__ import annotations

import os

from bot_client import ActionsByEntity, BotClient, BotState, Identity, mars_view

import common
import economy
import expansion
from common import (  # re-exported so older code that imported helpers from here still works
    can_move_onto, is_base, is_claimed, is_ore, path_to,
    ORE_MEMORY, BASE_COST, can_mine_here, capacity, inventory, load, pos_of,
    step_home, step_toward, worker_cost,
)

# Make sure to set your team token! (never commit it)
API_KEY = os.environ.get("API_KEY", "replace-with-api-key")
API_BASE_URL = os.environ.get("LUCID_API_BASE_URL", "http://localhost:5173")


def calculate_actions(gameState: BotState, identity: Identity) -> ActionsByEntity:
    view = mars_view(gameState)
    if view is None:
        return {}
    me = identity["teamId"]
    s = common.begin_turn(view, me)
    turn = s["turn"]

    economy.debug_status(view, me)
    iron_left = s["bank_iron"]                      # spent as we order spawns/bases this turn
    want_iron = economy.iron_wanted(view, me, iron_left)
    # Don't SKIP cooling-down workers (server ignores those actions anyway); just let ready ones act first.
    workers = sorted(s["my_workers"], key=lambda w: (w.get("readyOnTurn", 0) > turn, w["unitId"]))
    taken = {pos_of(w) for w in workers if pos_of(w) in ORE_MEMORY}   # ore we already stand on
    actions: dict = {}
    build_tiles: set = set()
    spawned = False

    for w in workers:
        wid = w["unitId"]
        p = pos_of(w)
        gold, iron = inventory(w)
        carry = gold + iron
        full = carry >= capacity(w)
        ending = economy.is_game_ending(view, me, w)

        # 0. Iron is useless late -> 3 iron becomes 1 gold (before depositing it)
        if economy.should_trade_iron(view, me, w):
            actions[wid] = {"type": "trade", "from": "iron"}
            continue

        # 1. On my base and carrying anything -> deposit
        if p in s["my_base_set"] and carry > 0:
            actions[wid] = {"type": "deposit"}
            continue

        # 2. SHOULD we buy a worker? (max one per turn)
        if not spawned and economy.should_build_worker(view, me, w, iron_left):
            iron_left -= worker_cost(len(s["my_workers"]) - 1)
            spawned = True
            actions[wid] = {"type": "spawn"}
            continue

        # 3. SHOULD we build a base AND is THIS tile a good site? (builder must be alone)
        alone = len(s["units_at"].get(p, ())) == 1
        if alone and economy.should_build_base(view, me, w, iron_left) and expansion.is_good_base_site(p, view, me):
            iron_left -= BASE_COST
            s["planned_bases"].append(p)
            build_tiles.add(p)
            actions[wid] = {"type": "build-base"}
            continue

        # 3b. EARLY: carrying enough iron to buy the next worker -> go bank it now
        if economy.should_return_with_iron(view, me, w, iron_left):
            actions[wid] = step_home(p, view, me)
            continue

        # 3c. Rush to the edge (Person B's rush_target; skipped until it exists)
        rush = getattr(expansion, "rush_target", None)
        target = rush(w, view, me) if rush else None
        if target is not None and target != p:
            actions[wid] = step_toward(p, target, view, me)
            continue

        # 4. Time is up and I carry something -> go bank it (checked BEFORE mining so we never miss the deadline)
        if ending and carry > 0:
            actions[wid] = step_home(p, view, me)
            continue

        # 5. Standing on ore I may mine and not full -> mine
        ore = ORE_MEMORY.get(p)
        if ore and not full and can_mine_here(p, view, me):
            # Gold or wanted iron: always. Unwanted iron: only if nothing better is worth walking to.
            if ore["type"] == "gold" or want_iron or expansion.best_ore(w, view, me, taken, want_iron) in (p, None):
                common.note_mine(wid, p, carry)
                actions[wid] = {"type": "mine"}
                continue

        # 6. Full and far from every base -> step to a good base site next to me (saves the walk home)
        if full and economy.should_build_base(view, me, w, iron_left):
            site = expansion.nearby_base_site(w, view, me)
            if site is not None:
                s["planned_bases"].append(site)   # reserve it so no second worker heads there
                actions[wid] = step_toward(p, site, view, me)
                continue

        # 7. Full -> go home
        if full:
            actions[wid] = step_home(p, view, me)
            continue
        if ending:   # empty and no time for another trip
            actions[wid] = dict(common.PASS)
            continue

        # 8. WHERE is the best ore? -> go there
        target = expansion.best_ore(w, view, me, taken, want_iron)
        if target is not None:
            taken.add(target)
            actions[wid] = step_toward(p, target, view, me)
            continue

        # 9. Nothing known to mine but carrying something -> bank it
        if carry > 0:
            actions[wid] = step_home(p, view, me)
            continue

        # 10. WHERE should I scout?
        actions[wid] = step_toward(p, expansion.explore_target(w, view, me), view, me)

    # Planned sites for walking workers were only reservations; real builds are in build_tiles.
    s["planned_bases"] = [b for b in s["planned_bases"] if b in build_tiles]

    # A base fails if anyone else is on the tile -> nobody walks onto a tile being built this turn.
    if build_tiles:
        for w in workers:
            a = actions.get(w["unitId"])
            if a and a["type"] == "move":
                d = next(k for k, v in common.DIRS.items() if v == a["direction"])
                p = pos_of(w)
                if (p[0] + d[0], p[1] + d[1]) in build_tiles:
                    actions[w["unitId"]] = dict(common.PASS)
    return actions


if __name__ == "__main__":
    if "LUCID_API_BASE_URL" not in os.environ:
        print(f"LUCID_API_BASE_URL is not set; using {API_BASE_URL}.")
    try:
        BotClient(API_KEY, API_BASE_URL).connect_and_run_bot(calculate_actions)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(error)
        raise SystemExit(1)