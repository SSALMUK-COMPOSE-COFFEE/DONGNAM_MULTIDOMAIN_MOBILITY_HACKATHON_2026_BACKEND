import asyncio
import json
import os

import httpx
import pytest

from freshswitch_backend.api import vessels
from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations.ulsan_position import (
    PositionProvider,
    PositionsUnavailable,
    UlsanApiError,
    UlsanPositionClient,
    load_snapshot,
    parse_positions,
)
from freshswitch_backend.main import app

JSON_PAGE = {
    "response": {
        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
        "body": {
            "items": {
                "item": [
                    {
                        "callsgn": "010511",
                        "ptentYr": "2025",
                        "vyg": "082",
                        "vslNm": "TEST ULSAN",
                        "lat": "35.5012",
                        "lot": "129.3877",
                        "sog": "0.3",
                        "cog": "211.5",
                        "rot": "0",
                        "drft": "7.4",
                        "navStts": "정박",
                        "rcvDt": "20261030140500",
                    },
                    {"vslNm": "호출부호 없음"},
                ]
            },
            "numOfRows": 100,
            "pageNo": 1,
            "totalCount": 1,
        },
    }
}

XML_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>
<body><items><item><호출부호>D7UL1</호출부호><선박명>KOREAN TAG</선박명>
<위도>35.47</위도><경도>129.39</경도><대지속력>5.5</대지속력><운항상태>항해</운항상태></item></items>
<totalCount>1</totalCount></body></response>"""

NOT_REGISTERED = {
    "OpenAPI_ServiceResponse": {
        "cmmMsgHeader": {
            "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
            "returnAuthMsg": "등록되지 않은 서비스키",
            "returnReasonCode": "30",
        }
    }
}


def test_parse_json_aliases():
    positions, total = parse_positions(json.dumps(JSON_PAGE))
    assert total == 1
    assert len(positions) == 1
    p = positions[0]
    assert (p.call_sign, p.entry_year, p.voyage, p.vessel_name) == (
        "010511",
        "2025",
        "082",
        "TEST ULSAN",
    )
    assert (p.lat, p.lon, p.sog_knots, p.cog_deg, p.draft_m) == (35.5012, 129.3877, 0.3, 211.5, 7.4)
    assert p.nav_status == "정박"
    assert p.observed_at is not None and p.observed_at.hour == 14 and p.observed_at.minute == 5


def test_parse_single_item_dict_and_xml():
    page = json.loads(json.dumps(JSON_PAGE))
    page["response"]["body"]["items"]["item"] = JSON_PAGE["response"]["body"]["items"]["item"][0]
    positions, _ = parse_positions(json.dumps(page))
    assert positions[0].call_sign == "010511"
    positions, total = parse_positions(XML_PAGE)
    assert total == 1
    assert positions[0].vessel_name == "KOREAN TAG"
    assert positions[0].lon == 129.39


def test_parse_not_registered_error():
    with pytest.raises(UlsanApiError) as info:
        parse_positions(json.dumps(NOT_REGISTERED))
    assert info.value.code == "30"


def test_client_pages_and_params():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=JSON_PAGE)

    async def go():
        async with UlsanPositionClient("k", transport=httpx.MockTransport(handler)) as client:
            return await client.fetch_positions(call_sign="010511")

    positions = asyncio.run(go())
    assert len(positions) == 1
    assert seen[0]["resultType"] == "json"
    assert seen[0]["callsgn"] == "010511"
    assert seen[0]["pageNo"] == "1"


def test_provider_falls_back_to_snapshot_on_unregistered_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=NOT_REGISTERED)

    provider = PositionProvider(
        lambda: UlsanPositionClient("k", transport=httpx.MockTransport(handler))
    )
    feed = asyncio.run(provider.get())
    assert feed.source == "snapshot"
    assert "[30]" in feed.fallback_reason
    assert feed.count == len(load_snapshot().positions) > 0


def test_provider_offline_and_missing_snapshot(tmp_path):
    feed = asyncio.run(PositionProvider(None, offline_mode=True).get())
    assert feed.source == "snapshot" and feed.fallback_reason is None and feed.offline_mode
    with pytest.raises(PositionsUnavailable):
        asyncio.run(PositionProvider(None, snapshot_path=tmp_path / "none.json").get())


def test_ulsan_endpoint_without_key(client):
    call_sign = next(p.call_sign for p in load_snapshot().positions if p.call_sign)
    app.dependency_overrides[vessels.ulsan_client_factory] = lambda: None
    try:
        response = client.get("/api/vessels/ulsan", params={"call_sign": call_sign})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "snapshot"
    assert body["dataset_url"].endswith("15156631/openapi.do")
    assert [p["call_sign"] for p in body["positions"]] == [call_sign]


@pytest.mark.skipif(
    not (os.environ.get("DATA_GO_KR_SERVICE_KEY") or get_settings().data_go_kr_service_key),
    reason="DATA_GO_KR_SERVICE_KEY 미설정",
)
def test_live_ulsan_positions():
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY") or get_settings().data_go_kr_service_key

    async def go():
        async with UlsanPositionClient(key, timeout=30) as client:
            return await client.fetch_positions()

    try:
        positions = asyncio.run(go())
    except UlsanApiError as exc:
        if exc.code in ("20", "30"):
            pytest.skip(f"울산항만공사_항내 선박위치정보 활용신청 필요: {exc}")
        raise
    assert all(p.call_sign or p.mmsi for p in positions)
