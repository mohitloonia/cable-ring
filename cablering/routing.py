"""Axis-aligned routing graph with bend-aware shortest paths.

The graph is a "Hanan-style" grid: only the horizontal and vertical lines that
matter (corners of the free space, door centre lines and parallel lanes, every
candidate point). A vertex is a line intersection inside the free space; an edge
joins neighbouring vertices when the segment between them is fully in free space.
So every route is horizontal/vertical by construction and uses exact coordinates.

Bends: each vertex exists twice, once for "moving horizontally" and once for
"moving vertically". Switching between the two costs `w_bend`, so Dijkstra
minimises  length + 0.5 x bends,  the same trade-off as the score.

Penalties (for rerouting one leg against the others):
  * segment already used by another leg  -> + w_overlap x length
  * vertex already touched by another leg -> + w_cross
  * vertex that is another node or hub    -> blocked
"""
from __future__ import annotations

import bisect

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .geometry import parts

COORD_TOL = 0.01      # coordinates closer than this become one grid line
BLOCKED = 1e6


def _merge(vals, tol: float = COORD_TOL) -> list[float]:
    v = np.sort(np.asarray(vals, dtype=float))
    out = [float(v[0])]
    for x in v[1:]:
        if x - out[-1] > tol:
            out.append(float(x))
    return out


def _snap(lines: list[float], val: float, tol: float = COORD_TOL) -> float:
    i = bisect.bisect_left(lines, val)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(lines) and abs(lines[j] - val) <= tol:
            if best is None or abs(lines[j] - val) < abs(best - val):
                best = lines[j]
    if best is not None:
        return best
    lines.insert(i, float(val))
    return float(val)


