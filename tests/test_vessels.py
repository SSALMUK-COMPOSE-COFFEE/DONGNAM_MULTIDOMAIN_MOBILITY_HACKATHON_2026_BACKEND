import asyncio
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.api import vessels
from freshswitch_backend.config import Settings
from freshswitch_backend.integrations import (
    MofVesselApiError,
    MofVesselClient,
    MovementProvider,
    MovementSnapshot,
    MovementsUnavailable,
    SnapshotStore,
    VesselMovement,
    demo_movements,
)
from freshswitch_backend.main import app

FETCHED = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def _movement(name: str, call_sign: str) -> VesselMovement:
    return VesselMovement(port_code="020", call_sign=call_sign, vessel_name=name)


def _store(tmp_path, days=(date(2026, 10, 27), date(2026, 10, 30))) -> SnapshotStore:
    store = SnapshotStore(tmp_path)
    for day in days:
        for direction in ("I", "O"):
            store.save(
                MovementSnapshot(
                    date=day,
                    direction=direction,
                    fetched_at=FETCHED,
                    count=1,
                    movements=[_movement(f"SNAP {day:%m%d}{direction}", f"S{day:%d}{direction}")],
                )
            )
    return store


class Hits:
    def __init__(self) -> None:
        self.count = 0
        self.factory_calls = 0


def _client_factory(handler, hits: Hits):
    def factory() -> MofVesselClient:
        hits.factory_calls += 1
        return MofVesselClient("test-key", transport=httpx.MockTransport(handler))

    return factory


def _failing(hits: Hits):
    def handler(request: httpx.Request) -> httpx.Response:
        hits.count += 1
        raise AssertionError(f"network must not be called: {request.url.path}")

    return handler


class SlowClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def fetch_movements(self, start, end, direction=None, **_):
        await asyncio.sleep(5)
        return []


class LiveClient:
    def __init__(self, movements=None, error: Exception | None = None) -> None:
        self.movements = movements or []
        self.error = error
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def fetch_movements(self, start, end, direction=None, **_):
        self.calls.append((start, end, direction))
        if self.error:
            raise self.error
        return self.movements


def _get(provider: MovementProvider, start: date, end: date, direction="O", demo=False):
    return asyncio.run(provider.get(start, end, direction, demo=demo))


def test_live_success(tmp_path) -> None:
    live = LiveClient([_movement("REAL", "R1")])
    feed = _get(
        MovementProvider(lambda: live, store=_store(tmp_path)),
        date(2026, 10, 30),
        date(2026, 10, 30),
    )
    assert feed.source == "live"
    assert feed.fallback_reason is None
    assert feed.snapshot_dates == []
    assert [m.call_sign for m in feed.movements] == ["R1"]
    assert live.calls == [(date(2026, 10, 30), date(2026, 10, 30), "O")]


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500, content=b"boom"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda request: httpx.Response(
            200,
            content=b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30"
            b"</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>",
        ),
    ],
    ids=["http-500", "connect", "api-error"],
)
def test_live_error_falls_back_to_snapshot(tmp_path, handler) -> None:
    hits = Hits()
    provider = MovementProvider(_client_factory(handler, hits), store=_store(tmp_path))
    feed = _get(provider, date(2026, 10, 30), date(2026, 10, 30))
    assert hits.factory_calls == 1
    assert feed.source == "snapshot"
    assert feed.fallback_reason
    assert feed.fetched_at == FETCHED
    assert feed.snapshot_dates == [date(2026, 10, 30)]
    assert [m.call_sign for m in feed.movements] == ["S30O"]


def test_timeout_falls_back(tmp_path) -> None:
    provider = MovementProvider(lambda: SlowClient(), store=_store(tmp_path), live_timeout=0.05)
    feed = _get(provider, date(2026, 10, 30), date(2026, 10, 30))
    assert feed.source == "snapshot"
    assert "시간 초과" in feed.fallback_reason


def test_missing_key_falls_back(tmp_path) -> None:
    feed = _get(
        MovementProvider(None, store=_store(tmp_path)), date(2026, 10, 27), date(2026, 10, 27)
    )
    assert feed.source == "snapshot"
    assert "키" in feed.fallback_reason
    assert feed.snapshot_dates == [date(2026, 10, 27)]


def test_offline_never_calls_network(tmp_path) -> None:
    hits = Hits()
    provider = MovementProvider(
        _client_factory(_failing(hits), hits), offline_mode=True, store=_store(tmp_path)
    )
    feed = _get(provider, date(2026, 11, 17), date(2026, 11, 21))
    assert hits.count == 0
    assert hits.factory_calls == 0
    assert feed.source == "snapshot"
    assert feed.offline_mode is True
    assert feed.demo is True
    assert feed.fallback_reason is None
    assert feed.snapshot_dates == [date(2026, 10, 30)]
    names = [m.vessel_name for m in feed.movements]
    assert names[0] == "SNAP 1030O"
    assert "MV FRESH HARMONY" in names


