"""Drive the coordinator (projection + dispatcher) through full lifecycles by
playing the other roles by hand over the real publish path (fakeredis).
"""

from __future__ import annotations

import pytest

from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.coordinator.dispatcher import Dispatcher, GitHooks
from relay.coordinator.model import BehaviourState, SwarmState
from relay.coordinator.policy import GateSpec, Policy
from relay.coordinator.projection import apply

SHA_BASE = "a" * 40
SHA_SPEC = "b" * 40
SHA_BUILD = "c" * 40

ROADMAP = {
    "iterations": [{
        "id": "I1",
        "goal": "See free rooms",
        "increment": "A CLI that lists free rooms",
        "stories": [{
            "id": "I1.S1",
            "title": "List free rooms",
            "narrative": "As a member, I want free rooms listed, so I can grab one.",
            "acceptance_criteria": [
                {"id": "I1.S1.B1", "text": "Given a booked and a free room, only the free one is listed."},
            ],
        }],
    }],
}


class MiniSwarm:
    """A single-process stand-in for the coordinator's read-apply-react loop."""

    def __init__(self, client, publisher, policy=None, git=None):
        self.client = client
        self.publisher = publisher
        self.dispatcher = Dispatcher(
            publisher, policy or Policy(), git or GitHooks(
                ensure_branch=lambda _i: SHA_BASE,
                head_sha=lambda: SHA_BASE,
                has_history=lambda: False,
                create_pr=lambda _it: "https://github.com/acme/x/pull/1",
            )
        )
        self.state = SwarmState()
        self._applied = 0

    def pump(self) -> None:
        """Apply unseen ledger events and react, until fixpoint."""
        while True:
            entries = self.client.xrange(ledger_key("testswarm"))
            fresh = entries[self._applied:]
            self._applied = len(entries)
            for _sid, fields in fresh:
                apply(self.state, Envelope.from_fields(fields))
            if self.dispatcher.react(self.state) == 0 and self._applied == len(
                self.client.xrange(ledger_key("testswarm"))
            ):
                return

    def sent(self, type_: str) -> list[Envelope]:
        return [
            Envelope.from_fields(f)
            for _sid, f in self.client.xrange(ledger_key("testswarm"))
            if f["type"] == type_
        ]

    def behaviour(self, bid: str):
        return self.state.behaviours[bid]


@pytest.fixture
def swarm(client, publisher) -> MiniSwarm:
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def _drive_behaviour_to_done(swarm: MiniSwarm, bid: str) -> None:
    p = swarm.publisher
    p.send("specifier", "coordinator", "spec.written",
           {"behaviour_id": bid, "test_paths": [f"tests/acceptance/test_{bid.lower().replace('.', '_')}.py"],
            "commit_sha": SHA_SPEC, "touches": ["src/rooms/cli.py"]})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": red.payload["run_id"], "kind": "acceptance_test", "commit_sha": SHA_SPEC,
            "exit_code": 1, "duration_s": 1.0, "output_digest": "d" * 64})
    swarm.pump()
    story_id = swarm.behaviour(bid).story_id
    p.send("builder", "coordinator", "behaviour.built",
           {"behaviour_id": bid, "story_id": story_id, "iteration_id": "I1",
            "commit_sha": SHA_BUILD, "attempt": swarm.behaviour(bid).attempt})
    swarm.pump()
    green = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": green.payload["run_id"], "kind": "acceptance_test", "commit_sha": SHA_BUILD,
            "exit_code": 0, "duration_s": 1.0, "output_digest": "e" * 64})
    swarm.pump()
    judgement = swarm.sent("judgement.requested")[-1]
    p.send("specifier", "coordinator", "acceptance.judged",
           {"behaviour_id": bid, "verdict": "pass", "run_id": judgement.payload["run_id"]})
    swarm.pump()


def test_happy_path_through_iteration_ready(swarm: MiniSwarm) -> None:
    # roadmap + start -> spec dispatched for the first AC behaviour
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.SPEC_DISPATCHED
    # the INT behaviour was created by code, not by any model
    assert swarm.behaviour("I1.INT").kind == "integration"

    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.DONE
    # the story is not done until its OWN integration behaviour is: that is
    # what the Owner gets to try at the end of every story
    assert swarm.sent("story.completed") == []
    assert swarm.behaviour("I1.S1.INT").state == BehaviourState.SPEC_DISPATCHED

    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    assert len(swarm.sent("story.completed")) == 1
    assert swarm.behaviour("I1.INT").state == BehaviourState.SPEC_DISPATCHED

    _drive_behaviour_to_done(swarm, "I1.INT")
    assert len(swarm.sent("iteration.finished")) == 1
    assert swarm.sent("progress.reported")[-1].payload["behaviours_done"] == 3