class RoutingGraph:
    def __init__(self, space, terminals, guide_x, guide_y, w_bend: float):
        shapely.prepare(space)
        xs, ys = list(guide_x), list(guide_y)
        for p in parts(space):
            for ring in (p.exterior, *p.interiors):
                c = np.asarray(ring.coords)
                xs.extend(c[:, 0])
                ys.extend(c[:, 1])
        xs, ys = _merge(xs), _merge(ys)
        # terminals snap onto existing lines (<= 1 cm) or add their own lines
        self.terminals = [(_snap(xs, x), _snap(ys, y)) for x, y in terminals]
        self.xs, self.ys = np.array(xs), np.array(ys)
        nx_, ny_ = len(xs), len(ys)

        X, Y = np.meshgrid(self.xs, self.ys, indexing="ij")
        inside = shapely.intersects_xy(space, X.ravel(), Y.ravel()).reshape(nx_, ny_)
        flat = np.flatnonzero(inside.ravel())
        vid = -np.ones(nx_ * ny_, dtype=np.int64)
        vid[flat] = np.arange(flat.size)
        vid = vid.reshape(nx_, ny_)
        self.N = N = int(flat.size)
        self.vx, self.vy = X.ravel()[flat], Y.ravel()[flat]

        # ---- undirected segments between neighbouring vertices -----------
        seg_a, seg_b, seg_len = [], [], []
        for axis in (0, 1):
            if axis == 0:
                both = inside[:-1, :] & inside[1:, :]
                I, J = np.nonzero(both)
                p, q = (I, J), (I + 1, J)
            else:
                both = inside[:, :-1] & inside[:, 1:]
                I, J = np.nonzero(both)
                p, q = (I, J), (I, J + 1)
            if I.size == 0:
                seg_a.append(np.zeros(0, np.int64)); seg_b.append(np.zeros(0, np.int64))
                seg_len.append(np.zeros(0)); continue
            coords = np.stack([np.stack([X[p], Y[p]], -1), np.stack([X[q], Y[q]], -1)], 1)
            ok = shapely.covers(space, shapely.linestrings(coords))
            seg_a.append(vid[p][ok]); seg_b.append(vid[q][ok])
            seg_len.append((np.abs(X[q] - X[p]) + np.abs(Y[q] - Y[p]))[ok])
        self.seg_axis = np.concatenate([np.zeros(len(seg_a[0]), np.int8), np.ones(len(seg_a[1]), np.int8)])
        self.seg_a = np.concatenate(seg_a)
        self.seg_b = np.concatenate(seg_b)
        self.seg_len = np.concatenate(seg_len)
        self.S = S = len(self.seg_a)
        self.seg_of = {(int(min(a, b)), int(max(a, b))): s
                       for s, (a, b) in enumerate(zip(self.seg_a, self.seg_b))}

        self._build_states(w_bend)

        # ---- terminal vertices ---------------------------------------------
        self.term_vid = []
        for x, y in self.terminals:
            i = int(np.searchsorted(self.xs, x)); j = int(np.searchsorted(self.ys, y))
            ok = i < nx_ and j < ny_ and self.xs[i] == x and self.ys[j] == y
            self.term_vid.append(int(vid[i, j]) if ok and vid[i, j] >= 0 else -1)

    # ------------------------------------------------------------------ #
    LAYERS = 2

    def _build_states(self, w_bend: float) -> None:
        """Directed state graph: state v (moving horizontally) / v + N (vertically)."""
        N, S = self.N, self.S
        off = np.where(self.seg_axis == 0, 0, N)
        sid = np.arange(S)
        verts = np.arange(N)
        u = np.concatenate([self.seg_a + off, self.seg_b + off, verts, verts + N])
        v = np.concatenate([self.seg_b + off, self.seg_a + off, verts + N, verts])
        w = np.concatenate([self.seg_len, self.seg_len, np.full(2 * N, w_bend)])
        seg = np.concatenate([sid, sid, -np.ones(2 * N, np.int64)])
        to_vert = np.concatenate([self.seg_b, self.seg_a, verts, verts])
        self._finish_states(u, v, w, seg, to_vert)

    def _finish_states(self, u, v, w, seg, to_vert) -> None:
        order = np.lexsort((v, u))
        self.e_u, self.e_v, self.e_w = u[order], v[order], w[order]
        self.e_seg, self.e_to = seg[order], to_vert[order]
        self._move = self.e_seg >= 0
        self.indptr = np.searchsorted(self.e_u, np.arange(self.LAYERS * self.N + 1))

    def _matrix(self, weights) -> csr_matrix:
        n = self.LAYERS * self.N
        return csr_matrix((weights, self.e_v, self.indptr), shape=(n, n))

    def all_pairs(self) -> np.ndarray:
        """Cost (length + bends) between every pair of terminals; inf if unreachable."""
        T = len(self.term_vid)
        D = np.full((T, T), np.inf)
        ok = [k for k, t in enumerate(self.term_vid) if t >= 0]
        if not ok or self.N == 0:
            return D
        tv = np.array([self.term_vid[k] for k in ok])
        dist = dijkstra(self._matrix(self.e_w), directed=True,
                        indices=np.concatenate([tv, tv + self.N]))
        n = len(ok)
        dh, dv = dist[:n], dist[n:]
        sub = np.minimum.reduce([dh[:, tv], dh[:, tv + self.N], dv[:, tv], dv[:, tv + self.N]])
        D[np.ix_(ok, ok)] = sub
        return D

    def route(self, a: int, b: int, used_seg=None, used_vert=None, blocked=None,
              w_overlap: float = 1.0, w_cross: float = 5.0):
        """Cheapest path from vertex a to vertex b. Returns (cost, [vertex ids]) or (inf, None)."""
        w = self.e_w.copy()
        mv = self._move
        if used_seg is not None:
            hit = np.zeros_like(mv)
            hit[mv] = used_seg[self.e_seg[mv]]
            w[hit] += w_overlap * self.seg_len[self.e_seg[hit]]
        if used_vert is not None:
            w[mv & used_vert[self.e_to]] += w_cross
        if blocked is not None:
            w[blocked[self.e_to]] += BLOCKED
        src = np.array([a, a + self.N])
        dist, pred = dijkstra(self._matrix(w), directed=True, indices=src, return_predecessors=True)
        options = [(dist[r, t], r, t) for r in (0, 1) for t in (b, b + self.N)]
        cost, r, t = min(options)
        if not np.isfinite(cost):
            return np.inf, None
        states = []
        s = t
        while s >= 0:
            states.append(int(s))
            s = pred[r, s]
        verts = []
        for s in reversed(states):
            vtx = s if s < self.N else s - self.N
            if not verts or verts[-1] != vtx:
                verts.append(vtx)
        return float(cost), verts

    def route_greedy(self, a: int, b: int, blocked=None) -> list[int] | None:
        """Greedy best-first search (no Dijkstra): always expand the vertex closest to b
        in straight-line (Manhattan) distance. Fast and always legal, but not shortest:
        it ignores the length walked so far and every bend."""
        import heapq
        import itertools

        if not hasattr(self, "_adj"):
            adj = [[] for _ in range(self.N)]
            for x, y in zip(self.seg_a.tolist(), self.seg_b.tolist()):
                adj[x].append(y)
                adj[y].append(x)
            self._adj = adj
        bx, by = self.vx[b], self.vy[b]
        h = lambda v: abs(self.vx[v] - bx) + abs(self.vy[v] - by)
        tie = itertools.count()
        frontier = [(h(a), next(tie), a)]
        parent = {a: -1}
        while frontier:
            _, _, v = heapq.heappop(frontier)
            if v == b:
                path = []
                while v != -1:
                    path.append(v)
                    v = parent[v]
                return path[::-1]
            for w in self._adj[v]:
                if w in parent or (blocked is not None and blocked[w] and w != b):
                    continue
                parent[w] = v
                heapq.heappush(frontier, (h(w), next(tie), w))
        return None

    def segments_of(self, verts) -> list[int]:
        return [self.seg_of[(min(a, b), max(a, b))] for a, b in zip(verts, verts[1:])]

    def polyline(self, verts) -> list[tuple[float, float]]:
        """Vertex path -> polyline with collinear points removed."""
        pts = [(float(self.vx[v]), float(self.vy[v])) for v in verts]
        if len(pts) == 1:
            return [pts[0], pts[0]]
        out = [pts[0]]
        for i in range(1, len(pts) - 1):
            a, b, c = out[-1], pts[i], pts[i + 1]
            collinear = (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1])
            onward = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) > 0
            if collinear and onward:       # straight on: drop b (but keep a reversal point)
                continue
            out.append(b)
        out.append(pts[-1])
        return out


