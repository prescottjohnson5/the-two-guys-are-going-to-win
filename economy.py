#!/usr/bin/env python3
"""
economy.py - PERSON A - answers "SHOULD we?"

Never looks at the map layout. Only money (bank iron, worker prices) and time (turn number).

THE PLAN
  EARLY game (until we have EARLY_WORKER_TARGET workers OR turn EARLY_END_TURN):
      - spend ALL iron on workers, no reserve
      - one base allowed early (the edge base) when a worker can't be afforded
      - mine iron first
  MID game:
      - buy a worker if we can afford cost + IRON_RESERVE and cost < turns_left / WORKER_PAYBACK_DIVISOR
      - otherwise a base if 6 iron (expansion decides WHERE)
      - prefer gold; still want iron while the next worker is worth buying
  END game (per worker):
      - head home when turns_left <= walk home + trades + 1 + END_BUFFER
      - trade carried iron to gold once iron can't be spent
      - bases still allowed until STOP_BASES_TURN (a base can save a long walk home)

The master (bot_template.py) asks:
    should_build_worker(view, me, worker, iron_left) -> bool
    should_build_base(view, me, worker, iron_left)   -> bool
    is_game_ending(view, me, worker)                 -> bool
    iron_wanted(view, me, iron_left)                 -> bool
    should_trade_iron(view, me, worker)              -> bool

`iron_left` = bank iron NOT yet spent this turn (the master subtracts each spawn/build it orders).
"""
from __future__ import annotations

from common import BASE_COST, MAX_TURNS, home_distance, inventory, pos_of, state, worker_cost

# ======================= KNOBS (change ONE at a time when testing) =======================
EARLY_WORKER_TARGET = 4     # early game ends when we have this many workers...
EARLY_END_TURN = 100        # ...or at this turn, whichever comes first
EARLY_MAX_BASES = 1         # bases allowed during early game (the edge base)

WORKER_PAYBACK_DIVISOR = 6  # mid game: buy a worker only if cost < turns_left / this
IRON_RESERVE = 6            # mid game: keep enough iron for one base when buying a worker

STOP_BASES_TURN = 440       # no new bases from this turn on
MAX_BASES = 14              # hard cap on bases

END_BUFFER = 3              # extra safety turns on the final walk home
# ==========================================================================================


# ---------------- Basic facts ----------------
def _workers_bought(s) -> int:
    return max(0, len(s["my_workers"]) - 1)   # we start with 1 free worker


def _turns_left(s) -> int:
    return MAX_TURNS - s["turn"]


def _base_count(s) -> int:
    return len(s["my_bases"]) + len(s["planned_bases"])


def phase(view, me) -> str:
    """'early' or 'mid'. End game is decided per worker by is_game_ending()."""
    s = state(view, me)
    if s["turn"] < EARLY_END_TURN and len(s["my_workers"]) < EARLY_WORKER_TARGET:
        return "early"
    return "mid"


def _worker_worth_it(s) -> bool:
    """Mid-game rule: next worker pays itself back before the game ends."""
    return worker_cost(_workers_bought(s)) < _turns_left(s) / WORKER_PAYBACK_DIVISOR


# ---------------- The questions ----------------
def should_build_worker(view, me, worker, iron_left) -> bool:
    """
    EARLY: yes whenever we can afford it (spend everything).
    MID:   yes if iron_left >= cost + IRON_RESERVE and cost < turns_left / WORKER_PAYBACK_DIVISOR.
    The master makes sure at most ONE worker is bought per turn.
    """
    s = state(view, me)
    cost = worker_cost(_workers_bought(s))
    if phase(view, me) == "early":
        return iron_left >= cost
    return iron_left >= cost + IRON_RESERVE and _worker_worth_it(s)


def should_build_base(view, me, worker, iron_left) -> bool:
    """
    Can we AFFORD a base right now? (expansion decides if the spot is good.)
    EARLY: only up to EARLY_MAX_BASES extra bases (the edge base), and never with
           iron that could buy the next worker right now.
    MID/END: 6 iron, before STOP_BASES_TURN, under MAX_BASES.
    Workers come first: the master asks should_build_worker before this.
    """
    s = state(view, me)
    if s["turn"] >= STOP_BASES_TURN or iron_left < BASE_COST:
        return False
    if _base_count(s) >= MAX_BASES:
        return False
    if phase(view, me) == "early":
        extra_bases = _base_count(s) - 1   # we start with 1 base
        if extra_bases >= EARLY_MAX_BASES:
            return False
        if iron_left >= worker_cost(_workers_bought(s)):
            return False   # a worker is affordable -> workers win early
    return True


def is_game_ending(view, me, worker) -> bool:
    """
    Per worker: go home when turns_left <= walk_home + trades + 1 (deposit) + END_BUFFER.
    A worker next to a base keeps mining almost to the end; a far one leaves earlier.
    """
    s = state(view, me)
    _, iron = inventory(worker)
    needed = home_distance(pos_of(worker)) + iron // 3 + 1 + END_BUFFER
    return _turns_left(s) <= needed


def iron_wanted(view, me, iron_left) -> bool:
    """
    EARLY: always (iron buys workers).
    MID:   yes while the next worker is still worth buying and we're short of cost + reserve.
           Otherwise gold is preferred (expansion still takes iron if it's very close).
    """
    s = state(view, me)
    if phase(view, me) == "early":
        return True
    if s["turn"] >= STOP_BASES_TURN:
        return False
    carried = sum(inventory(w)[1] for w in s["my_workers"])
    have = iron_left + carried
    if _worker_worth_it(s):
        return have < worker_cost(_workers_bought(s)) + IRON_RESERVE
    return have < BASE_COST and _base_count(s) < MAX_BASES


def should_trade_iron(view, me, worker) -> bool:
    """Carrying >= 3 iron and iron can't be spent anymore (bases stopped, or this worker is heading home for good)."""
    _, iron = inventory(worker)
    if iron < 3:
        return False
    s = state(view, me)
    return s["turn"] >= STOP_BASES_TURN or is_game_ending(view, me, worker)