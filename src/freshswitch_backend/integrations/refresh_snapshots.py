import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta

from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations.mof_snapshot import (
    DIRECTIONS,
    MovementSnapshot,
    SnapshotStore,
)
from freshswitch_backend.integrations.mof_vessel import KST, MofVesselClient


def _day(text: str) -> date:
    return datetime.strptime(text, "%Y%m%d").date()


async def capture(start: date, end: date, store: SnapshotStore, client: MofVesselClient) -> None:
    async with client:
        day = start
        while day <= end:
            for direction in DIRECTIONS:
                movements = await client.fetch_movements(day, day, direction=direction)
                path = store.save(
                    MovementSnapshot(
                        date=day,
                        direction=direction,
                        fetched_at=datetime.now(UTC),
                        count=len(movements),
                        movements=movements,
                    )
                )
                print(f"{day:%Y-%m-%d} {direction} {len(movements):4d}척 -> {path.name}")
            day += timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(KST).date()
    parser = argparse.ArgumentParser(description="해수부 부산항 입출항 스냅샷 저장")
    parser.add_argument("--start", type=_day, default=today - timedelta(days=3))
    parser.add_argument("--end", type=_day, default=today)
    args = parser.parse_args(argv)
    key = get_settings().data_go_kr_service_key
    if not key:
        print("DATA_GO_KR_SERVICE_KEY가 비어 있습니다", file=sys.stderr)
        return 1
    asyncio.run(capture(args.start, args.end, SnapshotStore(), MofVesselClient(key, timeout=30)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
