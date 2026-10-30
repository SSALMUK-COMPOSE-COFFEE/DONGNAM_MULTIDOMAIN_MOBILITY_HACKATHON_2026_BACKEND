from datetime import UTC, datetime, timedelta

import httpx
import pytest

from freshswitch_backend.api import forecast as forecast_api
from freshswitch_backend.api import vessels
from freshswitch_backend.config import Settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.forecast.port_congestion import (
    THRESHOLDS,
    PortCongestion,
    build_indicator,
    level_of,
)
from freshswitch_backend.forecast.predictor import DelayPredictor, DelayQuery
from freshswitch_backend.forecast.service import (
    ForecastOptions,
    congestion_use,
    forecast_voyages,
)
from freshswitch_backend.integrations.ulsan_position import (
    PositionFeed,
    PositionsUnavailable,
    UlsanPositionClient,
    VesselPosition,
    load_snapshot,
)
from freshswitch_backend.main import app
from freshswitch_backend.planning.service import build_plan

NOW = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
ANCHORAGE = (35.44, 129.45)
INNER = (35.45, 129.36)
OFFSHORE = (35.30, 129.71)

_seq = iter(range(10_000))


def ship(
    where: tuple[float, float],
    sog: float = 0.0,
    status: str | None = None,
    imo: str | None = "auto",
    draft: float | None = 8.0,
    age_hours: float = 0.0,
    mmsi: str | None = None,
) -> VesselPosition:
    n = next(_seq)
    return VesselPosition(
        vessel_name=f"SHIP {n}",
        call_sign=f"CS{n}",
        mmsi=mmsi or f"44000{n:04d}",
        imo=f"9{n:06d}" if imo == "auto" else imo,
        lat=where[0],
        lon=where[1],
        sog_knots=sog,
        draft_m=draft,
        nav_status=status,
        observed_at=NOW - timedelta(hours=age_hours),
    )


def feed(positions: list[VesselPosition], source: str = "snapshot") -> PositionFeed:
    return PositionFeed(
        source=source,
        fetched_at=NOW,
        offline_mode=False,
        count=len(positions),
        positions=positions,
    )


def fleet(waiting: int, berthed: int) -> list[VesselPosition]:
    return [ship(ANCHORAGE, status="정박(앵커링)") for _ in range(waiting)] + [
        ship(INNER, status="정박(계류)") for _ in range(berthed)
    ]


def test_classification_rules():
    positions = [
        ship(ANCHORAGE, status="정박(앵커링)"),
        ship(ANCHORAGE, status="항해(동력)"),
        ship((35.502, 129.367), status="정박(앵커링)"),
        ship(INNER, status="정박(계류)"),
        ship(INNER, status="항해(동력)"),
        ship(INNER, sog=0.8, status=None),
        ship(ANCHORAGE, sog=12.0, status="항해(동력)"),
        ship(OFFSHORE, sog=1.0, status="조종 불가"),
        ship(ANCHORAGE, imo=None),
        ship(ANCHORAGE, draft=0.0),
        ship(ANCHORAGE, status="낚시"),
        ship(ANCHORAGE, status="정박(앵커링)", age_hours=30),
    ]
    old = ship(INNER, status="정박(계류)", mmsi="440999999", age_hours=5)
    new = old.model_copy(update={"lat": ANCHORAGE[0], "lon": ANCHORAGE[1], "observed_at": NOW})
    indicator = build_indicator(feed([*positions, old, new]))
    c = indicator.counts
    assert (c.total, c.duplicates, c.stale, c.small_craft) == (14, 1, 1, 3)
    assert (c.waiting, c.berthed, c.underway, c.other) == (4, 3, 1, 1)
    assert c.considered == 9
    assert indicator.waiting_ratio == pytest.approx(4 / 7, abs=1e-3)
    assert {v.category for v in indicator.vessels if v.mmsi == "440999999"} == {"waiting"}
    assert all("가정" in a.source for a in indicator.anchorage_areas + indicator.berth_areas)


def test_level_thresholds():
    assert level_of(10, 0.2) == "normal"
    assert level_of(10, THRESHOLDS.elevated_ratio) == "elevated"
    assert level_of(THRESHOLDS.elevated_waiting, 0.1) == "elevated"
    assert level_of(10, THRESHOLDS.high_ratio) == "high"
    assert level_of(THRESHOLDS.high_waiting, 0.1) == "high"
    assert build_indicator(feed(fleet(5, 20))).level == "normal"
    assert build_indicator(feed(fleet(8, 12))).level == "elevated"
    assert build_indicator(feed(fleet(12, 8))).level == "high"
    assert build_indicator(feed([])).level == "normal"


def test_real_snapshot_indicator():
    snapshot = load_snapshot()
    indicator = build_indicator(
        PositionFeed(
            source="snapshot",
            fetched_at=snapshot.fetched_at,
            offline_mode=True,
            count=snapshot.count,
            positions=snapshot.positions,
        )
    )
    c = indicator.counts
    assert c.total == 572
    assert (c.waiting, c.berthed, c.underway) == (24, 79, 23)
    assert indicator.level == "normal"
    assert "울산항 스냅샷" in indicator.basis


def test_congestion_endpoint_snapshot(client):
    response = client.get("/api/vessels/ulsan/congestion")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "snapshot" and body["fallback_reason"]
    assert body["counts"]["waiting"] == 24 and body["level"] == "normal"
    assert {v["category"] for v in body["vessels"]} >= {"waiting", "berthed", "underway"}
    slim = client.get("/api/vessels/ulsan/congestion", params={"include_vessels": False}).json()
    assert slim["vessels"] == [] and slim["counts"] == body["counts"]


