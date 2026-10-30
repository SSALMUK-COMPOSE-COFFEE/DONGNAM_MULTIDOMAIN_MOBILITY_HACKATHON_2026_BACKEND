from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel

from freshswitch_backend.integrations.mof_vessel import BUSAN_PORT_CODE, Direction, VesselMovement

SNAPSHOT_ROOT = Path(__file__).resolve().parent.parent / "data" / "snapshots"
MOF_SNAPSHOT_DIR = SNAPSHOT_ROOT / "mof"
DEMO_FIXTURE_PATH = SNAPSHOT_ROOT / "demo" / "movements.json"
DIRECTIONS: tuple[Direction, ...] = ("I", "O")


class SnapshotMissing(Exception):
    pass


class MovementSnapshot(BaseModel):
    port_code: str = BUSAN_PORT_CODE
    date: date
    direction: Direction
    fetched_at: datetime
    count: int
    movements: list[VesselMovement]


class SnapshotDate(BaseModel):
    date: date
    directions: list[Direction]
    fetched_at: datetime


class DemoFixture(BaseModel):
    derived_from: str
    description: str
    movements: list[VesselMovement]


def _distance(day: date, start: date, end: date) -> int:
    if day < start:
        return (start - day).days
    if day > end:
        return (day - end).days
    return 0


class SnapshotStore:
    def __init__(self, root: Path = MOF_SNAPSHOT_DIR) -> None:
        self.root = root

    def path(self, day: date, direction: Direction) -> Path:
        return self.root / f"{day:%Y%m%d}-{direction}.json"

    def save(self, snapshot: MovementSnapshot) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(snapshot.date, snapshot.direction)
        path.write_text(
            snapshot.model_dump_json(indent=1, exclude_none=True) + "\n", encoding="utf-8"
        )
        return path

    def load(self, day: date, direction: Direction) -> MovementSnapshot | None:
        path = self.path(day, direction)
        if not path.is_file():
            return None
        return MovementSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def index(self) -> dict[date, list[Direction]]:
        found: dict[date, list[Direction]] = {}
        if not self.root.is_dir():
            return found
        for path in sorted(self.root.glob("*-?.json")):
            stem, _, direction = path.stem.partition("-")
            if direction not in DIRECTIONS:
                continue
            try:
                day = datetime.strptime(stem, "%Y%m%d").date()
            except ValueError:
                continue
            found.setdefault(day, []).append(direction)
        return dict(sorted(found.items()))

    def dates(self) -> list[SnapshotDate]:
        result: list[SnapshotDate] = []
        for day, directions in self.index().items():
            snapshots = [s for d in directions if (s := self.load(day, d)) is not None]
            if snapshots:
                result.append(
                    SnapshotDate(
                        date=day,
                        directions=[s.direction for s in snapshots],
                        fetched_at=max(s.fetched_at for s in snapshots),
                    )
                )
        return result

    def select(self, start: date, end: date, direction: Direction | None) -> list[MovementSnapshot]:
        wanted = [direction] if direction else list(DIRECTIONS)
        days = [day for day, dirs in self.index().items() if any(d in dirs for d in wanted)]
        if not days:
            raise SnapshotMissing("저장된 해수부 스냅샷이 없습니다")
        chosen = [d for d in days if start <= d <= end] or [
            min(days, key=lambda d: (_distance(d, start, end), -d.toordinal()))
        ]
        return [s for day in chosen for d in wanted if (s := self.load(day, d)) is not None]


def _in_range(value: datetime | None, start: date, end: date) -> bool:
    return value is not None and start <= value.date() <= end


def load_demo_fixture(path: Path = DEMO_FIXTURE_PATH) -> DemoFixture | None:
    if not path.is_file():
        return None
    return DemoFixture.model_validate_json(path.read_text(encoding="utf-8"))


def demo_movements(
    start: date, end: date, direction: Direction | None, path: Path = DEMO_FIXTURE_PATH
) -> list[VesselMovement]:
    fixture = load_demo_fixture(path)
    if fixture is None:
        return []
    picked: list[VesselMovement] = []
    for movement in fixture.movements:
        departs = _in_range(movement.observed_departure_at, start, end)
        arrives = _in_range(movement.arrival_at, start, end)
        wanted = {"O": departs, "I": arrives, None: departs or arrives}[direction]
        if wanted:
            picked.append(movement)
    return picked
