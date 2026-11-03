from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from freshswitch_backend.domain.models import Voyage
from freshswitch_backend.forecast.history import (
    MAX_DELAY_HOURS,
    MIN_DELAY_HOURS,
    DepartureRecord,
    terminal_of,
    vessel_key,
)
from freshswitch_backend.integrations.mof_vessel import (
    MAX_GAP_HOURS,
    DepartureMatch,
    PlannedVoyage,
    VesselMovement,
    match_departures,
)
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import DelayObservationRow
from freshswitch_backend.quality import learning_scope

OWN_SOURCE = "자체 관측: schedule-check로 매칭된 실제 출항(계획 ETD 대비 해수부 출항일시)"


def planned_voyages(voyages: Iterable[Voyage]) -> list[PlannedVoyage]:
    return [
        PlannedVoyage(voyage_id=v.id, vessel_name=v.vessel_name, etd=v.etd, call_sign=v.call_sign)
        for v in voyages
    ]


def _row(match: DepartureMatch) -> dict[str, object] | None:
    movement = match.movement
    if movement.departure_at is None:
        return None
    delay = (movement.departure_at - match.planned_etd).total_seconds() / 3600
    if not MIN_DELAY_HOURS <= delay <= MAX_DELAY_HOURS:
        return None
    call_sign = movement.call_sign or match.voyage.call_sign or None
    key = vessel_key(match.voyage.vessel_name, call_sign)
    if key is None:
        return None
    return {
        "voyage_id": match.voyage.voyage_id,
        "vessel_key": key,
        "vessel_name": match.voyage.vessel_name,
        "call_sign": call_sign,
        "matched_by": match.matched_by,
        "terminal": terminal_of(movement.berth),
        "vessel_kind": movement.vessel_kind,
        "planned_departure_at": match.planned_etd,
        "departure_at": movement.departure_at,
        "delay_hours": round(delay, 2),
    }


def record_matches(
    org_id: str,
    matches: Iterable[DepartureMatch],
    plan_id: str | None = None,
    target: Database | None = None,
) -> int:
    rows = [r for m in matches if (r := _row(m)) is not None]
    if not rows:
        return 0
    scope = learning_scope(org_id)
    values = [{**r, "org_id": org_id, "scope": scope, "plan_id": plan_id} for r in rows]
    statement = insert(DelayObservationRow).values(values)
    statement = statement.on_conflict_do_update(
        constraint="uq_delay_observations_org_vessel_planned",
        set_={
            name: statement.excluded[name]
            for name in (
                "plan_id",
                "voyage_id",
                "call_sign",
                "matched_by",
                "terminal",
                "vessel_kind",
                "departure_at",
                "delay_hours",
            )
        }
        | {"recorded_at": func.now()},
    )
    with (target or database).begin() as db:
        db.execute(statement)
    return len(rows)


def record_schedule_check(
    org_id: str,
    voyages: Iterable[Voyage],
    movements: Sequence[VesselMovement],
    plan_id: str | None = None,
    max_gap_hours: float = MAX_GAP_HOURS,
    target: Database | None = None,
) -> int:
    matches = match_departures(planned_voyages(voyages), movements, max_gap_hours)
    return record_matches(org_id, matches, plan_id, target)


def own_records(org_id: str, target: Database | None = None) -> list[DepartureRecord]:
    scope = learning_scope(org_id)
    with (target or database).begin() as db:
        rows = db.scalars(
            select(DelayObservationRow)
            .where(DelayObservationRow.scope == scope)
            .order_by(DelayObservationRow.id)
        ).all()
    latest = {(r.vessel_key, r.planned_departure_at): r for r in rows}
    return [
        DepartureRecord(
            call_sign=r.call_sign or r.vessel_key,
            vessel_name=r.vessel_name,
            vessel_kind=r.vessel_kind,
            terminal=r.terminal,
            planned_departure_at=r.planned_departure_at,
            departure_at=r.departure_at,
            delay_hours=r.delay_hours,
        )
        for r in latest.values()
    ]
