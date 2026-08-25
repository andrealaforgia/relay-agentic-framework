"""The toolchain earns the right to judge code BEFORE any code is written.

The ubies freeze: twelve acceptance runs 'failed' in pristine worktrees whose
dependencies were never installed, red-verification blessed the crashes, and
the builder burned three attempts against a suite that never loaded. When the
plan declares a `setup` command, the coordinator now proves it right after
plan.committed — one loud fault in minutes instead of a silent afternoon —
and every run carries it so each fresh checkout bootstraps."""

from __future__ import annotations

from test_coordinator import ROADMAP, MiniSwarm

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import Policy

SHA = "4" * 40
PLAN = {"iteration_id": "I1", "plan_path": "docs/relay/plans/I1.md",
        "summary": "extend the existing abstraction", "commit_sha": SHA}
PLAN_POLICY = Policy(plan_required=True)


def _start(client, publisher, commands: dict[str, str], mode: str = "greenfield") -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=PLAN_POLICY)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": mode}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    publisher.send("planner", "coordinator", "plan.committed",
                   {**PLAN, "commands": commands}, iteration_id="I1")
    mini.pump()
    return mini


def _setup_run(swarm: MiniSwarm):
    runs = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "setup"]
    return runs[-1] if runs else None


def test_the_plan_setup_is_proven_before_any_behaviour_dispatches(client, publisher) -> None:
    swarm = _start(client, publisher,
                   {"acceptance_test": "npx playwright test {test_paths}", "setup": "npm ci"})
    run = _setup_run(swarm)
    assert run is not None and run.payload["command"] == "npm ci"
    assert swarm.sent("spec.requested") == []       # held until the proof lands

    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "setup", "commit_sha": SHA,
                    "exit_code": 0, "duration_s": 8.0, "output_digest": "d" * 64})
    swarm.pump()
    assert len(swarm.sent("spec.requested")) >= 1   # proven: work flows
    # and every subsequent run carries the bootstrap for its pristine worktree
    at_run = [r for r in swarm.sent("run.requested") if r.payload["kind"] != "setup"]
    assert all(r.payload.get("setup_command") == "npm ci" for r in at_run) or not at_run

    # replay: a fresh coordinator does not re-prove or re-dispatch
    fresh = MiniSwarm(client, publisher, policy=PLAN_POLICY)
    fresh.pump()
    assert len([r for r in fresh.sent("run.requested")
                if r.payload["kind"] == "setup"]) == len(
        [r for r in swarm.sent("run.requested") if r.payload["kind"] == "setup"])


def test_a_failed_setup_blocks_the_iteration_loudly_and_retry_reproves(client, publisher) -> None:
    """On a LEGACY codebase there is nothing to scaffold — a broken setup goes
    straight to the human who owns the environment."""
    swarm = _start(client, publisher,
                   {"acceptance_test": "npx playwright test {test_paths}", "setup": "npm ci"},
                   mode="legacy")
    run = _setup_run(swarm)
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "setup", "commit_sha": SHA,
                    "exit_code": 1, "duration_s": 2.0, "output_digest": "d" * 64,
                    "summary": "npm ci: EAI_AGAIN registry.npmjs.org"})
    swarm.pump()
    assert swarm.sent("spec.requested") == []       # nothing built on a broken bench
    asks = [e for e in swarm.sent("decision.requested")
            if e.payload["subject_id"] == "I1"]
    assert len(asks) == 1                           # asked once, not twinned
    assert "setup command" in asks[0].payload["reason"]

    gate_id = asks[0].payload["gate_id"]
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1", "decision": "retry"})
    swarm.pump()
    reruns = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "setup"]
    assert len(reruns) == 2                         # the proof re-runs after retry


def test_a_plan_without_setup_changes_nothing(client, publisher) -> None:
    swarm = _start(client, publisher, {"acceptance_test": "uv run pytest -q {test_paths}"})
    assert _setup_run(swarm) is None
    assert len(swarm.sent("spec.requested")) >= 1
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.SPEC_DISPATCHED


NPM_EUSAGE = ("npm error code EUSAGE\nnpm error The `npm ci` command can only "
              "install with an existing package-lock.json")


