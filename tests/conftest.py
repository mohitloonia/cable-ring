import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cablering.config import DATA_PATH, Config  # noqa: E402
from cablering.model import Plan, Room  # noqa: E402


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def two_room_plan():
    """10 x 5 m flat: living (left) | bedroom (right), one 1 m door in the middle wall.

    Coordinates in metres, walls 0.2 m thick.
    """
    inner = box(0, 0, 10, 5)
    outer = box(-0.2, -0.2, 10.2, 5.2).difference(inner)
    partition = box(4.9, 0, 5.1, 5).difference(box(4.9, 2, 5.1, 3))
    walls = unary_union([outer, partition])
    door = box(4.9, 2, 5.1, 3)
    rooms = [Room("R01", "living_0", "living", box(0, 0, 4.9, 5), True),
             Room("R02", "bedroom_0", "bedroom", box(5.1, 0, 10, 5), True)]
    return Plan("TEST_2ROOM", 1.0, inner, walls, Polygon(), [door], Polygon(), Polygon(),
                rooms, 0.2)


@pytest.fixture(scope="session")
def dataset():
    if not DATA_PATH.exists():
        pytest.skip("data/ResPlan.pkl not present")
    from cablering.input_loader import load_dataset
    return load_dataset()
