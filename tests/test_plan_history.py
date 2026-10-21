import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as app_module
from app import (
    SolveRequest,
    list_plan_history,
    solve as solve_endpoint,
)
from cablering.plan_history import PlanHistoryStore


def test_history_survives_store_recreation_and_supports_management(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = PlanHistoryStore(path)
    common = {
        "input_data": {"plan_id": "TEST_PLAN"},
        "result": {"output": {"nodes": [], "hubs": [], "reported_metrics": {}}},
        "metadata": {"generation_settings": {}, "source": {"index": 42}},
        "metrics": {"hub_count": 2, "cost_J": 10.0},
    }
    first = store.save(
        name="First layout",
        building_ref="TEST_PLAN",
        idempotency_key="generation-first",
        **common,
    )
    second = store.save(
        name="Second layout",
        building_ref="TEST_PLAN",
        idempotency_key="generation-second",
        **common,
    )

    assert second["id"] == first["id"]
    assert set(store.list()[0]) == {
        "id", "name", "created_at", "building_ref", "metadata", "metrics",
        "last_accessed_at", "access_count",
    }
    assert store.list(query="FIRST")[0]["id"] == first["id"]
    assert [item["id"] for item in store.list(query="42")] == [first["id"]]
    assert store.record_access(first["id"])["access_count"] == 1
    assert store.list(sort="recently_accessed")[0]["id"] == first["id"]

    restarted = PlanHistoryStore(path)
    assert restarted.get(first["id"])["result"] == common["result"]
    assert restarted.rename(first["id"], "Renamed layout")["name"] == "Renamed layout"
    assert restarted.find_by_idempotency_key("generation-first")["id"] == first["id"]
    assert restarted.find_by_idempotency_key("generation-second")["id"] == first["id"]
    assert restarted.delete(second["id"])
    assert restarted.get(second["id"]) is None


def test_legacy_duplicates_show_once_at_original_position_and_keep_latest_result(tmp_path):
    path = tmp_path / "legacy-history.sqlite3"
    store = PlanHistoryStore(path)
    common = {
        "input_data": {"plan_id": "DUPLICATE_PLAN"},
        "metadata": {"source": {"index": 1}},
    }
    older = store.save(
        name="Original name",
        building_ref="DUPLICATE_PLAN",
        result={"output": {"revision": "old"}},
        metrics={"cost_J": 12},
        idempotency_key="legacy-first",
        **common,
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE saved_plans SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00", older["id"]),
        )
        connection.execute(
            """
            INSERT INTO saved_plans (
                id, name, created_at, building_ref, input_json, result_json,
                metadata_json, metrics_json, access_count, idempotency_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-duplicate",
                "Duplicate name",
                "2025-01-02T00:00:00+00:00",
                "DUPLICATE_PLAN",
                '{"plan_id":"DUPLICATE_PLAN"}',
                '{"output":{"revision":"latest"}}',
                '{"source":{"index":1}}',
                '{"cost_J":7}',
                2,
                "legacy-second",
                "2025-01-02T00:00:00+00:00",
            ),
        )

    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["id"] == "legacy-duplicate"
    assert listed[0]["name"] == "Duplicate name"
    assert listed[0]["created_at"] == "2025-01-01T00:00:00+00:00"
    assert listed[0]["metrics"]["cost_J"] == 7
    assert listed[0]["access_count"] == 2

    original_count = len(store.list())
    updated = store.save(
        name="Ignored replacement name",
        building_ref="DUPLICATE_PLAN",
        input_data={"plan_id": "DUPLICATE_PLAN", "revision": "new"},
        result={"output": {"revision": "newest"}},
        metadata={"source": {"index": 1}},
        metrics={"cost_J": 5},
        idempotency_key="generation-new",
    )
    assert len(store.list()) == original_count == 1
    assert updated["created_at"] == "2025-01-01T00:00:00+00:00"
    assert updated["name"] == "Duplicate name"
    assert store.get(updated["id"])["result"]["output"]["revision"] == "newest"


def test_recently_accessed_sort_puts_last_opened_plan_first(tmp_path):
    store = PlanHistoryStore(tmp_path / "access-order.sqlite3")
    base = {
        "input_data": {},
        "result": {"output": {}},
        "metadata": {},
        "metrics": {},
    }
    first = store.save(
        name="First",
        building_ref="PLAN_FIRST",
        idempotency_key="first-generation",
        **base,
    )
    store.save(
        name="Second",
        building_ref="PLAN_SECOND",
        idempotency_key="second-generation",
        **base,
    )
    store.record_access(first["id"])

    recently_accessed = store.list(sort="recently_accessed")
    assert recently_accessed[0]["id"] == first["id"]
    assert store.list(sort="newest")[0]["building_ref"] == "PLAN_SECOND"


def test_generated_plan_is_persisted_and_reopened_without_solving(two_room_plan, monkeypatch):
    two_room_plan.source.update({"dataset": "ResPlan", "index": 42})
    plan_key = uuid.uuid4().hex
    plans = app_module.plans
    plans[plan_key] = two_room_plan
    request = SolveRequest(
        plan_key=plan_key,
        generation_id=uuid.uuid4().hex,
        compare=False,
        time_budget_s=5,
    )
    client = TestClient(app_module.app)
    try:
        generated = solve_endpoint(request)
        assert generated["history_id"]
        assert generated["output"]["validation"]["valid"]
        assert len(list_plan_history(q="", sort="newest")["plans"]) == 1
        matching = client.get("/api/history/by-building/TEST_2ROOM")
        assert matching.status_code == 200
        assert matching.json()["id"] == generated["history_id"]
        assert client.get("/api/history/by-building/UNKNOWN").status_code == 404

        def optimizer_must_not_run(*args, **kwargs):
            raise AssertionError("Opening an existing plan must not invoke optimization.")

        monkeypatch.setattr(app_module, "solve_plan", optimizer_must_not_run)
        duplicate = solve_endpoint(request)
        assert duplicate["history_id"] == generated["history_id"]
        assert duplicate["output"] == generated["output"]
        assert len(list_plan_history(q="", sort="newest")["plans"]) == 1

        another_request = SolveRequest(
            plan_key=plan_key,
            generation_id=uuid.uuid4().hex,
            compare=False,
            time_budget_s=5,
        )
        reused = solve_endpoint(another_request)
        assert reused["history_id"] == generated["history_id"]
        assert reused["output"] == generated["output"]
        assert list_plan_history(q="", sort="newest")["plans"][0]["access_count"] == 1

        first_response = client.get(f"/api/history/{generated['history_id']}")
        assert first_response.status_code == 200
        first_open = first_response.json()
        assert first_open["result"]["output"] == generated["output"]
        assert first_open["result"]["optimized_image"] == generated["optimized_image"]
        assert first_open["metadata"]["access_count"] == 2
        assert first_open["metadata"]["last_accessed_at"]

        second_open = client.get(f"/api/history/{generated['history_id']}").json()
        assert second_open["metadata"]["access_count"] == 3
        assert second_open["result"]["output"] == generated["output"]
    finally:
        plans.pop(plan_key, None)
        app_module.plan_inputs.pop(plan_key, None)
        app_module.results.pop(plan_key, None)
        app_module.design_histories.pop(plan_key, None)
        app_module.plan_configs.pop(plan_key, None)


def test_failed_generation_is_not_saved(two_room_plan, monkeypatch):
    plan_key = uuid.uuid4().hex
    app_module.plans[plan_key] = two_room_plan

    def fail_optimization(*args, **kwargs):
        raise RuntimeError("simulated solver failure")

    monkeypatch.setattr(app_module, "solve_plan", fail_optimization)
    try:
        with pytest.raises(HTTPException, match="Optimisation failed"):
            solve_endpoint(SolveRequest(plan_key=plan_key, compare=False))
        assert list_plan_history(q="", sort="newest")["plans"] == []
    finally:
        app_module.plans.pop(plan_key, None)


def test_history_endpoints_rename_and_delete_persist(two_room_plan):
    plan_key = uuid.uuid4().hex
    app_module.plans[plan_key] = two_room_plan
    client = TestClient(app_module.app)
    try:
        generated = solve_endpoint(SolveRequest(plan_key=plan_key, compare=False, time_budget_s=5))
        renamed = client.patch(
            f"/api/history/{generated['history_id']}",
            json={"name": "  Favourite layout  "},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Favourite layout"
        assert client.get("/api/history").json()["plans"][0]["name"] == "Favourite layout"
        assert client.delete(f"/api/history/{generated['history_id']}").json() == {"deleted": True}
        assert client.get("/api/history").json()["plans"] == []
    finally:
        app_module.plans.pop(plan_key, None)
        app_module.plan_inputs.pop(plan_key, None)
        app_module.results.pop(plan_key, None)
        app_module.design_histories.pop(plan_key, None)
        app_module.plan_configs.pop(plan_key, None)
