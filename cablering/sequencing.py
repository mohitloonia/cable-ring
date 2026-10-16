"""Ring order: a closed cycle through one point in every room of a zone.

Choosing one candidate point per room plus a visiting order is a generalised
travelling-salesman problem. Zones hold at most ~10 rooms, so we solve it exactly
with Held-Karp dynamic programming over (rooms visited, last point), once per
starting point of the first room. Costs come from the routing graph
(length + 0.5 x bends per leg).

The hub is NOT part of this search: it is placed afterwards on a straight piece of
the finished cable (see optimizer.insert_hub), which adds no length and no bends.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RingPlan:
    cost: float
    order: list[int]      # candidate index per room, in visiting order (a cycle)
    hub: int | None = None  # fixed hub terminal (what-if "move hub"); then order[0] == hub


def best_cycles(D: np.ndarray, clusters: list[list[int]], top: int = 3,
                single_room_cost: float = 1.3) -> list[RingPlan]:
    if not clusters or any(not c for c in clusters):
        return []
    if len(clusters) == 1:     # one room: the hub sits next to the node (tiny out-and-back)
        return [RingPlan(single_room_cost, [c]) for c in clusters[0][:top]]
    plans = []
    for s in clusters[0]:
        cost, rest = _held_karp(D, s, clusters[1:])
        if rest is not None:
            plans.append(RingPlan(cost, [s, *rest]))
    plans.sort(key=lambda p: p.cost)
    return plans[:top]


def best_cycles_dir(D4: np.ndarray, clusters: list[list[int]], top: int = 3,
                    single_room_cost: float = 1.3, max_starts: int = 10) -> list[RingPlan]:
    """Like best_cycles, but costs carry the travel direction (D4[a, da, b, db]).

    Because the routing graph charges a bend for turning at a vertex, the cost of
    leaving point f moving e after arriving moving d is simply D4[f, d, ...] (the turn at
    f is already inside it). So the DP over (rooms visited, point, arrival direction)
    counts every bend at every node, including where the ring closes.
    """
    if not clusters or any(not c for c in clusters):
        return []
    if len(clusters) == 1:
        return [RingPlan(single_room_cost, [c]) for c in clusters[0][:top]]
    best: dict[tuple, float] = {}
    starts = clusters[0][: max_starts if len(clusters) < 7 else min(max_starts, 3)]
    for s in starts:
        for e0 in range(4):
            cost, rest = _held_karp_dir(D4, s, e0, clusters[1:])
            if rest is not None:
                key = (s, *rest)
                if cost < best.get(key, np.inf):
                    best[key] = cost
    plans = sorted((RingPlan(c, list(k)) for k, c in best.items()), key=lambda p: p.cost)
    return plans[:top]


def _held_karp_dir(D4, s, e0, clusters):
    """Cheapest cycle s(leaving e0) -> one point per cluster -> back to s (arriving e0)."""
    m = len(clusters)
    F = np.array([c for cl in clusters for c in cl], dtype=np.int64)
    cl = np.array([i for i, c in enumerate(clusters) for _ in c], dtype=np.int64)
    K = len(F)
    KS = 4 * K                                           # state = point * 4 + direction
    bits = np.repeat((1 << cl).astype(np.int64), 4)
    full = (1 << m) - 1
    sub = D4[np.ix_(F, range(4), F, range(4))].reshape(KS, KS)
    start = D4[s, e0][np.ix_(F, range(4))].reshape(KS)
    back = D4[np.ix_(F, range(4))][:, :, s, e0].reshape(KS)

    dp = np.full((1 << m, KS), np.inf)
    par = np.full((1 << m, KS), -1, dtype=np.int64)
    dp[bits, np.arange(KS)] = start
    idx = np.arange(KS)
    for mask in range(1, full):
        row = dp[mask]
        live = np.nonzero(np.isfinite(row))[0]
        if live.size == 0:
            continue
        tot = row[live][:, None] + sub[live]
        k = tot.argmin(0)
        best = tot[k, idx]
        prev = live[k]
        tgt = np.nonzero(((mask & bits) == 0) & np.isfinite(best))[0]
        if tgt.size == 0:
            continue
        nm = mask | bits[tgt]
        better = best[tgt] < dp[nm, tgt]
        tgt, nm = tgt[better], nm[better]
        dp[nm, tgt] = best[tgt]
        par[nm, tgt] = prev[tgt]

    total = dp[full] + back
    f = int(np.argmin(total))
    cost = float(total[f])
    if not np.isfinite(cost):
        return np.inf, None
    order, mask = [], full
    while f != -1:
        order.append(int(F[f // 4]))
        pf = int(par[mask, f])
        mask ^= int(bits[f])
        f = pf
    order.reverse()
    return cost, order


def _held_karp(D, s, clusters):
    """Cheapest path s -> one point of every cluster -> back to s."""
    m = len(clusters)
    F = np.array([c for cl in clusters for c in cl], dtype=np.int64)
    cl = np.array([i for i, c in enumerate(clusters) for _ in c], dtype=np.int64)
    K = len(F)
    bits = (1 << cl).astype(np.int64)
    full = (1 << m) - 1
    sub = D[np.ix_(F, F)]
    dp = np.full((1 << m, K), np.inf)
    par = np.full((1 << m, K), -1, dtype=np.int64)
    dp[bits, np.arange(K)] = D[s, F]
    for mask in range(1, full):
        row = dp[mask]
        if not np.isfinite(row).any():
            continue
        tot = row[:, None] + sub
        prev = tot.argmin(0)
        best = tot[prev, np.arange(K)]
        tgt = np.nonzero(((mask & bits) == 0) & np.isfinite(best))[0]
        if tgt.size == 0:
            continue
        nm = mask | bits[tgt]
        better = best[tgt] < dp[nm, tgt]
        tgt, nm = tgt[better], nm[better]
        dp[nm, tgt] = best[tgt]
        par[nm, tgt] = prev[tgt]

    total = dp[full] + D[F, s]
    f = int(np.argmin(total))
    cost = float(total[f])
    if not np.isfinite(cost):
        return np.inf, None
    order, mask = [], full
    while f != -1:
        order.append(int(F[f]))
        pf = int(par[mask, f])
        mask ^= int(bits[f])
        f = pf
    order.reverse()
    return cost, order
