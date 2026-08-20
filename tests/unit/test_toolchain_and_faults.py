"""The toolchain comes from the approved change plan, and a command that never
ran is never evidence about the code.

Both halves of one incident: the toolgate held `uv run pytest` for a Rust
project, every acceptance run "failed" for ninety minutes, red-verification
was satisfied by a missing interpreter, and two behaviours blocked with
"acceptance test still failing after build" while `cargo test` was green.
"""

from __future__ import annotations

from test_coordinator import ROADMAP, SHA_BUILD, SHA_SPEC, MiniSwarm

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import GateSpec, Policy
from relay.workers.faults import NOT_EXECUTABLE, NO_COMMAND, TIMEOUT, classify

PLAN_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

# What `uv run pytest` actually printed, and the code it actually exited with:
# 2, not 127, because uv itself was found and it was uv that failed to spawn.
UV_SPAWN_FAILURE = (
    "error: Failed to spawn: `pytest`\n"
    "  Caused by: No such file or directory (os error 2)"
)


# ── the classifier: which non-zero exits are not evidence ────────────────────

def test_shell_cannot_find_the_program() -> None:
    assert classify(127, "sh: cargo: command not found") == NOT_EXECUTABLE
    assert classify(126, "sh: /x: Permission denied") == NOT_EXECUTABLE


def test_launcher_that_reports_its_own_spawn_failure() -> None:
    # the incident: exit 2, so exit code alone can never catch it
    assert classify(2, UV_SPAWN_FAILURE) == NOT_EXECUTABLE


def test_a_real_test_failure_is_evidence_and_never_a_fault() -> None:
    assert classify(101, "test result: FAILED. 1 passed; 1 failed") is None
    assert classify(1, "assert 0 == 3\nE  AssertionError\n1 failed in 0.4s") is None
    assert classify(0, "") is None


def test_a_long_run_that_merely_mentions_a_missing_file_is_evidence() -> None:
    # a suite that ran, printed a summary, and happens to contain the words:
    # signatures only speak for output short enough to be a launcher's dying breath
    output = "no such file or directory\n" + ("test_x ... ok\n" * 60) + "1 failed"
    assert classify(1, output) is None


# ── the coordinator: a fault stops the machine instead of feeding it ─────────

def _spec_written(swarm: MiniSwarm, bid: str = "I1.S1.B1") -> None:
    swarm.publisher.send("specifier", "coordinator", "spec.written",
                         {"behaviour_id": bid, "test_paths": ["tests/acceptance/test_b1.py"],
                          "commit_sha": SHA_SPEC, "touches": ["src/rooms/cli.py"]})
    swarm.pump()


def _complete_last_run(swarm: MiniSwarm, sha: str, **extra: object) -> None:
    run = swarm.sent("run.requested")[-1]
    payload = {"run_id": run.payload["run_id"], "kind": "acceptance_test",
               "commit_sha": sha, "duration_s": 0.2, "output_digest": "f" * 64}
    payload.update(extra)
    swarm.publisher.send("toolgate", "coordinator", "run.completed", payload)
    swarm.pump()


def test_spawn_failure_is_never_a_verified_red(client, publisher) -> None:
    """The headline defect: a non-zero exit from a command that never ran used
    to satisfy "prove the test fails first"."""
    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=2, summary=UV_SPAWN_FAILURE,
                       fault=NOT_EXECUTABLE)

    b = swarm.behaviour("I1.S1.B1")
    assert b.state is not BehaviourState.RED_VERIFIED   # nothing was proved
    assert b.state is BehaviourState.BLOCKED            # and the swarm stops
    assert swarm.sent("build.requested") == []          # no build on a phantom red
    (escalation,) = swarm.sent("decision.requested")
    assert escalation.to_role == "interpreter"
    assert escalation.payload["subject_id"] == "I1.S1.B1"
    reason = escalation.payload["reason"]
    assert "did not run" in reason and "environment" in reason


def test_spawn_failure_after_build_is_not_a_failing_test(client, publisher) -> None:
    """The reason the roadmap said "acceptance test still failing after build"
    about code whose tests were green."""
    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=1, summary="1 failed")   # a real red
    publisher.send("builder", "coordinator", "behaviour.built",
                   {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
                    "commit_sha": SHA_BUILD, "attempt": 1})
    swarm.pump()
    _complete_last_run(swarm, SHA_BUILD, exit_code=2, summary=UV_SPAWN_FAILURE,
                       fault=NOT_EXECUTABLE)

    b = swarm.behaviour("I1.S1.B1")
    assert b.last_fail_reason != "acceptance test still failing after build"
    assert b.state is BehaviourState.BLOCKED
    assert len(swarm.sent("decision.requested")) == 1


def test_the_owner_retry_clears_the_fault_and_re_runs_the_cycle(client, publisher) -> None:
    # a wip limit wide enough that the freed slot does not go to the story's
    # integration behaviours instead: this test is about B1 getting back to work
    swarm = MiniSwarm(client, publisher, policy=Policy(wip_limit=3))
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=2, summary=UV_SPAWN_FAILURE,
                       fault=NOT_EXECUTABLE)
    escalation = swarm.sent("decision.requested")[-1]

    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": escalation.payload["gate_id"],
                    "subject_id": "I1.S1.B1", "decision": "retry"})
    swarm.pump()

    b = swarm.behaviour("I1.S1.B1")
    assert b.infra_fault is None                        # the fault does not survive
    assert b.state is BehaviourState.SPEC_DISPATCHED    # the whole cycle re-runs
    assert not swarm.state.decision_mismatch


