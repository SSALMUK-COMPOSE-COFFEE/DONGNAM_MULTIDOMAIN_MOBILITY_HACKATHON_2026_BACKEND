from functools import lru_cache
from pathlib import Path

from freshswitch_backend.domain.models import Scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"


@lru_cache
def load_scenarios() -> dict[str, Scenario]:
    scenarios = (
        Scenario.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(SCENARIO_DIR.glob("*.json"))
    )
    return {s.id: s for s in scenarios}


def get_scenario(scenario_id: str) -> Scenario | None:
    return load_scenarios().get(scenario_id)
