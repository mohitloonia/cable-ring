"""Zones: split the eligible rooms into the fewest connected, capacity-feasible groups.

Plans have at most ~10 eligible rooms, so we enumerate every valid split exactly
(no heuristic, no solver): a group is valid when its rooms are connected through
doors/open passages, total area <= 100 m2 and node count <= max_nodes.
Lower bound on hubs = max(ceil(area / 100), ceil(nodes / max_nodes)).
"""
from __future__ import annotations

import math

from .room_graph import zone_connected

MAX_EXACT_ROOMS = 14   # 2^14 subsets; real plans have <= 10 eligible rooms


def lower_bound(areas: list[float], cfg) -> int:
    n = len(areas)
    return max(1, math.ceil(sum(areas) / cfg.max_area_m2 - 1e-9), math.ceil(n / cfg.max_nodes))


def valid_groups(room_ids: list[str], areas: list[float], G, cfg) -> list[bool]:
    n = len(room_ids)
    if n > MAX_EXACT_ROOMS:
        raise ValueError(f"{n} eligible rooms: too many for exact zoning")
    ok = [False] * (1 << n)
    for mask in range(1, 1 << n):
        idx = [i for i in range(n) if mask >> i & 1]
        if len(idx) > cfg.max_nodes:
            continue
        if sum(areas[i] for i in idx) > cfg.max_area_m2 + cfg.tol_m:
            continue
        ok[mask] = zone_connected(G, [room_ids[i] for i in idx], cfg.zone_via_other_rooms)
    return ok


def partitions(n: int, areas: list[float], ok: list[bool], k: int, max_area: float,
               cap: int = 20000) -> list[list[int]]:
    """All splits of rooms 0..n-1 into exactly k valid groups (as bit masks)."""
    out: list[list[int]] = []

    def rec(remaining: int, k_left: int, acc: list[int]) -> None:
        if len(out) >= cap:
            return
        if remaining == 0:
            if k_left == 0:
                out.append(acc.copy())
            return
        if k_left == 0:
            return
        rest_area = sum(areas[i] for i in range(n) if remaining >> i & 1)
        if rest_area > k_left * max_area + 1e-6:
            return
        low = remaining & -remaining          # the lowest room must go into the next group
        sub = remaining
        while sub:
            if sub & low and ok[sub]:
                acc.append(sub)
                rec(remaining ^ sub, k_left - 1, acc)
                acc.pop()
            sub = (sub - 1) & remaining

    rec((1 << n) - 1, k, [])
    return out


def rooms_of(mask: int, room_ids: list[str]) -> list[str]:
    return [room_ids[i] for i in range(len(room_ids)) if mask >> i & 1]
