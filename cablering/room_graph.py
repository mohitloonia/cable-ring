"""Legal room connections: doors and open passages only.

Node  = a room (every type; cable may pass through bathrooms etc.).
Edge  = rooms linked by a door or by an open passage (shared boundary with no
        wall, at least `min_open_passage_m` wide). Windows, wall-only adjacency
        and the entrance never create an edge.
Each edge holds `links`: [{"kind": "door"|"open", "geom": polygon, "portal": point}].
"""
from __future__ import annotations

import networkx as nx
from shapely.ops import unary_union

from .geometry import clean, grow, interior_point, largest


def build_room_graph(plan, cfg) -> nx.Graph:
    G = nx.Graph()
    for r in plan.rooms:
        G.add_node(r.id, type=r.type, eligible=r.eligible, area=r.area,
                   center=interior_point(r.poly))
    _door_links(plan, G)
    _open_links(plan, G, cfg.min_open_passage_m)
    return G


def _link(G, a: str, b: str, kind: str, geom, portal) -> None:
    if not G.has_edge(a, b):
        G.add_edge(a, b, kind=kind, links=[])
    G.edges[a, b]["links"].append({"kind": kind, "geom": geom, "portal": portal})
    if kind == "door":
        G.edges[a, b]["kind"] = "door"


def _door_links(plan, G) -> None:
    reach = plan.wall_thickness * 0.75 + 0.05     # a door spans the wall: reach both sides
    for door in plan.doors:
        zone = door.buffer(reach)
        hits = sorted(((zone.intersection(r.poly).area, r) for r in plan.rooms
                       if zone.intersects(r.poly)), key=lambda t: -t[0])
        hits = [r for a, r in hits if a > 0.01]
        if len(hits) >= 2:                         # 1 hit = exterior door (balcony / outside)
            _link(G, hits[0].id, hits[1].id, "door", door, door.centroid)


def _open_links(plan, G, min_width: float) -> None:
    eps = 0.05
    blockers = grow(unary_union([plan.walls, plan.windows]), eps)
    rooms = plan.rooms
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            if a.poly.distance(b.poly) > eps:
                continue
            shared = clean(grow(a.poly, eps).intersection(grow(b.poly, eps)).difference(blockers))
            if shared.is_empty or shared.area / (2 * eps) < min_width:
                continue
            _link(G, a.id, b.id, "open", shared, largest(shared).centroid)


def zone_connected(G: nx.Graph, room_ids, via_other_rooms: bool) -> bool:
    """Are these rooms one connected zone through doors / open passages?"""
    room_ids = list(room_ids)
    if len(room_ids) <= 1:
        return True
    if via_other_rooms:
        keep = set(room_ids) | {n for n, d in G.nodes(data=True) if not d["eligible"]}
        H = G.subgraph(keep)
        comp = nx.node_connected_component(H, room_ids[0])
        return all(r in comp for r in room_ids)
    return nx.is_connected(G.subgraph(room_ids))


def portals(G: nx.Graph, room_id: str) -> list:
    return [ln["portal"] for nb in G[room_id] for ln in G.edges[room_id, nb]["links"]]
