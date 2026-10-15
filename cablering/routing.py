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

        # ---- directed state graph: state v (horizontal) / v + N (vertical) -
        off = np.where(self.seg_axis == 0, 0, N)
        sid = np.arange(S)
        verts = np.arange(N)
        u = np.concatenate([self.seg_a + off, self.seg_b + off, verts, verts + N])
        v = np.concatenate([self.seg_b + off, self.seg_a + off, verts + N, verts])
        w = np.concatenate([self.seg_len, self.seg_len, np.full(2 * N, w_bend)])
        seg = np.concatenate([sid, sid, -np.ones(2 * N, np.int64)])
        to_vert = np.concatenate([self.seg_b, self.seg_a, verts, verts])
        order = np.lexsort((v, u))
        self.e_u, self.e_v, self.e_w = u[order], v[order], w[order]
        self.e_seg, self.e_to = seg[order], to_vert[order]
        self._move = self.e_seg >= 0
        self.indptr = np.searchsorted(self.e_u, np.arange(2 * N + 1))

        # ---- terminal vertices ---------------------------------------------
        self.term_vid = []
        for x, y in self.terminals:
            i = int(np.searchsorted(self.xs, x)); j = int(np.searchsorted(self.ys, y))
            ok = i < nx_ and j < ny_ and self.xs[i] == x and self.ys[j] == y
            self.term_vid.append(int(vid[i, j]) if ok and vid[i, j] >= 0 else -1)

    # ------------------------------------------------------------------ #
    def _matrix(self, weights) -> csr_matrix:
        return csr_matrix((weights, self.e_v, self.indptr), shape=(2 * self.N, 2 * self.N))

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
            if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
                continue
            out.append(b)
        out.append(pts[-1])
        return out
