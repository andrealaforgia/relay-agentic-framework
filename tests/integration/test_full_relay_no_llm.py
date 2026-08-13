"""The framework's own acceptance test: a complete engagement — problem ->
Q&A -> roadmap -> approval -> spec -> red -> build -> AT green -> acceptance
-> story done -> INT behaviour -> iteration ready -> checkpoint — with ZERO
model calls. FakeRunners play the models, doing real git work in a real repo;
the toolgate really runs pytest; every message rides the real validated bus.

Then: chaos — a worker crashes mid-turn and a cold-restarted swarm finishes
the engagement exactly, without redoing completed work.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from relay.bus.keys import ledger_key
from relay.bus.publisher import Publisher
from relay.contract.envelope import Envelope
from relay.coordinator.main import Coordinator
from relay.coordinator.model import BehaviourState
from relay.ledger.audit import audit_ledger
from relay.runners.base import TurnResult
from relay.runners.fake import FakeRunner
from relay.workers.chain import ChainWorker
from relay.workers.toolgate import Toolgate

PYTEST_CMD = f"{sys.executable} -m pytest -q {{test_paths}}"
GATE_ID = "gate-01J5AB3CDEF4GH5JK6MN7PQ8RS"
QID = "q-01J5AB3CDEF4GH5JK6MN7PQ8RS"

ROADMAP = {
    "iterations": [{
        "id": "I1", "goal": "See free rooms", "increment": "a rooms CLI listing free rooms",
        "stories": [{
            "id": "I1.S1", "title": "List free rooms", "narrative": "As a member...",
            "acceptance_criteria": [{"id": "I1.S1.B1", "text": "free rooms are listed"}],
        }],
    }],
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _trigger(prompt: str) -> tuple[str, str, dict]:
    """Extract (event_id, type, payload) of the triggering message from the prompt."""
    event_id = re.search(r"event_id: (\S+)", prompt).group(1)  # type: ignore[union-attr]
    type_ = re.search(r"type: (\S+)", prompt).group(1)  # type: ignore[union-attr]
    payload, _end = json.JSONDecoder().raw_decode(prompt.split("payload:\n", 1)[1])
    return event_id, type_, payload


class Fakes:
    """The four scripted 'models'. Each publishes through the real relay-send
    path (Publisher) and does real git work where the role demands it."""

    def __init__(self, publisher: Publisher, project: Path) -> None:
        self.pub = publisher
        self.project = project

    # -- interpreter ----------------------------------------------------------
    def interpreter(self, prompt: str, _s: str | None) -> str:
        event_id, type_, payload = _trigger(prompt)
        if type_ == "chat.problem":
            self.pub.send("interpreter", "analyst", "work.analysis_requested",
                          {"problem": payload["text"]}, in_reply_to=event_id)
        elif type_ == "work.question_raised":
            self.pub.send("interpreter", "owner", "chat.question",
                          {"question_id": payload["question_id"],
                           "questions": [{"text": q} for q in payload["questions"]]},
                          in_reply_to=event_id)
        elif type_ == "chat.answer":
            self.pub.send("interpreter", "analyst", "work.answers",
                          {"question_id": payload["question_id"], "answers": payload["answers"]},
                          in_reply_to=event_id)
        elif type_ == "work.stories_ready":
            self.pub.send("interpreter", "owner", "chat.roadmap_proposed",
                          {"roadmap": ROADMAP, "narrative": "One iteration.", "gate_id": GATE_ID},
                          in_reply_to=event_id)
        elif type_ == "chat.decision" and payload["gate_id"] == GATE_ID:
            self.pub.send("interpreter", "coordinator", "plan.roadmap_committed",
                          {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}},
                          in_reply_to=event_id)
            self.pub.send("interpreter", "coordinator", "plan.iteration_started",
                          {"iteration_id": "I1"})
        elif type_ == "plan.story_done":
            self.pub.send("interpreter", "owner", "chat.result",
                          {"text": f"story {payload['story_id']} delivered"}, in_reply_to=event_id)
        elif type_ == "plan.iteration_ready":
            self.pub.send("interpreter", "owner", "chat.checkpoint",
                          {"kind": "iteration", "subject_id": payload["iteration_id"],
                           "gate_id": GATE_ID, "summary": payload["summary"]},
                          in_reply_to=event_id)
        else:  # anything unscripted still gets an on-contract reply
            self.pub.send("interpreter", "owner", "chat.result",
                          {"text": f"noted: {type_}"}, in_reply_to=event_id)
        return "ok"

    # -- analyst --------------------------------------------------------------
    def analyst(self, prompt: str, _s: str | None) -> str:
        event_id, type_, payload = _trigger(prompt)
        if type_ == "work.analysis_requested":
            self.pub.send("analyst", "interpreter", "work.question_raised",
                          {"question_id": QID, "questions": ["Which calendar system?"]},
                          in_reply_to=event_id)
        elif type_ == "work.answers":
            self.pub.send("analyst", "interpreter", "work.stories_ready",
                          {"stories": [{"title": "List free rooms", "narrative": "As a member...",
                                        "acceptance_criteria": ["free rooms are listed"],
                                        "priority": 1}]},
                          in_reply_to=event_id)
        return "ok"

    # -- specifier ------------------------------------------------------------
    def specifier(self, prompt: str, _s: str | None) -> str:
        event_id, type_, payload = _trigger(prompt)
        if type_ == "work.spec_requested":
            bid = payload["behaviour_id"]
            test_file = self.project / "tests" / "acceptance" / f"test_{bid.lower().replace('.', '_')}.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            if payload["kind"] == "integration":
                body = "import rooms\n\ndef test_increment_runs_end_to_end():\n    assert rooms.main() == 0\n"
            else:
                body = "import rooms\n\ndef test_free_rooms_listed():\n    assert rooms.free() == ['R2']\n"
            test_file.write_text(body)
            _git(self.project, "add", "-A")
            _git(self.project, "commit", "-qm", f"[{bid}] acceptance test")
            self.pub.send("specifier", "coordinator", "work.spec_ready",
                          {"behaviour_id": bid,
                           "test_paths": [str(test_file.relative_to(self.project))],
                           "commit_sha": _git(self.project, "rev-parse", "HEAD"),
                           "touches": ["rooms.py"]},
                          in_reply_to=event_id, behaviour_id=bid)
        elif type_ == "work.judgement_requested":
            self.pub.send("specifier", "coordinator", "work.acceptance_verdict",
                          {"behaviour_id": payload["behaviour_id"], "verdict": "pass",
                           "run_id": payload["run_id"], "reason": "AT green on the real surface"},
                          in_reply_to=event_id, behaviour_id=payload["behaviour_id"])
        return "ok"

    # -- builder --------------------------------------------------------------
    def builder(self, prompt: str, _s: str | None) -> str:
        event_id, type_, payload = _trigger(prompt)
        if type_ in ("work.build_requested", "work.rework_requested"):
            bid = payload["behaviour_id"]
            rooms = self.project / "rooms.py"
            existing = rooms.read_text() if rooms.exists() else ""
            if bid.endswith(".INT") and "def main" not in existing:
                rooms.write_text(existing + "\ndef main():\n    print(free())\n    return 0\n")
            elif not bid.endswith(".INT") and "def free" not in existing:
                rooms.write_text("def free():\n    return ['R2']\n")
            _git(self.project, "add", "-A")
            _git(self.project, "commit", "-qm", f"[{bid}] implement", "--allow-empty")
            self.pub.send("builder", "coordinator", "work.built",
                          {"behaviour_id": bid,
                           "story_id": None if bid.endswith(".INT") else "I1.S1",
                           "iteration_id": "I1",
                           "commit_sha": _git(self.project, "rev-parse", "HEAD"),
                           "attempt": payload.get("attempt", 1),
                           "summary": "free rooms are listed"},
                          in_reply_to=event_id, behaviour_id=bid)
        return "ok"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "config", "user.email", "relay@test")
    _git(proj, "config", "user.name", "relay")
    (proj / "README.md").write_text("fixture\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    return proj


def _swarm(client, publisher, project, tmp_path, crash_specifier_once=False):
    fakes = Fakes(publisher, project)
    roles_dir = Path(__file__).resolve().parents[2] / "roles"

    crashes = {"left": 1 if crash_specifier_once else 0}

    def specifier_respond(prompt: str, s: str | None):
        if crashes["left"]:
            crashes["left"] -= 1
            return TurnResult(ok=False, error="scripted mid-turn crash", session_ref=s)
        return fakes.specifier(prompt, s)

    def worker(role: str, respond) -> ChainWorker:
        return ChainWorker(
            "testswarm", role, FakeRunner(respond),
            playbook_path=roles_dir / f"{role}.md",
            workspace=project, state_dir=tmp_path / "state" / role, client=client,
        )

    return {
        "coordinator": Coordinator("testswarm", project, client=client),
        "toolgate": Toolgate("testswarm", project,
                             commands={"acceptance_test": PYTEST_CMD}, client=client),
        "interpreter": worker("interpreter", fakes.interpreter),
        "analyst": worker("analyst", fakes.analyst),
        "specifier": worker("specifier", specifier_respond),
        "builder": worker("builder", fakes.builder),
    }


def _pump(swarm: dict, rounds: int = 40) -> None:
    swarm["coordinator"].bootstrap()
    for w in swarm.values():
        if isinstance(w, (ChainWorker, Toolgate)):
            w.start()
    for _ in range(rounds):
        moved = swarm["coordinator"].step(block_ms=1)
        for name, w in swarm.items():
            if name != "coordinator":
                moved += w.step(block_ms=1)
        if not moved:
            return


def _types(client) -> list[str]:
    return [f["type"] for _s, f in client.xrange(ledger_key("testswarm"))]


def _owner_kickoff(publisher) -> None:
    publisher.send("owner", "interpreter", "chat.problem", {"text": "finding a free room is slow"})


def test_full_engagement_no_llm(client, publisher, project, tmp_path, validator) -> None:
    swarm = _swarm(client, publisher, project, tmp_path)
    _owner_kickoff(publisher)
    _pump(swarm)
    # Q&A round: the analyst asked, the interpreter relayed — answer as the owner
    publisher.send("owner", "interpreter", "chat.answer",
                   {"question_id": QID, "answers": ["Google Calendar"]})
    _pump(swarm)
    # roadmap proposed — approve as the owner
    publisher.send("owner", "interpreter", "chat.decision",
                   {"gate_id": GATE_ID, "decision": "approve"})
    _pump(swarm)

    state = swarm["coordinator"].state
    assert state.behaviours["I1.S1.B1"].state == BehaviourState.DONE
    assert state.behaviours["I1.INT"].state == BehaviourState.DONE
    types = _types(client)
    assert "plan.story_done" in types
    assert "plan.iteration_ready" in types
    assert "chat.checkpoint" in types
    # red really ran red and green really ran green, via real pytest
    completions = [Envelope.from_fields(f) for _s, f in client.xrange(ledger_key("testswarm"))
                   if f["type"] == "run.completed"]
    exit_codes = [c.payload["exit_code"] for c in completions]
    reds = [c for c in exit_codes if c != 0]
    greens = [c for c in exit_codes if c == 0]
    assert len(reds) >= 2 and len(greens) >= 2  # a real red and a real green per behaviour
    # the whole ledger stands up to the audit
    report = audit_ledger(client, validator, "testswarm")
    assert report.ok, [f"{f.rule}: {f.detail}" for f in report.findings]
    # work landed on the iteration branch
    assert _git(project, "rev-parse", "--abbrev-ref", "HEAD") == "relay/testswarm/i1"
    assert (project / "rooms.py").exists()


def test_crash_and_cold_restart_finishes_exactly(client, publisher, project, tmp_path, validator) -> None:
    # first life: the specifier's model crashes on its first turn
    first = _swarm(client, publisher, project, tmp_path, crash_specifier_once=True)
    _owner_kickoff(publisher)
    _pump(first, rounds=6)
    publisher.send("owner", "interpreter", "chat.answer",
                   {"question_id": QID, "answers": ["Google Calendar"]})
    _pump(first, rounds=6)
    publisher.send("owner", "interpreter", "chat.decision",
                   {"gate_id": GATE_ID, "decision": "approve"})
    _pump(first, rounds=3)  # short life: the swarm 'dies' mid-engagement

    # second life: entirely new processes, same ledger, same project
    second = _swarm(client, publisher, project, tmp_path)
    _pump(second)

    state = second["coordinator"].state
    assert state.behaviours["I1.S1.B1"].state == BehaviourState.DONE
    assert state.behaviours["I1.INT"].state == BehaviourState.DONE
    # exactly one spec dispatch per behaviour: the restart redid nothing done
    spec_requests = [f for _s, f in client.xrange(ledger_key("testswarm"))
                     if f["type"] == "work.spec_requested"]
    by_behaviour: dict[str, int] = {}
    for f in spec_requests:
        bid = json.loads(f["payload"])["behaviour_id"]
        by_behaviour[bid] = by_behaviour.get(bid, 0) + 1
    assert by_behaviour == {"I1.S1.B1": 1, "I1.INT": 1}
    assert audit_ledger(client, validator, "testswarm").ok
