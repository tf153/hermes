"""Named eval set runner for the travel-desk crew, with a closed loop.

Runs a fixed, version-controlled set of traveller messages - plus any cases
captured from real production failures (`eval/capture.py`) - through the real
Intake Analyst and Trip Director agents and scores their structured output
against expected outcomes. Results are saved per version so a prompt/model change
can be compared across versions, and the run exits non-zero when the pass rate
drops below the dataset threshold, so it can gate a release in CI.

Usage:
    python -m eval.run_evals                 # live run (needs hermes configured)
    python -m eval.run_evals --version v3    # tag the results with a version label
    python -m eval.run_evals --offline       # validate dataset + harness, no API calls
    python -m eval.run_evals --trend         # print pass rate across saved versions
"""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval import capture

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.json"
RESULTS_DIR = HERE / "results"


def _load() -> dict:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def _git_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=HERE, timeout=5,
        ).stdout.strip()
        return sha or "dev"
    except (subprocess.SubprocessError, OSError):
        return "dev"


def _captured_as_cases() -> list[dict]:
    """Fold captured production failures into the eval set as regression cases."""
    cases: list[dict] = []
    for i, rec in enumerate(capture.load_captured()):
        message = (rec.get("message") or "").strip()
        if not message:
            continue
        expect: dict = {
            "needs_accessibility_review": bool(rec.get("needs_accessibility_review"))
        }
        dest = rec.get("destination")
        if dest:
            expect["destination_contains"] = dest.split(",")[0].strip().lower()
        cases.append(
            {
                "id": f"cap-{i}-{rec.get('reason', 'flagged')}",
                "message": message,
                "expect": expect,
                "source": "captured",
            }
        )
    return cases


def _score_case(spec: dict, needs_review: bool, expect: dict) -> dict:
    """Run only the checks the case actually specifies (captured cases are sparse)."""
    checks: dict[str, bool] = {}
    if "destination_contains" in expect:
        dest = (spec.get("destination") or "").lower()
        checks["destination"] = expect["destination_contains"].lower() in dest
    if "personas_any" in expect:
        personas = set(spec.get("personas") or [])
        checks["persona"] = bool(personas.intersection(expect["personas_any"]))
    if "needs_accessibility_review" in expect:
        checks["accessibility_routing"] = bool(needs_review) == bool(
            expect["needs_accessibility_review"]
        )
    return checks


async def _run_live(cases: list[dict]) -> list[dict]:
    from app import agents  # lazy import so --offline needs no environment

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = []
    for case in cases:
        msg = case["message"]
        spec = await agents.intake_analyst(msg, None, today, "Goa, India")
        decision = await agents.compose_crew(spec)
        needs_review = bool(
            decision.get("needs_accessibility_review")
        ) or agents.spec_has_mobility_limits(spec)
        checks = _score_case(spec, needs_review, case["expect"])
        results.append(
            {
                "id": case["id"],
                "source": case.get("source", "dataset"),
                "checks": checks,
                "destination": spec.get("destination"),
                "personas": spec.get("personas"),
                "crew": [c.get("role") for c in decision.get("crew") or []],
                "needs_review": needs_review,
            }
        )
    return results


def _validate_offline(data: dict) -> None:
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
    print(f"captured cases available: {len(capture.load_captured())}")


def _report(results: list[dict], threshold: float, version: str) -> bool:
    total = passed = 0
    print(f"\nversion {version}")
    print(f"{'case':26} {'src':>9} {'dest':>5} {'persona':>8} {'a11y':>5}")
    print("-" * 58)
    for r in results:
        c = r["checks"]
        total += len(c)
        passed += sum(c.values())
        cell = lambda k: (" ok " if c[k] else "MISS") if k in c else "  - "  # noqa: E731
        print(
            f"{r['id']:26} {r.get('source','dataset'):>9} "
            f"{cell('destination'):>5} {cell('persona'):>8} "
            f"{cell('accessibility_routing'):>5}"
        )
    rate = passed / total if total else 0.0
    print("-" * 58)
    print(f"pass rate: {passed}/{total} = {rate:.0%} (threshold {threshold:.0%})")
    _save_results(version, results, rate, passed, total)
    return rate >= threshold


def _save_results(version, results, rate, passed, total) -> None:
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / f"{version}.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "pass_rate": round(rate, 4),
                    "passed": passed,
                    "total": total,
                    "n_cases": len(results),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"saved results to eval/results/{version}.json")
    except OSError as exc:
        print(f"warning: could not save results: {exc}", file=sys.stderr)


def _trend() -> None:
    if not RESULTS_DIR.exists():
        print("no saved results yet - run the evals first.")
        return
    rows = []
    for path in RESULTS_DIR.glob("*.json"):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda r: r.get("timestamp") or "")
    print(f"{'version':16} {'when':20} {'cases':>6} {'pass rate':>10}")
    print("-" * 56)
    for r in rows:
        when = (r.get("timestamp") or "")[:19]
        print(
            f"{r.get('version',''):16} {when:20} {r.get('n_cases',0):>6} "
            f"{r.get('pass_rate',0):>9.0%}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the crew eval set.")
    parser.add_argument("--offline", action="store_true",
                        help="validate dataset and harness without calling any API")
    parser.add_argument("--trend", action="store_true",
                        help="print pass rate across saved versions and exit")
    parser.add_argument("--version", default=None,
                        help="version label for the saved results (default: git sha)")
    args = parser.parse_args()

    if args.trend:
        _trend()
        return 0

    data = _load()
    if args.offline:
        _validate_offline(data)
        return 0

    version = args.version or _git_version()
    cases = list(data["cases"]) + _captured_as_cases()
    results = asyncio.run(_run_live(cases))
    ok = _report(results, float(data.get("threshold", 0.8)), version)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
