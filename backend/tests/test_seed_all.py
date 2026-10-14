"""Unit tests for the seed orchestrator (BS-104).

These cover plan construction, step success/failure classification, and the
abort-on-required / continue-on-best-effort logic WITHOUT touching a database,
the network, or the real ingestion scripts.
"""

from __future__ import annotations

import sys

import scripts.seed_all as seed
from scripts.seed_all import Step, build_plan, main, run_step

# --- build_plan --------------------------------------------------------------


def test_build_plan_default_is_offline_core_in_order():
    plan = build_plan(skip_migrate=False, with_accommodations=False)
    assert [s.name for s in plan] == [
        "migrate",
        "destinations",
        "features",
        "accommodation-estimates",
        "fuel",
    ]
    required = {s.name: s.required for s in plan}
    # Schema + the data the app needs to rank must succeed; fuel + estimates can degrade.
    assert required == {
        "migrate": True,
        "destinations": True,
        "features": True,
        "accommodation-estimates": False,
        "fuel": False,
    }


def test_build_plan_skip_migrate_drops_only_migrate():
    plan = build_plan(skip_migrate=True, with_accommodations=False)
    assert [s.name for s in plan] == [
        "destinations",
        "features",
        "accommodation-estimates",
        "fuel",
    ]


def test_build_plan_with_accommodations_appends_best_effort_steps():
    plan = build_plan(skip_migrate=False, with_accommodations=True)
    names = [s.name for s in plan]
    assert names[-3:] == [
        "accommodations:ingest",
        "accommodations:clean",
        "accommodations:normalize",
    ]
    # The network-heavy import never aborts the core seed.
    assert all(not s.required for s in plan if s.name.startswith("accommodations"))


def test_features_step_uses_offline_snapshot_cost_source():
    plan = build_plan(skip_migrate=True, with_accommodations=False)
    features = next(s for s in plan if s.name == "features")
    assert "--cost-source" in features.argv and "snapshot" in features.argv


# --- run_step ----------------------------------------------------------------


def _inline(code: str, name: str = "x", required: bool = True) -> Step:
    return Step(name=name, argv=[sys.executable, "-c", code], description="t", required=required)


def test_run_step_returns_true_on_zero_exit():
    assert run_step(_inline("raise SystemExit(0)")) is True


def test_run_step_returns_false_on_nonzero_exit():
    assert run_step(_inline("raise SystemExit(3)")) is False


def test_run_step_returns_false_when_command_cannot_start():
    step = Step(name="x", argv=["this-binary-does-not-exist-bs104"], description="t")
    assert run_step(step) is False


# --- main orchestration ------------------------------------------------------


def test_main_dry_run_executes_nothing(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(seed, "run_step", lambda step: calls.append(step.name) or True)
    assert main(["--dry-run", "--with-accommodations"]) == 0
    assert calls == []


def test_main_aborts_after_required_failure(monkeypatch):
    plan = [
        Step("a", ["x"], "t", required=True),
        Step("b", ["x"], "t", required=True),
        Step("c", ["x"], "t", required=False),
    ]
    monkeypatch.setattr(seed, "build_plan", lambda **_: plan)
    run_order: list[str] = []

    def fake_run(step: Step) -> bool:
        run_order.append(step.name)
        return step.name != "a"  # first required step fails

    monkeypatch.setattr(seed, "run_step", fake_run)
    assert main([]) == 1
    assert run_order == ["a"]  # stopped immediately, never ran b or c


def test_main_continues_after_best_effort_failure(monkeypatch):
    plan = [
        Step("a", ["x"], "t", required=True),
        Step("b", ["x"], "t", required=False),
        Step("c", ["x"], "t", required=True),
    ]
    monkeypatch.setattr(seed, "build_plan", lambda **_: plan)
    run_order: list[str] = []

    def fake_run(step: Step) -> bool:
        run_order.append(step.name)
        return step.name != "b"  # best-effort step fails

    monkeypatch.setattr(seed, "run_step", fake_run)
    # Best-effort failure => exit 0, and later steps still run.
    assert main([]) == 0
    assert run_order == ["a", "b", "c"]
