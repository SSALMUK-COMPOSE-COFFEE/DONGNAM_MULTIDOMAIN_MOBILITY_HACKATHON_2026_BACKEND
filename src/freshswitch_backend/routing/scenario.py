from datetime import datetime, time

from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.routing.models import Depot, PickupRequest, PickupStop, PickupTruck

DEFAULT_READY_TIME = time(8, 0)


def build_pickup_request(
    scenario: Scenario,
    lot_ids: list[str],
    voyage_id: str,
    ready_time: time = DEFAULT_READY_TIME,
    **options,
) -> PickupRequest:
    voyages = {v.id: v for v in scenario.voyages}
    if voyage_id not in voyages:
        raise KeyError(f"unknown voyage: {voyage_id}")
    voyage = voyages[voyage_id]
    ports = {p.id: p for p in scenario.ports}
    port = ports[voyage.origin]
    lots = {lot.id: lot for lot in scenario.lots}
    shippers = {s.id: s for s in scenario.shippers}
    packhouses = {p.id: p for p in scenario.packhouses}
    tz = voyage.cy_cutoff.tzinfo

    grouped: dict[str, list] = {}
    for lot_id in lot_ids:
        if lot_id not in lots:
            raise KeyError(f"unknown lot: {lot_id}")
        lot = lots[lot_id]
        grouped.setdefault(shippers[lot.shipper_id].packhouse_id, []).append(lot)

    stops = []
    for packhouse_id, group in grouped.items():
        ph = packhouses[packhouse_id]
        ready = max(datetime.combine(lot.ready_date, ready_time, tzinfo=tz) for lot in group)
        stops.append(
            PickupStop(
                id=f"stop-{packhouse_id}",
                packhouse_id=packhouse_id,
                lat=ph.lat,
                lon=ph.lon,
                pallets=sum(lot.pallets for lot in group),
                ready_at=ready,
                lot_ids=[lot.id for lot in group],
            )
        )

    trucks = [
        PickupTruck(id=t.id, capacity_pallets=t.capacity_pallets)
        for t in scenario.trucks
        if t.depot_id == port.id
    ]
    return PickupRequest(
        depot=Depot(id=port.id, lat=port.lat, lon=port.lon),
        stops=stops,
        trucks=trucks,
        deadline=voyage.cy_cutoff,
        **options,
    )
