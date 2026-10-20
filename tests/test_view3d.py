from cablering.view3d import build_figure


def test_scene_frames_rendered_geometry_and_defaults_to_transparent_walls(two_room_plan):
    output = {
        "hubs": [{
            "hub_id": "H1",
            "room_id": "R01",
            "location": {"x": 2.0, "y": 2.5},
            "assigned_rooms": ["R01"],
            "assigned_area_m2": 24.5,
            "assigned_nodes": [],
            "ring_length_m": 7.0,
            "route_segments": [{"polyline": [[1.0, 1.0], [4.0, 1.0]]}],
        }],
        "nodes": [],
    }

    figure = build_figure(two_room_plan, output, animate=False)
    scene = figure.layout.scene
    walls = next(trace for trace in figure.data if trace.name == "walls")
    floors = [trace for trace in figure.data if trace.name == "floor"]

    assert walls.opacity == 0.1
    assert {trace.color for trace in floors} == {"#087fba", "#40536f"}
    assert list(scene.xaxis.range) == [-0.2, 10.2]
    assert list(scene.yaxis.range) == [-0.2, 5.2]
    assert scene.camera.center.to_plotly_json() == {"x": 0, "y": 0, "z": 0}
    assert scene.camera.eye.x > 0
    assert scene.camera.eye.y < 0
    assert scene.camera.eye.z > 0
    hub = next(trace for trace in figure.data if trace.name == "H1 hub")
    assert hub.marker.size == 12