def test_one_escalation_per_fault_not_one_per_react(client, publisher) -> None:
    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=2, summary=UV_SPAWN_FAILURE,
                       fault=NOT_EXECUTABLE)
    swarm.pump()
    swarm.pump()
    assert len(swarm.sent("decision.requested")) == 1

    fresh = MiniSwarm(client, publisher)                # cold restart, same ledger
    fresh.pump()
    assert len(fresh.sent("decision.requested")) == 1   # replay re-asks nothing


MUTATION_POLICY = Policy(per_story=(GateSpec(gate="mutation", role="qa"),))


def test_a_faulted_mutation_run_never_reaches_the_gate(client, publisher) -> None:
    """cargo mutants missing must not look like surviving mutants, and qa must
    never be asked to judge a run that did not happen."""
    from test_coordinator import _drive_behaviour_to_done

    swarm = MiniSwarm(client, publisher, policy=MUTATION_POLICY)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")

    mutation = [r for r in swarm.sent("run.requested")
                if r.payload["kind"] == "mutation"][-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": mutation.payload["run_id"], "kind": "mutation",
                    "commit_sha": SHA_BUILD, "exit_code": 127, "duration_s": 0.1,
                    "output_digest": "f" * 64, "fault": NOT_EXECUTABLE,
                    "summary": "sh: cargo: command not found"})
    swarm.pump()

    assert swarm.sent("gate.requested") == []           # qa is never asked
    (escalation,) = swarm.sent("decision.requested")
    assert escalation.payload["subject_id"] == "I1.S1"
    assert swarm.state.stories["I1.S1"].escalated


def test_a_faulted_property_run_is_not_a_broken_invariant(client, publisher) -> None:
    from test_coordinator import _drive_behaviour_to_done

    swarm = MiniSwarm(client, publisher, policy=Policy(properties="story"))
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")

    props = [r for r in swarm.sent("run.requested")
             if r.payload["kind"] == "properties"][-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": props.payload["run_id"], "kind": "properties",
                    "commit_sha": SHA_BUILD, "exit_code": 127, "duration_s": 0.1,
                    "output_digest": "f" * 64, "fault": NOT_EXECUTABLE,
                    "summary": "sh: hypothesis: command not found"})
    swarm.pump()

    rework = swarm.sent("rework.requested")
    assert not any("invariant" in str(r.payload) for r in rework)
    assert swarm.state.stories["I1.S1"].escalated


# ── the toolchain: decided by the plan, carried on the work item ─────────────

def test_the_plan_decides_what_the_toolgate_runs(client, publisher) -> None:
    swarm = MiniSwarm(client, publisher, policy=Policy(plan_required=True))
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    publisher.send("planner", "coordinator", "plan.committed",
                   {"iteration_id": "I1", "plan_path": "docs/relay/plans/I1.md",
                    "summary": "a dependency-free Cargo binary crate",
                    "commit_sha": PLAN_SHA,
                    "commands": {"acceptance_test": "cargo test -q",
                                 "mutation": "cargo mutants"}},
                   iteration_id="I1")
    swarm.pump()
    assert swarm.state.iterations["I1"].commands["acceptance_test"] == "cargo test -q"

    _spec_written(swarm)
    red = swarm.sent("run.requested")[-1]
    assert red.payload["command"] == "cargo test -q"    # on the ledger, per run


def test_a_run_with_no_plan_command_carries_none(client, publisher) -> None:
    """Projects that configure the toolgate locally still work: the payload
    simply says nothing and the toolgate falls back to its own config."""
    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    assert "command" not in swarm.sent("run.requested")[-1].payload


def test_story_and_iteration_runs_use_the_plan_too(client, publisher) -> None:
    from test_coordinator import _drive_behaviour_to_done

    swarm = MiniSwarm(client, publisher, policy=MUTATION_POLICY)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    publisher.send("planner", "coordinator", "plan.committed",
                   {"iteration_id": "I1", "plan_path": "docs/relay/plans/I1.md",
                    "summary": "s", "commit_sha": PLAN_SHA,
                    "commands": {"mutation": "cargo mutants"}},
                   iteration_id="I1")
    swarm.pump()
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")

    mutation = [r for r in swarm.sent("run.requested")
                if r.payload["kind"] == "mutation"][-1]
    assert mutation.payload["command"] == "cargo mutants"


def test_no_command_configured_is_a_fault_not_a_red(client, publisher) -> None:
    """With the Python defaults gone, an unconfigured project must stop the
    swarm loudly instead of running somebody else's test runner."""
    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=127, fault=NO_COMMAND,
                       summary="no command configured for run kind 'acceptance_test'")

    assert swarm.behaviour("I1.S1.B1").state is BehaviourState.BLOCKED
    assert len(swarm.sent("decision.requested")) == 1


def test_a_timeout_is_a_fault_not_a_failing_test(client, publisher) -> None:
    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=124, fault=TIMEOUT,
                       summary="timed out after 900s")
    assert swarm.behaviour("I1.S1.B1").state is BehaviourState.BLOCKED
