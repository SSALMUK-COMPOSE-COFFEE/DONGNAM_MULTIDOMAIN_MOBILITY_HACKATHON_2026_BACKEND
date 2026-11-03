from typing import Literal

from pydantic import BaseModel

from freshswitch_backend.domain.models import Quantity


def _q(value: float, unit: str, source: str) -> Quantity:
    return Quantity(value=value, unit=unit, source=source)


class ExecutionParams(BaseModel):
    forwarder_processing_hours: Quantity = _q(
        12,
        "시간",
        "가정: 화주 확인 뒤 포워더가 선사·터미널·항공사에 요청을 넣고 회신을 받는 시간",
    )
    booking_amendment_lead_hours: Quantity = _q(
        24,
        "시간",
        "가정: 선적지시(SI)·VGM 마감이 CY 반입 마감보다 앞선다. 선사별 상이",
    )
    fallback_pickup_lead_hours: Quantity = _q(
        6,
        "시간",
        "가정: 집하 경로가 없을 때 경남 선과장→부산신항 운행·적입 리드타임",
    )
    harvest_notice_hours: Quantity = _q(
        12,
        "시간",
        "가정: 당겨 싣기 화주가 출하일 0시 기준 수확·예냉을 앞당기려면 필요한 통지 시간",
    )
    air_booking_lead_hours: Quantity = _q(
        24,
        "시간",
        "가정: 출하일 0시 기준 항공 화물 예약·반입 리드타임(인천 육송 약 5시간 + 보관)",
    )
    cancellation_fee_rate: Quantity = _q(
        1.0,
        "비율",
        "가정: 취소·감량 화주가 같은 컨테이너 다른 화주의 늘어난 운임·부대비 몫(데드프레이트)을 "
        "부담하는 비율. 심사 실무자 지적(judge-2: 미달분 데드프레이트 부담 규칙은 참여 계약서에)",
    )
    cancellation_consolidator: Literal["platform", "forwarder"] = "platform"
    cancellation_consolidator_source: str = (
        "결정(D12): 정산표 단가 화주는 컨테이너 몫이 늘어도 단가가 그대로라 손실이 없다. "
        "그 빈칸 비용은 혼재 주관자(플랫폼)가 떠안으므로 부담금도 플랫폼이 받는다. "
        "포워더 배분은 D2(0%)를 따른다"
    )