def test_congestion_endpoint_live(client, monkeypatch):
    items = [
        {
            "callsgn": p.call_sign,
            "mmsiNo": p.mmsi,
            "imoNo": p.imo,
            "vslNm": p.vessel_name,
            "lat": p.lat,
            "lot": p.lon,
            "sog": p.sog_knots,
            "drft": p.draft_m,
            "nvgtStts": p.nav_status,
            "updtTm": "20261030230000",
        }
        for p in fleet(12, 8)
    ]
    page = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"items": {"item": items}, "totalCount": len(items)},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(offline_mode=False))
    app.dependency_overrides[vessels.ulsan_client_factory] = lambda: (
        lambda: UlsanPositionClient("k", transport=httpx.MockTransport(handler))
    )
    body = client.get("/api/vessels/ulsan/congestion").json()
    assert body["source"] == "live" and body["level"] == "high"
    assert body["counts"]["waiting"] == 12


def test_predictor_elevated_factor():
    predictor = DelayPredictor([])
    query = DelayQuery()
    normal = predictor.predict(query)
    elevated = predictor.predict(query, congestion="elevated")
    assert elevated.congestion_factor == 1.25
    assert elevated.p90_hours == pytest.approx(normal.p90_hours * 1.25, abs=0.1)
    assert elevated.congestion_added_p90_hours == pytest.approx(
        elevated.p90_hours - normal.p90_hours, abs=0.1
    )
    assert normal.congestion_added_p50_hours == 0


@pytest.fixture(scope="module")
def default_plan():
    scenario = get_scenario("default")
    return scenario, build_plan(scenario).plan


def test_auto_congestion_scopes(default_plan):
    scenario, plan = default_plan
    predictor = DelayPredictor([])
    port = build_indicator(feed(fleet(12, 8))).summary()
    proxy = forecast_voyages(predictor, scenario, plan, ForecastOptions(), port)
    assert all(v.congestion_scope == "ulsan_proxy" for v in proxy)
    assert all(v.forecast.congestion == "high" for v in proxy)
    ulsan = scenario.model_copy(
        update={
            "voyages": [
                v.model_copy(update={"origin": "KRUSN", "terminal": "울산신항"})
                for v in scenario.voyages
            ]
        }
    )
    direct = forecast_voyages(predictor, ulsan, plan, ForecastOptions(), port)
    assert all(v.congestion_scope == "ulsan_direct" for v in direct)
    use = congestion_use(ForecastOptions(), direct, port)
    assert use.mode == "auto" and use.level == "high"
    assert "대리 지표" not in use.note
    assert "대리 지표" in congestion_use(ForecastOptions(), proxy, port).note
    missing = forecast_voyages(predictor, scenario, plan, ForecastOptions(), None)
    assert {v.congestion_scope for v in missing} == {"unavailable"}
    manual = forecast_voyages(
        predictor, scenario, plan, ForecastOptions(congestion="elevated"), port
    )
    assert {(v.congestion_scope, v.forecast.congestion) for v in manual} == {("manual", "elevated")}


def _new_plan(client) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def test_forecast_endpoint_auto_uses_ulsan(client, monkeypatch):
    plan_id = _new_plan(client)
    calm = client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).json()
    use = calm["port_congestion"]
    assert use["mode"] == "auto" and use["level"] == "normal"
    assert use["indicator"]["source"] == "snapshot" and "vessels" not in use["indicator"]
    assert all(e["scope"] == "ulsan_proxy" and e["p90_added_hours"] == 0 for e in use["effects"])
    assert "대리 지표" in use["note"]

    async def busy(factory) -> PortCongestion:
        return build_indicator(feed(fleet(12, 8)))

    monkeypatch.setattr(forecast_api, "load_ulsan_congestion", busy)
    high = client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).json()
    assert high["port_congestion"]["level"] == "high"
    first, base = high["voyages"][0]["forecast"], calm["voyages"][0]["forecast"]
    assert first["congestion"] == "high" and first["p90_hours"] > base["p90_hours"]
    assert high["port_congestion"]["effects"][0]["p90_added_hours"] > 0


def test_forecast_endpoint_preset_pins_congestion(client, monkeypatch):
    async def boom(factory):
        raise AssertionError("preset must not read Ulsan positions")

    monkeypatch.setattr(forecast_api, "load_ulsan_congestion", boom)
    plan_id = _new_plan(client)
    body = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"preset": "typhoon"}
    ).json()
    use = body["port_congestion"]
    assert use["mode"] == "preset" and use["level"] == "high" and use["indicator"] is None
    assert body["options"]["congestion"] == "high"
    assert body["voyages"][0]["forecast"]["p90_hours"] == 93.0
    manual = client.post(
        f"/api/plans/{plan_id}/delay-forecast?llm=false", json={"congestion": "normal"}
    ).json()
    assert manual["port_congestion"]["mode"] == "manual"


def test_forecast_endpoint_ulsan_unavailable(client, monkeypatch):
    async def down(factory):
        raise PositionsUnavailable("오프라인 모드, 저장된 울산항 스냅샷이 없습니다")

    monkeypatch.setattr(forecast_api, "load_ulsan_congestion", down)
    plan_id = _new_plan(client)
    body = client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).json()
    use = body["port_congestion"]
    assert use["level"] == "normal" and use["indicator"] is None
    assert "스냅샷" in use["unavailable_reason"]
    assert {e["scope"] for e in use["effects"]} == {"unavailable"}
