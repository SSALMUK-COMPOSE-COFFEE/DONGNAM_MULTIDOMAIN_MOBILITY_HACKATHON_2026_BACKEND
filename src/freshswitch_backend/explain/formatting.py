import re

NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def format_krw(amount: int) -> str:
    return f"{amount:,}원"


def format_amount_krw(amount: int) -> str:
    magnitude = abs(amount)
    if magnitude >= 10_000:
        man = f"{magnitude / 10_000:,.1f}".removesuffix(".0")
        return f"{man}만 원"
    return f"{magnitude:,}원"


def format_delta_krw(delta: int) -> str:
    if delta == 0:
        return "변동 없음"
    sign = "+" if delta > 0 else "-"
    return f"{sign}{format_amount_krw(delta)}"


def change_direction(delta: int) -> str:
    if delta > 0:
        return "증가"
    if delta < 0:
        return "감소"
    return "변동 없음"


def change_verb(delta: int) -> str:
    if delta > 0:
        return "늘어납니다"
    if delta < 0:
        return "줄어듭니다"
    return "변동 없습니다"


def format_fill(before: int, after: int, capacity: int) -> str:
    return f"{before}→{after}/{capacity}파렛트"


def extract_numbers(text: str) -> set[str]:
    return {token.rstrip(",").replace(",", "") for token in NUMBER_PATTERN.findall(text)}
