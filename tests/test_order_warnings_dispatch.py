from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.config import get_settings
from freshswitch_backend.execution import orders as orders_module
from freshswitch_backend.execution.models import ExecutionOrder
from freshswitch_backend.main import create_app
from freshswitch_backend.planning import store as store_module

REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


def _captured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, ExecutionOrder]:
    calls: list[tuple[dict, ExecutionOrder]] = []

    def spy(**kwargs: object) -> ExecutionOrder:
        order = orders_module.build_order(**kwargs)
        calls.append((dict(kwargs), order))
        return order

    monkeypatch.setattr(store_module, "build_order", spy)
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert response.status_code == 200, response.text
    assert all("warnings" not in a for a in response.json()["alternatives"])
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200, applied.text
    return calls[-1]


def _dispatches(order: ExecutionOrder, truck_id: str) -> list:
    return [a for a in order.automatic if a.kind == "truck_dispatch" and a.truck_id == truck_id]


def test_first_log_note_carries_alternative_warnings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    assert kwargs["alternative"].warnings == []
    assert order.log[0].action == "대안 pull_forward 적용 → 실행 지시 생성"
    assert order.log[0].note is None
    warned = kwargs["alternative"].model_copy(
        update={"warnings": ["DISPATCH_RELEASED: RT-01", "고정 해제"]}
    )
    rebuilt = orders_module.build_order(**{**kwargs, "alternative": warned})
    assert rebuilt.log[0].note == "DISPATCH_RELEASED: RT-01; 고정 해제"
    assert all(entry.note is None for entry in rebuilt.log[1:])


def _split(kwargs: dict, order: ExecutionOrder, shift: timedelta) -> tuple[ExecutionOrder, str]:
    alternative = kwargs["alternative"]
    voyage_pickup, route = next(
        (vp, r)
        for vp in alternative.routes
        for r in vp.pickup.routes
        if _dispatches(order, r.truck_id)
    )
    other = next(v.id for v in kwargs["scenario_after"].voyages if v.id != voyage_pickup.voyage_id)
    stop = route.stops[0].model_copy(
        update={
            "stop_id": "stop-split",
            "lot_ids": ["lot-split"],
            "pallets": 2,
            "sequence": route.stops[-1].sequence + 1,
        }
    )
    part = route.model_copy(
        update={
            "stops": [stop],
            "load_pallets": 2,
            "earliest_departure": route.earliest_departure + shift,
        }
    )
    extra = voyage_pickup.model_copy(
        update={
            "voyage_id": other,
            "lot_ids": ["lot-split"],
            "pickup": voyage_pickup.pickup.model_copy(update={"routes": [part]}),
        }
    )
    split = alternative.model_copy(update={"routes": [*alternative.routes, extra]})
    rebuilt = orders_module.build_order(**{**kwargs, "alternative": split})
    return rebuilt, f"{route.truck_id}|{voyage_pickup.voyage_id}|{other}|{route.load_pallets}"


def test_same_trip_across_voyages_is_one_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    rebuilt, marker = _split(kwargs, order, timedelta(0))
    truck_id, first, second, pallets = marker.split("|")
    lines = _dispatches(rebuilt, truck_id)
    original = _dispatches(order, truck_id)
    assert len(original) == 1
    assert len(lines) == 1
    assert lines[0].description.startswith(f"{truck_id} {first}·{second}: ")
    assert f"{int(pallets) + 2}파렛트" in lines[0].description
    assert lines[0].lot_ids == [*original[0].lot_ids, "lot-split"]


def test_different_trips_stay_separate_dispatches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, order = _captured(client, monkeypatch)
    rebuilt, marker = _split(kwargs, order, timedelta(hours=3))
    truck_id, first, second, _ = marker.split("|")
    lines = _dispatches(rebuilt, truck_id)
    original = _dispatches(order, truck_id)
    assert len(lines) == 2
    assert lines[0].description == original[0].description
    assert lines[0].lot_ids == original[0].lot_ids
    assert lines[1].description.startswith(f"{truck_id} {second}: ")
    assert lines[1].lot_ids == ["lot-split"]
    assert first != second


def test_openapi_lists_optional_warnings_and_seed_counts() -> None:
    settings = get_settings().model_copy(
        update={"api_prefix": "/test/api", "session_cookie_path": "/test", "saas_mode": True}
    )
    schemas = create_app(settings).openapi()["components"]["schemas"]
    alternative = schemas["Alternative"]
    assert alternative["properties"]["warnings"]["items"] == {"type": "string"}
    assert "warnings" not in alternative["required"]
    drift = schemas["OrgDrift"]["properties"]
    assert drift["counts"]["properties"]["missing_seed_lots"] == {"type": "integer"}
    assert "missing_seed_lots" in drift["counts"]["required"]
    assert drift["baseline"]["properties"]["missing_seed_lots"] == {"type": "integer"}
    assert "missing_seed_lots" in drift["drift"]["properties"]
    assert "required" not in drift["drift"]
