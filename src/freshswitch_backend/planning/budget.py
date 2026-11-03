import time


class PlanningTimeout(TimeoutError):
    """The shared planning budget expired; no partial plan may be applied."""


def remaining(deadline: float | None, limit: float) -> float:
    if deadline is None:
        return limit
    available = deadline - time.perf_counter()
    if available <= 0:
        raise PlanningTimeout("계획 계산 시간이 초과되었습니다. 다시 요청해 주세요")
    return min(limit, available)
