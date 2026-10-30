from pydantic import BaseModel

from freshswitch_backend.domain.models import Quantity, _q


class QualityParams(BaseModel):
    prior_weight_pallets: Quantity = _q(
        20, "파렛트", "가정: 사전 손실률을 컨테이너 1대(20파렛트) 분량의 관측과 같은 무게로 둔다"
    )
    short_outage_minutes: Quantity = _q(30, "분", "가정: 30분 미만 단전은 단전 없음으로 본다")
    long_outage_minutes: Quantity = _q(
        180,
        "분",
        "가정: 3시간 이상을 긴 단전으로 본다. JLST 6권 2호(2025.6) 누적 5시간 56분의 절반 수준",
    )
    prior_relative_sd: Quantity = _q(
        0.5, "배", "가정: 사전 손실률의 불확실성을 사전값의 ±50%(1표준편차)로 둔다"
    )
    range_z: Quantity = _q(1.645, "z", "정규 근사 90% 범위")