def test_red_verification_failure_returns_to_specifier(swarm: MiniSwarm) -> None:
    swarm.publisher.send("specifier", "coordinator", "spec.written",
                         {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/t.py"],
                          "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    # the "failing" test passes -> red verification fails -> spec re-dispatched
    swarm.publisher.send("toolgate", "coordinator", "run.completed",
                         {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                          "commit_sha": SHA_SPEC, "exit_code": 0, "duration_s": 0.1,
                          "output_digest": "d" * 64})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.SPEC_DISPATCHED
    assert len(swarm.sent("spec.requested")) == 2
    assert len(swarm.sent("build.requested")) == 0  # build never dispatched on a green red


def test_at_failure_causes_rework_then_blocked_after_max_attempts(swarm: MiniSwarm) -> None:
    p = swarm.publisher
    p.send("specifier", "coordinator", "spec.written",
           {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/t.py"], "commit_sha": SHA_SPEC,
            "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": red.payload["run_id"], "kind": "acceptance_test", "commit_sha": SHA_SPEC,
            "exit_code": 1, "duration_s": 0.1, "output_digest": "d" * 64})
    swarm.pump()

    for expected_attempt in (2, 3):  # two failing build->AT rounds
        p.send("builder", "coordinator", "behaviour.built",
               {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
                "commit_sha": SHA_BUILD, "attempt": swarm.behaviour("I1.S1.B1").attempt})
        swarm.pump()
        at_run = swarm.sent("run.requested")[-1]
        p.send("toolgate", "coordinator", "run.completed",
               {"run_id": at_run.payload["run_id"], "kind": "acceptance_test",
                "commit_sha": SHA_BUILD, "exit_code": 1, "duration_s": 0.1,
                "output_digest": "e" * 64})
        swarm.pump()
        assert swarm.behaviour("I1.S1.B1").attempt == expected_attempt
        assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BUILD_DISPATCHED

    # third failure exceeds max_attempts=3 -> blocked + owner decision, not another silent loop
    p.send("builder", "coordinator", "behaviour.built",
           {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
            "commit_sha": SHA_BUILD, "attempt": 3})
    swarm.pump()
    at_run = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": at_run.payload["run_id"], "kind": "acceptance_test",
            "commit_sha": SHA_BUILD, "exit_code": 1, "duration_s": 0.1, "output_digest": "e" * 64})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED
    assert len(swarm.sent("decision.requested")) == 1


def test_replay_resume_never_double_dispatches(swarm: MiniSwarm, client, publisher) -> None:
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    before = {t: len(swarm.sent(t)) for t in
              ("spec.requested", "build.requested", "run.requested",
               "judgement.requested")}

    # cold start: fresh dispatcher + state, full replay of the same ledger
    fresh = MiniSwarm(client, publisher)
    fresh.pump()
    after = {t: len(fresh.sent(t)) for t in before}
    assert after == before  # replay produced no duplicate dispatch of any kind
    assert fresh.behaviour("I1.S1.B1").state == BehaviourState.DONE
    assert fresh.behaviour("I1.INT").state == BehaviourState.SPEC_DISPATCHED


def test_invalid_roadmap_rejected_in_code(client, publisher) -> None:
    mini = MiniSwarm(client, publisher)
    bad = {
        "iterations": [{
            "id": "I1", "goal": "g", "increment": "   ",  # no demonstrable increment
            "stories": [{
                "id": "I1.S1", "title": "t", "narrative": "n",
                "acceptance_criteria": [{"id": "I1.S1.B1", "text": "x"}],
            }],
        }],
    }
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": bad, "intake": {"mode": "greenfield"}})
    mini.pump()
    assert len(mini.sent("roadmap.rejected")) == 1
    assert len(mini.sent("spec.requested")) == 0
    assert mini.state.roadmap_committed is False


def test_story_completion_tells_the_owner_how_to_try_it(swarm: MiniSwarm) -> None:
    """The Owner checks at the end of every STORY, not only every iteration:
    a story is a vertical slice or it is not a story. The coordinator carries
    the builder's own commands through, so the Interpreter has something real
    to relay."""
    p = swarm.publisher
    p.send("specifier", "coordinator", "spec.written",
           {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/acceptance/test_b1.py"],
            "commit_sha": SHA_SPEC, "touches": ["src/rooms/cli.py"]})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": red.payload["run_id"], "kind": "acceptance_test", "commit_sha": SHA_SPEC,
            "exit_code": 1, "duration_s": 1.0, "output_digest": "d" * 64})
    swarm.pump()
    p.send("builder", "coordinator", "behaviour.built",
           {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
            "commit_sha": SHA_BUILD, "attempt": 1,
            "how_to_run": "uv run rooms free --now"})
    swarm.pump()
    green = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": green.payload["run_id"], "kind": "acceptance_test",
            "commit_sha": SHA_BUILD, "exit_code": 0, "duration_s": 1.0,
            "output_digest": "e" * 64})
    swarm.pump()
    judgement = swarm.sent("judgement.requested")[-1]
    p.send("specifier", "coordinator", "acceptance.judged",
           {"behaviour_id": "I1.S1.B1", "verdict": "pass",
            "run_id": judgement.payload["run_id"]})
    swarm.pump()

    # the story's own integration behaviour closes it, and its builder's
    # commands are what the Owner is handed
    _drive_behaviour_to_done(swarm, "I1.S1.INT")

    (completed,) = swarm.sent("story.completed")
    assert completed.payload["story_id"] == "I1.S1"
    assert completed.payload["how_to_try"] == "uv run rooms free --now"


