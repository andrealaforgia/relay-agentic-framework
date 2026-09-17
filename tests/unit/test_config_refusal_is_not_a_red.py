"""A command that refused to run is not a failing test.

A runner that selects tests by behaviour can be handed a selection it cannot
honour — an identity it does not recognise, a behaviour that owns no check.
It then runs NOTHING and says so, exiting 78 (the sysexits convention for a
configuration error) with its diagnostic on the first line.

Read as a test result, that refusal is indistinguishable from a red: it
satisfies "prove the test fails first", sends a builder to write code against
a test nobody ever saw fail, then blocks the behaviour with "acceptance test
still failing after build" — and burns one of three attempts each time round.
No number of retries makes a misconfigured selection select something.

This is the same lesson as `not_executable`, one layer up: there, the command
could not start; here, it started, understood the request, and declined it.
"""

from __future__ import annotations

from test_coordinator import ROADMAP, SHA_BUILD, SHA_SPEC, MiniSwarm

from relay.coordinator.model import BehaviourState
from relay.workers.faults import CONFIG_REFUSED, classify

# What the project's runner actually prints when the selection is at fault.
SELECTION_REFUSAL = (
    "ACCEPTANCE-SELECTION-ERROR: behaviour I3.S10.B3 owns no check and no "
    "scenario, so a run would report success having run nothing\n"
    "command: acceptance-run I3.S10.B3\n"
    "revision: 76d89fbf2bd5b2b13ff6ef33ed43eb78be354f97\n"
    "selection: none resolved"
)


# ── the classifier ───────────────────────────────────────────────────────────

def test_a_configuration_refusal_is_not_evidence_about_the_code() -> None:
    assert classify(78, SELECTION_REFUSAL) == CONFIG_REFUSED


def test_an_assertion_failure_is_still_evidence() -> None:
    """The conservative half: calling a real failure a fault stalls a swarm
    that should be doing rework."""
    assert classify(1, "--- FAIL: TestThing (0.00s)\nFAIL\texit status 1") is None
    assert classify(2, "# prova-lang/compiler\n./x.go:3:2: undefined: y") is None
    assert classify(0, "ok\tprova-lang/compiler\t0.2s") is None


# ── the coordinator refuses to reason from it ────────────────────────────────

def _spec_written(swarm: MiniSwarm, bid: str = "I1.S1.B1") -> None:
    swarm.publisher.send("specifier", "coordinator", "spec.written",
                         {"behaviour_id": bid,
                          "test_paths": ["tests/acceptance/test_b1.py"],
                          "commit_sha": SHA_SPEC, "touches": ["src/rooms/cli.py"]})
    swarm.pump()


def _complete_last_run(swarm: MiniSwarm, sha: str, **extra: object) -> None:
    run = swarm.sent("run.requested")[-1]
    payload = {"run_id": run.payload["run_id"], "kind": "acceptance_test",
               "commit_sha": sha, "duration_s": 0.2, "output_digest": "f" * 64}
    payload.update(extra)
    swarm.publisher.send("toolgate", "coordinator", "run.completed", payload)
    swarm.pump()


def _started(swarm: MiniSwarm) -> MiniSwarm:
    swarm.publisher.send("interpreter", "coordinator", "roadmap.committed",
                         {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    swarm.publisher.send("interpreter", "coordinator", "iteration.started",
                         {"iteration_id": "I1"})
    swarm.pump()
    return swarm


def test_a_refused_selection_never_counts_as_a_verified_red(client, publisher) -> None:
    swarm = _started(MiniSwarm(client, publisher))
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=78, summary=SELECTION_REFUSAL,
                       fault=CONFIG_REFUSED)

    b = swarm.behaviour("I1.S1.B1")
    assert b.state is not BehaviourState.RED_VERIFIED   # nothing was proved
    assert b.state is BehaviourState.BLOCKED            # and the swarm stops
    assert swarm.sent("build.requested") == []          # no build on a phantom red


def test_a_refused_selection_consumes_no_attempt(client, publisher) -> None:
    """Three attempts against a selection that cannot select is three wasted
    builder turns and a behaviour blocked for the wrong reason."""
    swarm = _started(MiniSwarm(client, publisher))
    _spec_written(swarm)
    before = swarm.behaviour("I1.S1.B1").attempt
    _complete_last_run(swarm, SHA_SPEC, exit_code=78, summary=SELECTION_REFUSAL,
                       fault=CONFIG_REFUSED)
    assert swarm.behaviour("I1.S1.B1").attempt == before


def test_a_refused_selection_asks_the_owner_once(client, publisher) -> None:
    swarm = _started(MiniSwarm(client, publisher))
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=78, summary=SELECTION_REFUSAL,
                       fault=CONFIG_REFUSED)
    swarm.pump()
    swarm.pump()
    (escalation,) = swarm.sent("decision.requested")
    assert escalation.payload["subject_id"] == "I1.S1.B1"
    assert "did not run" in escalation.payload["reason"]


def test_a_refused_selection_after_build_is_not_a_failing_test(client, publisher) -> None:
    """The sentence the Owner kept reading — "acceptance test still failing
    after build" — about a test that never ran."""
    swarm = _started(MiniSwarm(client, publisher))
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=1, summary="1 failed")  # a real red
    publisher.send("builder", "coordinator", "behaviour.built",
                   {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1",
                    "iteration_id": "I1", "commit_sha": SHA_BUILD, "attempt": 1})
    swarm.pump()
    _complete_last_run(swarm, SHA_BUILD, exit_code=78, summary=SELECTION_REFUSAL,
                       fault=CONFIG_REFUSED)

    b = swarm.behaviour("I1.S1.B1")
    assert b.last_fail_reason != "acceptance test still failing after build"
    assert b.state is BehaviourState.BLOCKED
    assert swarm.sent("rework.requested") == []      # never rework on a phantom red


def test_the_owner_retry_clears_it_and_re_runs_the_cycle(client, publisher) -> None:
    from relay.coordinator.policy import Policy

    swarm = _started(MiniSwarm(client, publisher, policy=Policy(wip_limit=3)))
    _spec_written(swarm)
    _complete_last_run(swarm, SHA_SPEC, exit_code=78, summary=SELECTION_REFUSAL,
                       fault=CONFIG_REFUSED)
    escalation = swarm.sent("decision.requested")[-1]

    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": escalation.payload["gate_id"],
                    "subject_id": "I1.S1.B1", "decision": "retry"})
    swarm.pump()

    b = swarm.behaviour("I1.S1.B1")
    assert b.infra_fault is None                        # the fault does not survive
    assert b.state is BehaviourState.SPEC_DISPATCHED    # the whole cycle re-runs
