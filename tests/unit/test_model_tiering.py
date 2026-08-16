"""One brain per kind of work, not one per role.

$258.99 for two iterations, of which the Interpreter was $110.94 on opus for
conversation and roadmap assembly, while the analyst — whose reading of the
problem shapes every story after it — ran six turns on sonnet for $6.95. Role
granularity could not express that, and it could not express the difference
between a specifier writing a fresh acceptance test and the same specifier
re-checking a green run.
"""

from pathlib import Path

from relay.runners.claude import ClaudeRunner
from relay.workers.run import ROLE_DEFAULT_MODELS, _runner_for, per_trigger_runners

CONFIG = {
    "roles": {
        "specifier": {
            "model": "sonnet",
            "effort": "medium",
            "triggers": {
                "judgement.requested": {"model": "haiku", "effort": "low"},
            },
        },
        "reviewer": {"model": "sonnet"},
    }
}


def test_a_trigger_may_have_its_own_brain(tmp_path: Path) -> None:
    runners = per_trigger_runners("specifier", CONFIG, tmp_path)
    judging = runners["judgement.requested"]
    assert isinstance(judging, ClaudeRunner)
    assert judging.model == "haiku"
    assert judging.effort == "low"


def test_the_override_patches_rather_than_replaces(tmp_path: Path) -> None:
    """A trigger that only changes the model keeps everything else the role
    configured — budget, permissions, runner."""
    config = {"roles": {"specifier": {"model": "sonnet", "max_budget_usd": 2.5,
                                      "triggers": {"judgement.requested": {"model": "haiku"}}}}}
    judging = per_trigger_runners("specifier", config, tmp_path)["judgement.requested"]
    assert isinstance(judging, ClaudeRunner)
    assert judging.model == "haiku"
    assert judging.max_budget_usd == 2.5


def test_work_without_an_override_uses_the_role_default(tmp_path: Path) -> None:
    runners = per_trigger_runners("specifier", CONFIG, tmp_path)
    assert "spec.requested" not in runners       # falls back to the role's runner
    role_runner = _runner_for("specifier", CONFIG, tmp_path)
    assert isinstance(role_runner, ClaudeRunner) and role_runner.model == "sonnet"


def test_a_role_without_triggers_has_none(tmp_path: Path) -> None:
    assert per_trigger_runners("reviewer", CONFIG, tmp_path) == {}


def test_the_analyst_thinks_hardest_and_the_gates_do_not(tmp_path: Path) -> None:
    """Opus where the reading of the problem shapes everything after it;
    sonnet on the gates, because a cheap gate defers a bug rather than saving
    money."""
    assert ROLE_DEFAULT_MODELS["analyst"] == "opus"
    for gate in ("reviewer", "qa", "security"):
        assert _runner_for(gate, {}, tmp_path).model == "sonnet"   # type: ignore[union-attr]
    assert _runner_for("interpreter", {}, tmp_path).model == "sonnet"  # type: ignore[union-attr]


def test_the_worker_picks_the_brain_that_matches_the_work(client, publisher, tmp_path) -> None:
    from relay.runners.base import TurnResult
    from relay.runners.fake import FakeRunner
    from relay.workers.chain import ChainWorker

    used: list[str] = []

    def brain(name: str) -> FakeRunner:
        def respond(prompt, session_ref):
            used.append(name)
            import re
            event_id = re.search(r"event_id: (\S+)", prompt).group(1)
            publisher.send("specifier", "coordinator", "error.raised",
                           {"kind": "other", "detail": "noted"}, in_reply_to=event_id)
            return TurnResult(ok=True, session_ref="s1")
        return FakeRunner(respond)

    worker = ChainWorker(
        "testswarm", "specifier", brain("role-default"),
        playbook_path=Path(__file__).resolve().parents[2] / "roles" / "specifier.md",
        workspace=tmp_path, state_dir=tmp_path / "s", client=client,
        runners={"judgement.requested": brain("cheap")},
    )
    worker.start()
    publisher.send("coordinator", "specifier", "spec.requested",
                   {"behaviour_id": "I1.S1.B1", "iteration_id": "I1", "ac_text": "…",
                    "kind": "ac", "base_sha": "b" * 40}, behaviour_id="I1.S1.B1")
    worker.step(block_ms=1)
    publisher.send("coordinator", "specifier", "judgement.requested",
                   {"behaviour_id": "I1.S1.B1", "commit_sha": "a" * 40,
                    "run_id": "run-01J5AB3CDEF4GH5JK6MN7PQ8RS"}, behaviour_id="I1.S1.B1")
    worker.step(block_ms=1)

    assert used == ["role-default", "cheap"]