# --------------------------------------------------------------------------- #
# Direction-aware graph (quality engine)
# --------------------------------------------------------------------------- #
PX, MX, PY, MY = 0, 1, 2, 3          # moving +x, -x, +y, -y


class DirRoutingGraph(RoutingGraph):
    """Same grid, but each vertex has 4 states: the direction the cable is moving.

    Turning 90 degrees or reversing (180 degrees) at a vertex costs one bend, exactly
    like the score. Costs between terminals are kept per (start direction, arrival
    direction), so the ring order can count the bends at nodes, where one leg meets
    the next.
    """

    LAYERS = 4

    def _build_states(self, w_bend: float) -> None:
        N, S = self.N, self.S
        sid = np.arange(S)
        h = self.seg_axis == 0
        a, b = self.seg_a, self.seg_b                   # b is the +x / +y neighbour of a
        fwd = np.where(h, PX, PY) * N                   # a -> b moves +x or +y
        bwd = np.where(h, MX, MY) * N                   # b -> a moves -x or -y
        verts = np.arange(N)
        tu, tv = [], []
        for d in range(4):
            for e in range(4):
                if d != e:
                    tu.append(verts + d * N)
                    tv.append(verts + e * N)
        u = np.concatenate([a + fwd, b + bwd, *tu])
        v = np.concatenate([b + fwd, a + bwd, *tv])
        w = np.concatenate([self.seg_len, self.seg_len, np.full(12 * N, w_bend)])
        seg = np.concatenate([sid, sid, -np.ones(12 * N, np.int64)])
        to_vert = np.concatenate([b, a, np.tile(verts, 12)])
        self._finish_states(u, v, w, seg, to_vert)

    def all_pairs_dir(self, chunk: int = 48) -> np.ndarray:
        """D[a, da, b, db]: cost leaving terminal a moving da, arriving at b moving db."""
        T, N = len(self.term_vid), self.N
        D = np.full((T, 4, T, 4), np.inf)
        ok = [k for k, t in enumerate(self.term_vid) if t >= 0]
        if not ok or N == 0:
            return D
        tv = np.array([self.term_vid[k] for k in ok])
        cols = (np.arange(4)[None, :] * N + tv[:, None]).ravel()      # (terminal, dir) order
        src = cols.copy()
        m = self._matrix(self.e_w)
        rows = np.empty((len(src), len(cols)))
        for i in range(0, len(src), chunk):
            rows[i:i + chunk] = dijkstra(m, directed=True, indices=src[i:i + chunk])[:, cols]
        n = len(ok)
        sub = rows.reshape(n, 4, n, 4)
        D[np.ix_(ok, range(4), ok, range(4))] = sub
        return D

    def all_pairs(self) -> np.ndarray:
        return self.all_pairs_dir().min(axis=(1, 3))

    def route(self, a: int, b: int, used_seg=None, used_vert=None, blocked=None,
              w_overlap: float = 1.0, w_cross: float = 5.0, start_cost=None, end_cost=None):
        """Cheapest path a -> b. start_cost[d] / end_cost[d]: extra cost for leaving a /
        arriving at b moving in direction d (the bend against the neighbouring legs).
        Returns (cost, [vertex ids], start_dir, end_dir) or (inf, None, None, None)."""
        N = self.N
        w = self.e_w.copy()
        mv = self._move
        if used_seg is not None:
            hit = np.zeros_like(mv)
            hit[mv] = used_seg[self.e_seg[mv]]
            w[hit] += w_overlap * self.seg_len[self.e_seg[hit]]
        if used_vert is not None:
            w[mv & used_vert[self.e_to]] += w_cross
        if blocked is not None:
            w[blocked[self.e_to]] += BLOCKED
        # super source S = 4N with an edge into each start direction (tiny eps: csgraph drops 0)
        sc = np.zeros(4) if start_cost is None else np.asarray(start_cost, float)
        n = 4 * N
        data = np.concatenate([w, sc + 1e-9])
        indices = np.concatenate([self.e_v, np.arange(4) * N + a])
        indptr = np.concatenate([self.indptr, [self.indptr[-1] + 4]])
        m = csr_matrix((data, indices, indptr), shape=(n + 1, n + 1))
        dist, pred = dijkstra(m, directed=True, indices=[n], return_predecessors=True)
        ec = np.zeros(4) if end_cost is None else np.asarray(end_cost, float)
        ends = [dist[0, d * N + b] + ec[d] for d in range(4)]
        d_end = int(np.argmin(ends))
        cost = ends[d_end]
        if not np.isfinite(cost):
            return np.inf, None, None, None
        states, s = [], d_end * N + b
        while s >= 0 and s != n:
            states.append(int(s))
            s = pred[0, s]
        states.reverse()
        verts = []
        for s in states:
            vtx = s % N
            if not verts or verts[-1] != vtx:
                verts.append(vtx)
        return float(cost), verts, states[0] // N, d_end
