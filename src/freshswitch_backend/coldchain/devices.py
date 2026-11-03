import hashlib
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from freshswitch_backend.coldchain.models import Binding
from freshswitch_backend.domain.ids import check_id
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import DEMO_ORG, BindingRow, DeviceRow

SIMULATOR_PREFIX = "sim-"
SANDBOX_DEVICE_PREFIX = "demo-"


class DeviceConflict(ValueError):
    pass


def is_simulator_device(device_id: str) -> bool:
    return device_id.startswith(SIMULATOR_PREFIX)


def device_prefix_error(device_id: str, sandbox: bool) -> str | None:
    lowered = device_id.lower()
    if sandbox and not lowered.startswith(SANDBOX_DEVICE_PREFIX):
        return f"데모 조직 기기 id는 '{SANDBOX_DEVICE_PREFIX}'로 시작해야 합니다"
    if not sandbox and lowered.startswith(SANDBOX_DEVICE_PREFIX):
        return f"'{SANDBOX_DEVICE_PREFIX}'로 시작하는 id는 데모 조직용이라 등록할 수 없습니다"
    if not sandbox and lowered.startswith(SIMULATOR_PREFIX):
        return f"'{SIMULATOR_PREFIX}'로 시작하는 id는 시뮬레이터용이라 등록할 수 없습니다"
    return None


class DeviceRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=200)

    @field_validator("device_id")
    @classmethod
    def _device_id(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("빈 값은 사용할 수 없습니다")
        if is_simulator_device(trimmed):
            raise ValueError(
                f"'{SIMULATOR_PREFIX}'로 시작하는 id는 시뮬레이터용이라 등록할 수 없습니다"
            )
        return check_id(trimmed)


class Device(BaseModel):
    device_id: str
    name: str | None = None
    created_at: datetime
    last_seen_at: datetime | None = None
    binding: Binding | None = None


class RegisteredDevice(Device):
    device_token: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class DeviceStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db or database
        self.clock: Callable[[], datetime] = _now

    def _device(self, row: DeviceRow, binding: BindingRow | None) -> Device:
        return Device(
            device_id=row.device_id,
            name=row.name,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            binding=Binding.model_validate(binding.payload) if binding is not None else None,
        )

    def register(self, body: DeviceRequest, org_id: str) -> RegisteredDevice:
        token = secrets.token_urlsafe(32)
        try:
            with self._db.begin() as db:
                if db.get(DeviceRow, body.device_id) is not None:
                    raise DeviceConflict(f"이미 등록된 장치입니다: {body.device_id}")
                binding = db.get(BindingRow, body.device_id)
                if binding is not None and binding.org_id not in (DEMO_ORG, org_id):
                    raise DeviceConflict(f"다른 조직에 연결된 장치입니다: {body.device_id}")
                if binding is not None and binding.org_id == DEMO_ORG:
                    db.execute(delete(BindingRow).where(BindingRow.device_id == body.device_id))
                row = DeviceRow(
                    device_id=body.device_id,
                    org_id=org_id,
                    token_hash=token_hash(token),
                    name=body.name,
                    created_at=self.clock(),
                )
                db.add(row)
                db.flush()
                device = self._device(row, db.get(BindingRow, body.device_id))
        except IntegrityError as exc:
            raise DeviceConflict(f"이미 등록된 장치입니다: {body.device_id}") from exc
        return RegisteredDevice(**device.model_dump(), device_token=token)

    def for_org(self, org_id: str) -> list[Device]:
        with self._db.begin() as db:
            rows = db.execute(
                select(DeviceRow, BindingRow)
                .outerjoin(
                    BindingRow,
                    (BindingRow.device_id == DeviceRow.device_id)
                    & (BindingRow.org_id == DeviceRow.org_id),
                )
                .where(DeviceRow.org_id == org_id)
                .order_by(DeviceRow.device_id)
            ).all()
            return [self._device(row, binding) for row, binding in rows]

    def delete(self, device_id: str, org_id: str) -> bool:
        with self._db.begin() as db:
            row = db.get(DeviceRow, device_id)
            if row is None or row.org_id != org_id:
                return False
            db.execute(
                delete(BindingRow).where(
                    BindingRow.device_id == device_id, BindingRow.org_id == org_id
                )
            )
            db.delete(row)
            return True

    def owners(self, device_ids: Iterable[str]) -> dict[str, str]:
        ids = set(device_ids)
        if not ids:
            return {}
        with self._db.begin() as db:
            rows = db.execute(
                select(DeviceRow.device_id, DeviceRow.org_id).where(DeviceRow.device_id.in_(ids))
            ).all()
            return dict(rows)

    def verify(self, device_id: str, token: str | None) -> bool:
        if not token:
            return False
        with self._db.begin() as db:
            stored = db.scalar(select(DeviceRow.token_hash).where(DeviceRow.device_id == device_id))
        if stored is None:
            return False
        return secrets.compare_digest(token_hash(token).encode("ascii"), stored.encode("ascii"))

    def touch(self, device_ids: Iterable[str]) -> None:
        ids = set(device_ids)
        if not ids:
            return
        with self._db.begin() as db:
            db.execute(
                update(DeviceRow)
                .where(DeviceRow.device_id.in_(ids))
                .values(last_seen_at=self.clock())
            )


device_store = DeviceStore()
