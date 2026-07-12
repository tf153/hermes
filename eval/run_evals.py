"""Named eval set runner for the travel-desk crew.

Runs a fixed, version-controlled set of traveller messages through the real
Intake Analyst and Trip Director agents and scores their structured output
against expected outcomes, so a prompt/model change can be compared version to
version. Exits non-zero when the pass rate drops below the dataset threshold,
so it can gate a release in CI.

Usage:
    python -m eval.run_evals            # run the crew live (needs hermes configured)
    python -m eval.run_evals --offline  # validate dataset + harness only, no API calls
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "dataset.json"


def _load() -> dict:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def _score_case(spec: dict, needs_review: bool, expect: dict) -> dict:
    checks: dict[str, bool] = {}

    dest = (spec.get("destination") or "").lower()
    checks["destination"] = expect["destination_contains"].lower() in dest

    personas = set(spec.get("personas") or [])
    checks["persona"] = bool(personas.intersection(expect["personas_any"]))

    checks["accessibility_routing"] = bool(needs_review) == bool(
        expect["needs_accessibility_review"]
    )
    return checks


async def _run_live(cases: list[dict]) -> list[dict]:
    # Imported lazily so --offline works without a configured environment.
    from app import agents

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = []
    for case in cases:
        msg = case["message"]
        spec = await agents.intake_analyst(msg, None, today, "Goa, India")
        decision = await agents.manager_plan(msg, None)
        # An accessibility review fires if either the manager asks for it or the
        # spec itself implies mobility limits - mirror the pipeline's logic.
        needs_review = bool(
            decision.get("needs_accessibility_review")
        ) or agents.spec_has_mobility_limits(spec)
        checks = _score_case(spec, needs_review, case["expect"])
        results.append(
            {
                "id": case["id"],
                "checks": checks,
                "destination": spec.get("destination"),
                "personas": spec.get("personas"),
                "needs_review": needs_review,
            }
        )
    return results


def _validate_offline(data: dict) -> None:
    """Structural smoke test - always runnable in CI without secrets."""
    assert isinstance(data.get("cases"), list) and data["cases"], "no cases"
    assert 0 < float(data.get("threshold", 0)) <= 1, "bad threshold"
    valid_personas = {
        "pilgrimage", "sunset", "trek", "photography", "family_with_kids",
        "seniors_low_mobility", "accessibility_first", "food", "slow_traveler",
        "beaches", "nature",
    }
    ids = set()
    for case in data["cases"]:
        assert case["id"] not in ids, f"duplicate id {case['id']}"
        ids.add(case["id"])
        exp = case["expect"]
        assert exp["destination_contains"], f"{case['id']}: empty destination"
        assert exp["personas_any"], f"{case['id']}: no expected personas"
        unknown = set(exp["personas_any"]) - valid_personas
        assert not unknown, f"{case['id']}: unknown personas {unknown}"
        assert isinstance(exp["needs_accessibility_review"], bool)
    print(f"offline check OK: {len(data['cases'])} cases, all well-formed")


def _report(results: list[dict], threshold: float) -> bool:
    total = passed = 0
    print(f"\n{'case':22} {'dest':>5} {'persona':>8} {'a11y':>5}")
    print("-" * 46)
    for r in results:
        c = r["checks"]
        total += len(c)
        passed += sum(c.values())
        mark = lambda ok: " ok " if ok else "MISS"  # noqa: E731
        print(
            f"{r['id']:22} {mark(c['destination']):>5} "
            f"{mark(c['persona']):>8} {mark(c['accessibility_routing']):>5}"
        )
    rate = passed / total if total else 0.0
    print("-" * 46)
    print(f"pass rate: {passed}/{total} = {rate:.0%} (threshold {threshold:.0%})")
    return rate >= threshold


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the crew eval set.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="validate the dataset and harness without calling any API",
    )
    args = parser.parse_args()

    data = _load()
    if args.offline:
        _validate_offline(data)
        return 0

    results = asyncio.run(_run_live(data["cases"]))
    ok = _report(results, float(data.get("threshold", 0.8)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