def test_every_story_ends_with_its_own_integration_behaviour(swarm: MiniSwarm) -> None:
    """A story is a vertical slice or it is not a story, so the Owner gets
    something to try at the end of each one — not only at the end of the
    iteration."""
    story = swarm.state.stories["I1.S1"]
    assert story.int_behaviour_id == "I1.S1.INT"
    assert story.behaviour_ids[-1] == "I1.S1.INT", "the story's INT comes last"
    behaviour = swarm.state.behaviours["I1.S1.INT"]
    assert behaviour.kind == "integration"
    assert behaviour.story_id == "I1.S1"
    # the iteration keeps its own, which proves the stories work together
    assert swarm.state.iterations["I1"].int_behaviour_id == "I1.INT"


def test_an_integration_behaviour_is_never_listed_twice(swarm: MiniSwarm) -> None:
    """The duplicate rows on the board: one dict entry, two order entries."""
    order = swarm.state.behaviour_order
    assert len(order) == len(set(order))


def test_a_roadmap_that_writes_its_own_integration_behaviour_is_rejected(client, publisher) -> None:
    import copy
    roadmap = copy.deepcopy(ROADMAP)
    roadmap["iterations"][0]["stories"][0]["acceptance_criteria"].append(
        {"id": "I1.INT", "text": "Open the game and play it end to end."}
    )
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": roadmap, "intake": {"mode": "greenfield"}})
    mini.pump()
    (rejected,) = mini.sent("roadmap.rejected")
    assert any("integration behaviours are created by the coordinator" in r
               for r in rejected.payload["reasons"])


def test_a_model_written_integration_criterion_never_reaches_the_board(client, publisher) -> None:
    """Belt to the validation's braces: even if one slips through, it must not
    produce a second row for the same id."""
    import copy
    roadmap = copy.deepcopy(ROADMAP)
    roadmap["iterations"][0]["stories"][0]["acceptance_criteria"].append(
        {"id": "I1.INT", "text": "Open the game and play it end to end."}
    )
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": roadmap, "intake": {"mode": "greenfield"}})
    mini.pump()
    order = mini.state.behaviour_order
    assert order.count("I1.INT") == 1
    assert "I1.INT" not in mini.state.stories["I1.S1"].behaviour_ids


def test_test_design_rework_leaves_the_behaviour_awaiting_the_specifier(
    client, publisher
) -> None:
    """A test_design gate failure is the specifier's to fix, so the behaviour
    must wait in SPEC_DISPATCHED. Parking it in BUILD_DISPATCHED makes the
    specifier's answering spec.written unmatchable, and the behaviour then
    waits forever for a build nobody asked for.
    """
    policy = Policy(per_behaviour=(
        GateSpec(gate="code_review", role="reviewer"),
        GateSpec(gate="test_design", role="qa"),
    ))
    swarm = MiniSwarm(client, publisher, policy=policy)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()

    # spec -> red -> build -> green, at which point the behaviour gates run
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/t.py"],
                    "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 0.1,
                    "output_digest": "d" * 64})
    swarm.pump()
    publisher.send("builder", "coordinator", "behaviour.built",
                   {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
                    "commit_sha": SHA_BUILD, "attempt": swarm.behaviour("I1.S1.B1").attempt})
    swarm.pump()
    green = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": green.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_BUILD, "exit_code": 0, "duration_s": 0.1,
                    "output_digest": "e" * 64})
    swarm.pump()

    gates = {g.payload["gate"]: g for g in swarm.sent("gate.requested")}
    publisher.send("reviewer", "coordinator", "gate.judged",
                   {"gate_id": gates["code_review"].payload["gate_id"],
                    "verdict": "pass", "findings": []})
    publisher.send("qa", "coordinator", "gate.judged",
                   {"gate_id": gates["test_design"].payload["gate_id"], "verdict": "fail",
                    "findings": [{"title": "one test asserts three things",
                                  "detail": "split the bundled assertions",
                                  "severity": "major", "source": "qa"}]})
    swarm.pump()

    rework = swarm.sent("rework.requested")[-1]
    assert rework.to_role == "specifier"
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.SPEC_DISPATCHED

    # and the specifier's answer must actually land, not be dropped by a guard
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/t.py"],
                    "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state != BehaviourState.SPEC_DISPATCHED
