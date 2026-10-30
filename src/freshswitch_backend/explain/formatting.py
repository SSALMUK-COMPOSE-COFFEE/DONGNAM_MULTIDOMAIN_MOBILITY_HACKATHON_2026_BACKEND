import re

NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def format_krw(amount: int) -> str:
    return f"{amount:,}원"


def format_delta_krw(delta: int) -> str:
    if delta == 0:
        return "변동 없음"
    sign = "+" if delta > 0 else "-"
    magnitude = abs(delta)
    if magnitude >= 10_000:
        man = f"{magnitude / 10_000:.1f}".removesuffix(".0")
        return f"{sign}{man}만 원"
    return f"{sign}{magnitude:,}원"


def format_fill(before: int, after: int, capacity: int) -> str:
    return f"{before}→{after}/{capacity}파렛트"


def extract_numbers(text: str) -> set[str]:
    return {token.rstrip(",").replace(",", "") for token in NUMBER_PATTERN.findall(text)}