def test_nearest_snapshot_selection(tmp_path) -> None:
    store = _store(tmp_path)
    assert [s.date for s in store.select(date(2026, 10, 25), date(2026, 10, 25), "I")] == [
        date(2026, 10, 27)
    ]
    assert [s.date for s in store.select(date(2026, 10, 27), date(2026, 10, 30), None)] == [
        date(2026, 10, 27),
        date(2026, 10, 27),
        date(2026, 10, 30),
        date(2026, 10, 30),
    ]
    tie = store.select(date(2026, 10, 28), date(2026, 10, 29), "O")
    assert [s.date for s in tie] == [date(2026, 10, 30)]


def test_no_snapshot_and_no_live_is_unavailable(tmp_path) -> None:
    provider = MovementProvider(None, store=SnapshotStore(tmp_path / "empty"))
    with pytest.raises(MovementsUnavailable):
        _get(provider, date(2026, 10, 30), date(2026, 10, 30))


def test_demo_movements_filter() -> None:
    departures = demo_movements(date(2026, 11, 17), date(2026, 11, 21), "O")
    assert [m.vessel_name for m in departures] == ["MV FRESH HARMONY", "MV COLD BRIDGE"]
    assert demo_movements(date(2026, 11, 20), date(2026, 11, 21), "O")[0].call_sign == "D5CB2"
    assert demo_movements(date(2026, 10, 30), date(2026, 10, 30), None) == []


def test_bundled_snapshots_are_clean() -> None:
    store = SnapshotStore()
    dates = store.dates()
    assert len(dates) >= 3
    assert all(d.directions == ["I", "O"] for d in dates)
    for path in store.root.glob("*.json"):
        assert "servicekey" not in path.read_text(encoding="utf-8").lower()


@pytest.fixture
def fake_vessels():
    def use(client) -> None:
        app.dependency_overrides[vessels.vessel_client_factory] = lambda: lambda: client

    yield use
    app.dependency_overrides.pop(vessels.vessel_client_factory, None)


def test_busan_live_response_shape(client: TestClient, fake_vessels) -> None:
    live = LiveClient([_movement("MV FRESH HARMONY", "ABC")])
    fake_vessels(live)
    response = client.get("/api/vessels/busan", params={"date": "20261118", "direction": "O"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "live"
    assert body["fetched_at"]
    assert body["movement_count"] == 1
    assert body["movements"][0]["call_sign"] == "ABC"
    assert live.calls == [(date(2026, 11, 18), date(2026, 11, 18), "O")]


@pytest.mark.parametrize(
    "error", [MofVesselApiError("30"), httpx.ConnectError("down")], ids=["api", "network"]
)
def test_busan_errors_fall_back_to_snapshot(client: TestClient, fake_vessels, error) -> None:
    fake_vessels(LiveClient(error=error))
    response = client.get("/api/vessels/busan", params={"date": "20261118", "direction": "O"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "snapshot"
    assert body["fallback_reason"]
    assert body["snapshot_dates"] == [SnapshotStore().dates()[-1].date.isoformat()]
    assert body["movement_count"] > 0


def test_busan_without_key_uses_snapshot(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(data_go_kr_service_key=""))
    response = client.get("/api/vessels/busan", params={"date": "20261030"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "snapshot"
    assert "키" in body["fallback_reason"]
    assert body["snapshot_dates"] == ["2026-10-30"]


def test_busan_unavailable_is_503(client: TestClient, fake_vessels, monkeypatch, tmp_path) -> None:
    fake_vessels(LiveClient(error=httpx.ConnectError("down")))
    monkeypatch.setattr(
        vessels,
        "movement_provider",
        lambda factory: MovementProvider(factory, store=SnapshotStore(tmp_path)),
    )
    response = client.get("/api/vessels/busan", params={"date": "20261030"})
    assert response.status_code == 503
    assert "해수부" in response.json()["detail"]


def test_busan_demo_flag_adds_fixture(client: TestClient, fake_vessels) -> None:
    fake_vessels(LiveClient([_movement("REAL", "R1")]))
    response = client.get(
        "/api/vessels/busan", params={"date": "20261119", "direction": "O", "demo": "true"}
    )
    body = response.json()
    assert body["source"] == "live"
    assert body["demo"] is True
    assert [m["call_sign"] for m in body["movements"]] == ["R1", "D5FH1"]


def test_offline_schedule_check_detects_demo_delay(client: TestClient, monkeypatch) -> None:
    hits = Hits()
    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(offline_mode=True))
    app.dependency_overrides[vessels.vessel_client_factory] = lambda: _client_factory(
        _failing(hits), hits
    )
    try:
        plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
        response = client.post(f"/api/plans/{plan_id}/schedule-check")
    finally:
        app.dependency_overrides.pop(vessels.vessel_client_factory, None)
    assert response.status_code == 200
    assert hits.count == 0 and hits.factory_calls == 0
    body = response.json()
    assert [(c["voyage_id"], c["delta_hours"]) for c in body["changes"]] == [("V-2611A", 12.0)]
    assert body["events"][0]["new_etd"] == "2026-11-19T06:00:00+09:00"
