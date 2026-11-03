"""Run offline workflow, provenance and synthetic load checks without touching a server.

uv run python -m freshswitch_backend.readiness --output .venv/readiness-report.json
"""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.data.provenance import provenance_catalog
from freshswitch_backend.evaluation import plan_checks
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.budget import PlanningTimeout
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan import ReduceEvent, locations, replan


def workflow_evaluation() -> dict:
    checks = []
    original_clock = store.clock
    simulation_time = datetime(2026, 11, 10, tzinfo=UTC)
    store.clock = lambda: simulation_time
    try:
        with TestClient(create_app()) as client:

            def call(method, path, body=None, status=200):
                response = client.request(method, path, json=body)
                assert response.status_code == status, f"{method} {path}: unexpected HTTP status"
                return response.json()

            def new_plan():
                return call("POST", "/api/plans", {"scenario_id": "default"}, 201)

            def apply(plan_id, event, key):
                prefix = f"/api/plans/{plan_id}"
                event_body = {"event": event, "idempotency_key": key}
                result = call("POST", prefix + "/events", event_body)
                replay = call("POST", prefix + "/events", event_body)
                assert replay == result, "event replay must return the same result"
                assert result["recommended_id"], "no feasible recommended alternative"
                body = {
                    "alternative_id": result["recommended_id"],
                    "replan_id": result["replan_id"],
                    "idempotency_key": key + "-apply",
                }
                applied = call("POST", prefix + "/apply", body)
                replay = call("POST", prefix + "/apply", body)
                assert replay == applied, "apply replay must not advance version"
                order = call("GET", prefix + "/orders")[-1]
                return result, applied, order

            def execute(plan_id, order):
                prefix = f"/api/plans/{plan_id}/orders/{order['order_id']}"
                for confirmation in order["shipper_confirmations"]:
                    body = {
                        "actor": "shipper",
                        "shipper_id": confirmation["shipper_id"],
                        "accept": True,
                    }
                    call("POST", prefix + "/confirm", body)
                    call("POST", prefix + "/confirm", body)
                confirmed = call(
                    "POST", prefix + "/confirm", {"actor": "forwarder", "accept": True}
                )
                for request in confirmed["requests"]:
                    call("POST", prefix + "/requests/" + request["request_id"], {"accepted": True})
                    call("POST", prefix + "/requests/" + request["request_id"], {"accepted": True})
                assert call("GET", prefix)["status"] == "executed", "order not executed"
                current = call("GET", f"/api/plans/{plan_id}")
                assert current["status"] == "committed" and current["active_order_id"] is None
                return current

            session = new_plan()
            plan_id = session["plan_id"]
            events = [
                {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3},
                {
                    "type": "add_lot",
                    "lot": {
                        **next(
                            lot
                            for lot in session["scenario"]["lots"]
                            if lot["id"] == "lot-sancheong-sb-1"
                        ),
                        "id": "workflow-added-lot",
                        "pallets": 2,
                    },
                },
                {"type": "air_switch", "lot_id": "lot-jinju-sb-1"},
            ]
            for index, event in enumerate(events):
                _, applied, order = apply(plan_id, event, f"workflow-{index}")
                session = execute(plan_id, order)
                assert session["committed_version"] == applied["version"]
            assert len(call("GET", f"/api/plans/{plan_id}/orders")) == 3
            assert len(call("GET", f"/api/plans/{plan_id}/history")) == 3
            checks.append(
                {"name": "three_sequential_changes_and_duplicate_confirmations", "passed": True}
            )

            # A rejected fourth change must roll back to the most recent committed plan.
            before_reject = session
            _, _, rejected_order = apply(
                plan_id,
                {
                    "type": "reduce",
                    "lot_id": "lot-sancheong-sb-1",
                    "new_pallets": 2,
                },
                "reject-fourth",
            )
            prefix = f"/api/plans/{plan_id}/orders/{rejected_order['order_id']}"
            confirmations = rejected_order["shipper_confirmations"]
            if confirmations:
                rejection = {
                    "actor": "shipper",
                    "shipper_id": confirmations[0]["shipper_id"],
                    "accept": False,
                }
            else:
                rejection = {"actor": "forwarder", "accept": False}
            assert call("POST", prefix + "/confirm", rejection)["status"] == "rejected"
            session = call("GET", f"/api/plans/{plan_id}")
            assert session["plan"] == before_reject["plan"], (
                "rollback lost a previously executed change"
            )
            checks.append({"name": "rollback_preserves_previous_executed_changes", "passed": True})

            # Repeating the same arrival must not double the refund or learning observation.
            lot = next(a for a in session["plan"]["assignments"] if a["mode"] == "sea")
            arrival = {"lot_id": lot["lot_id"], "inspected_loss_pct": 100}
            prefix = f"/api/plans/{plan_id}/settlement"
            first = call("POST", prefix + "/arrival", arrival)
            settlement = call("GET", prefix)
            count = call("GET", "/api/quality/observations")["count"]
            second = call("POST", prefix + "/arrival", arrival)
            assert second["refund_change"] == 0 and second["platform_net_change"] == 0
            assert call("GET", prefix) == settlement
            assert call("GET", "/api/quality/observations")["count"] == count
            assert first["refund_after"] <= first["refund_cap"]
            assert all(row["fee"] >= 0 for row in settlement["settlements"])
            checks.append(
                {"name": "arrival_replay_refund_cap_and_learning_deduplication", "passed": True}
            )

            expired = new_plan()
            _, _, order = apply(expired["plan_id"], events[0], "expiry")
            simulation_time = datetime.fromisoformat(order["deadlines"]["shipper"])
            simulation_time += timedelta(seconds=1)
            current = call("GET", f"/api/plans/{expired['plan_id']}")
            assert current["plan"] == expired["plan"] and current["status"] == "committed"
            checks.append({"name": "confirmation_expiry_restores_committed_plan", "passed": True})
    except (AssertionError, ValueError, KeyError) as error:
        checks.append(
            {
                "name": "workflow_failure",
                "passed": False,
                "error_type": type(error).__name__,
                "message": str(error),
            }
        )
    finally:
        store.clock = original_clock
    return {
        "scope": "isolated in-process HTTP simulation; no carrier bookings or server writes",
        "simulation_start": "2026-11-10T00:00:00+00:00",
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


def scaled_scenario(factor: int):
    if factor < 1 or factor > 20:
        raise ValueError("scale factor must be between 1 and 20")
    original = get_scenario("default")
    lots, shippers, containers, trucks = [], [], [], []
    for index in range(factor):
        suffix = f"-load{index}"
        shippers.extend(s.model_copy(update={"id": s.id + suffix}) for s in original.shippers)
        lots.extend(
            lot.model_copy(update={"id": lot.id + suffix, "shipper_id": lot.shipper_id + suffix})
            for lot in original.lots
        )
        containers.extend(c.model_copy(update={"id": c.id + suffix}) for c in original.containers)
        trucks.extend(t.model_copy(update={"id": t.id + suffix}) for t in original.trucks)
    return original.model_copy(
        update={
            "id": f"synthetic-load-{factor}",
            "lots": lots,
            "shippers": shippers,
            "containers": containers,
            "trucks": trucks,
        }
    )


def scalability_evaluation(factors=(1, 3, 5), budget_seconds: float = 10) -> dict:
    if not 0 < budget_seconds <= 60:
        raise ValueError("budget_seconds must be in (0, 60]")
    rows = []
    for factor in factors:
        scenario = scaled_scenario(factor)
        row = {
            "factor": factor,
            "lots": len(scenario.lots),
            "shippers": len(scenario.shippers),
            "containers": len(scenario.containers),
            "trucks": len(scenario.trucks),
            "budget_seconds_per_stage": budget_seconds,
        }
        stage = "initial_plan"
        started = time.perf_counter()
        try:
            built = build_plan(scenario, deadline=started + budget_seconds)
            row["initial_plan_ms"] = round((time.perf_counter() - started) * 1000)
            row["initial_solver_status"] = built.plan.status
            if built.plan.status not in ("OPTIMAL", "FEASIBLE") or built.plan.unassigned:
                row.update(
                    status="no_complete_initial_plan",
                    passed=False,
                    unassigned_lots=len(built.plan.unassigned),
                )
                rows.append(row)
                continue
            errors = plan_checks(scenario, built.plan, built.routes, {})
            stage = "replan"
            event_lot = next(
                lot for lot in scenario.lots if lot.id.startswith("lot-sancheong-sb-1-")
            )
            started = time.perf_counter()
            result = replan(
                scenario,
                built.plan,
                ReduceEvent(lot_id=event_lot.id, new_pallets=3),
                deadline=started + budget_seconds,
            )
            row["replan_ms"] = round((time.perf_counter() - started) * 1000)
            recommended = next(
                (a for a in result.alternatives if a.id == result.recommended_id), None
            )
            if recommended:
                base = locations(built.plan)
                errors += plan_checks(
                    result.scenario_after,
                    recommended.plan,
                    recommended.routes,
                    {lid: base[lid] for lid in result.fixed_lot_ids},
                )
            row.update(
                status="completed",
                violations=errors,
                unassigned_lots=len(built.plan.unassigned),
                recommended_unassigned_lots=len(recommended.plan.unassigned)
                if recommended
                else None,
                passed=bool(
                    not errors
                    and not built.plan.unassigned
                    and recommended
                    and not recommended.plan.unassigned
                    and row["initial_plan_ms"] <= budget_seconds * 1000
                    and row["replan_ms"] <= budget_seconds * 1000
                ),
            )
        except PlanningTimeout:
            row.update(
                status="timeout",
                timed_out_stage=stage,
                passed=False,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
        rows.append(row)
    return {
        "scope": "synthetic replicated shipments/resources at the same locations; engine only, no LLM",
        "limitations": "Not a concurrency, production traffic or geographic diversity benchmark",
        "passed": all(row["passed"] for row in rows),
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale-factors", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument("--budget-seconds", type=float, default=10)
    args = parser.parse_args()
    if any(factor < 1 or factor > 20 for factor in args.scale_factors):
        parser.error("scale factors must be between 1 and 20")
    if not 0 < args.budget_seconds <= 60:
        parser.error("budget seconds must be in (0, 60]")
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workflow": workflow_evaluation(),
        "provenance": provenance_catalog(),
        "scalability": scalability_evaluation(args.scale_factors, args.budget_seconds),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Readiness report written to {args.output}")
    return 0 if report["workflow"]["passed"] and report["scalability"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