def test_greenfield_setup_failure_sends_the_builder_not_the_owner(client, publisher) -> None:
    """The ubies greenfield freeze: the plan proposed a stack, the setup proof
    ran `npm ci` against a repo with only docs in it, and the OWNER was asked
    to fix a 'startup-step defect'. Wrong recipient: the builder initialises
    the project, the proof re-runs, work begins — no human in the loop."""
    swarm = _start(client, publisher,
                   {"acceptance_test": "npx playwright test {test_paths}",
                    "setup": "npm ci && npx playwright install chromium"})
    run = _setup_run(swarm)
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "setup", "commit_sha": SHA,
                    "exit_code": 1, "duration_s": 1.0, "output_digest": "d" * 64,
                    "summary": NPM_EUSAGE})
    swarm.pump()

    assert swarm.sent("decision.requested") == []       # the Owner hears nothing
    (scaffold,) = swarm.sent("scaffold.requested")
    assert scaffold.to_role == "builder"
    assert "npm ci" in scaffold.payload["detail"]       # the evidence travels

    publisher.send("builder", "coordinator", "scaffold.completed",
                   {"iteration_id": "I1", "commit_sha": "5" * 40,
                    "summary": "Vite TS skeleton, lockfile, playwright config"})
    swarm.pump()
    setups = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "setup"]
    assert len(setups) == 2                             # the proof re-runs at new HEAD
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": setups[-1].payload["run_id"], "kind": "setup",
                    "commit_sha": setups[-1].payload["commit_sha"], "exit_code": 0,
                    "duration_s": 20.0, "output_digest": "e" * 64})
    swarm.pump()
    assert len(swarm.sent("spec.requested")) >= 1       # work begins

    # replay: nothing re-dispatches
    fresh = MiniSwarm(client, publisher, policy=PLAN_POLICY)
    fresh.pump()
    assert len(fresh.sent("scaffold.requested")) == 1
    assert len([r for r in fresh.sent("run.requested")
                if r.payload["kind"] == "setup"]) == 2


def test_setup_failing_even_after_the_scaffold_reaches_the_owner(client, publisher) -> None:
    swarm = _start(client, publisher, {"acceptance_test": "x", "setup": "npm ci"})
    run = _setup_run(swarm)
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "setup", "commit_sha": SHA,
                    "exit_code": 1, "duration_s": 1.0, "output_digest": "d" * 64,
                    "summary": NPM_EUSAGE})
    swarm.pump()
    publisher.send("builder", "coordinator", "scaffold.completed",
                   {"iteration_id": "I1", "commit_sha": "5" * 40})
    swarm.pump()
    second = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "setup"][-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": second.payload["run_id"], "kind": "setup",
                    "commit_sha": second.payload["commit_sha"], "exit_code": 1,
                    "duration_s": 1.0, "output_digest": "d" * 64,
                    "summary": "EAI_AGAIN registry.npmjs.org"})
    swarm.pump()
    asks = [e for e in swarm.sent("decision.requested")
            if e.payload["subject_id"] == "I1"]
    assert len(asks) == 1                               # now it IS the Owner's
    assert len(swarm.sent("scaffold.requested")) == 1   # never scaffolds twice


def test_a_stalled_scaffold_is_supervised(client, publisher) -> None:
    import time as _time

    swarm = _start(client, publisher, {"acceptance_test": "x", "setup": "npm ci"})
    run = _setup_run(swarm)
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "setup", "commit_sha": SHA,
                    "exit_code": 1, "duration_s": 1.0, "output_digest": "d" * 64,
                    "summary": NPM_EUSAGE})
    swarm.pump()
    overdue = _time.time() + PLAN_POLICY.dispatch_timeout_s + 1
    swarm.dispatcher.tick(swarm.state, overdue)
    swarm.pump()
    assert len(swarm.sent("scaffold.requested")) == 2   # one re-dispatch
    swarm.dispatcher.tick(swarm.state, overdue + PLAN_POLICY.dispatch_timeout_s + 1)
    asks = [e for e in swarm.sent("decision.requested") if e.payload["subject_id"] == "I1"]
    assert len(asks) == 1 and "builder" in asks[0].payload["reason"]
