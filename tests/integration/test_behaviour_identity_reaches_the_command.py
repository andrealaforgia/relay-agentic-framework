"""The work item's behaviour identity must reach the command that runs it.

The coordinator has always put `behaviour_id` on the acceptance run beside
`test_paths`; the toolgate substituted only `{test_paths}` and dropped the
identity one line later. A project whose runner selects tests BY BEHAVIOUR
therefore received the literal string `{behaviour_id}` and had to guess the
work from the paths instead — impossible whenever several behaviours share one
file. Measured on a real iteration: of 37 parked behaviours, 10 resolved from
paths alone and 26 refused; with the identity supplied, 36 of 37 resolved.

The cases here are real blocked work items, not invented ones: a shared
feature file carrying five behaviours, a characterization check, and an
iteration integration check.

An identity that is missing, malformed, or contradicted by the envelope is a
configuration fault: the command MUST NOT run, because a command run against
the wrong identity produces something indistinguishable from evidence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.workers.toolgate import Toolgate

# Real work items from the iteration this defect stalled.
SHARED_FEATURE = "features/I3-S10-event-loop-budget-buildtime-only.feature"
BEHAVIOUR_ON_SHARED_FILE = "I3.S10.B3"      # one of five behaviours in that file
CHARACTERIZATION = "I3.S10.CHAR1"
ITERATION_INTEGRATION = "I3.INT"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "config", "user.email", "relay@test")
    _git(proj, "config", "user.name", "relay")
    # the shared feature file: five behaviours, one path — no path names the work
    feature = proj / SHARED_FEATURE
    feature.parent.mkdir(parents=True)
    feature.write_text(
        "Feature: event loop budget\n"
        + "".join(f"  @I3.S10.B{n}\n  Scenario: b{n}\n    Given nothing\n"
                 for n in range(1, 6))
    )
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    return proj


def _completions(client) -> list[Envelope]:
    return [
        Envelope.from_fields(f)
        for _sid, f in client.xrange(ledger_key("testswarm"))
        if f["type"] == "run.completed"
    ]


def _request(publisher, run_id: str, sha: str, command: str,
             *, behaviour_id: str | None = None,
             envelope_behaviour_id: str | None = "__same__",
             test_paths: list[str] | None = None) -> None:
    payload: dict[str, object] = {
        "run_id": run_id, "kind": "acceptance_test",
        "commit_sha": sha, "command": command,
    }
    if behaviour_id is not None:
        payload["behaviour_id"] = behaviour_id
    if test_paths is not None:
        payload["test_paths"] = test_paths
    envelope_id = (behaviour_id if envelope_behaviour_id == "__same__"
                   else envelope_behaviour_id)
    publisher.send("coordinator", "toolgate", "run.requested", payload,
                   behaviour_id=envelope_id, commit_sha=sha)


def _run_one(client, project: Path, publisher, **kwargs) -> Envelope:
    sha = _git(project, "rev-parse", "HEAD")
    gate = Toolgate("testswarm", project, client=client)
    _request(publisher, kwargs.pop("run_id"), sha, **kwargs)
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    return completion


# ── the identity travels ─────────────────────────────────────────────────────

@pytest.mark.parametrize("behaviour_id", [
    BEHAVIOUR_ON_SHARED_FILE,   # a B behaviour sharing a file with four others
    CHARACTERIZATION,           # a characterization check
    ITERATION_INTEGRATION,      # an iteration integration check
])
def test_the_command_receives_the_behaviour_identity(
    client, publisher, project: Path, behaviour_id: str
) -> None:
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R1",
        command="echo identity=[{behaviour_id}]",
        behaviour_id=behaviour_id,
    )
    assert completion.payload["exit_code"] == 0
    assert f"identity=[{behaviour_id}]" in completion.payload["summary"]


def test_a_shared_file_still_names_the_one_behaviour_being_run(
    client, publisher, project: Path
) -> None:
    """The case that stalled 26 behaviours: five of them share this path, so
    only the identity can say which one this run is about."""
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R2",
        command="echo id={behaviour_id} paths={test_paths}",
        behaviour_id=BEHAVIOUR_ON_SHARED_FILE,
        test_paths=[SHARED_FEATURE],
    )
    summary = completion.payload["summary"]
    assert f"id={BEHAVIOUR_ON_SHARED_FILE}" in summary
    assert SHARED_FEATURE in summary            # the paths still arrive too


def test_a_command_that_never_asks_for_the_identity_is_untouched(
    client, publisher, project: Path
) -> None:
    """Projects that select by path keep working exactly as before."""
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R3",
        command="echo no-placeholder-here",
        behaviour_id=BEHAVIOUR_ON_SHARED_FILE,
    )
    assert completion.payload["exit_code"] == 0
    assert "no-placeholder-here" in completion.payload["summary"]
    assert "fault" not in completion.payload


# ── an identity that cannot be trusted stops the run before it starts ────────

def test_a_missing_identity_refuses_instead_of_running(
    client, publisher, project: Path
) -> None:
    """The command asks for an identity the work item does not carry. Running
    it anyway selects whatever an empty id happens to match, and reports that
    as the behaviour's result."""
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R4",
        command="echo the-command-ran {behaviour_id}",
        behaviour_id=None,
    )
    assert completion.payload["fault"] == "config_refused"
    assert "the-command-ran" not in completion.payload["summary"]


def _handle_directly(client, validator, project: Path, run_id: str,
                     command: str, behaviour_id: str) -> Envelope:
    """Hand the worker an envelope the bus itself would never carry.

    The contract is the FIRST guard here: `behaviour_id` is a BehaviourId, so
    a malformed one cannot be published at all — which is why this arrives by
    the only route such a message could. The toolgate is the second guard, and
    it is the one that hands strings to a shell, so it checks rather than
    trusts.
    """
    sha = _git(project, "rev-parse", "HEAD")
    gate = Toolgate("testswarm", project, client=client)
    env = Envelope.model_validate({
        "swarm": "testswarm",
        "plane": validator.plane_of("run.requested"),
        "from": "coordinator", "to": "toolgate", "type": "run.requested",
        "commit_sha": sha,
        "contract_hash": validator.contract.contract_hash,
        "payload": {"run_id": run_id, "kind": "acceptance_test",
                    "commit_sha": sha, "command": command,
                    "behaviour_id": behaviour_id},
    })
    gate.handle(env)
    (completion,) = _completions(client)
    return completion


def test_a_malformed_identity_refuses_instead_of_running(
    client, validator, project: Path
) -> None:
    completion = _handle_directly(
        client, validator, project, "run-01J5AB3CDEF4GH5JK6MN7PQ8R5",
        "echo the-command-ran {behaviour_id}", "not-a-behaviour-id")
    assert completion.payload["fault"] == "config_refused"
    assert "the-command-ran" not in completion.payload["summary"]


def test_an_identity_the_envelope_contradicts_refuses_instead_of_running(
    client, publisher, project: Path
) -> None:
    """The envelope routes the result back to a behaviour; the payload tells
    the command which behaviour to run. If they disagree, one of them receives
    another behaviour's evidence."""
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R6",
        command="echo the-command-ran {behaviour_id}",
        behaviour_id=BEHAVIOUR_ON_SHARED_FILE,
        envelope_behaviour_id=CHARACTERIZATION,
    )
    assert completion.payload["fault"] == "config_refused"
    assert "the-command-ran" not in completion.payload["summary"]


def test_the_refusal_says_which_identity_it_objected_to(
    client, validator, project: Path
) -> None:
    """A fault nobody can diagnose costs the afternoon the fault vocabulary
    exists to save."""
    completion = _handle_directly(
        client, validator, project, "run-01J5AB3CDEF4GH5JK6MN7PQ8R7",
        "echo {behaviour_id}", "not-a-behaviour-id")
    assert "not-a-behaviour-id" in completion.payload["summary"]


# ── a command that refuses on configuration grounds is not a failing test ────

def test_a_command_refusing_with_exit_78_is_a_fault_not_a_red(
    client, publisher, project: Path
) -> None:
    """The project's runner exits 78 to say "I ran nothing; my selection is at
    fault". Read as a test result it satisfies red-verification, triggers
    rework and burns an attempt — none of which any retry can fix."""
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R8",
        command=("echo 'ACCEPTANCE-SELECTION-ERROR: behaviour owns no check'; "
                 "exit 78"),
        behaviour_id=BEHAVIOUR_ON_SHARED_FILE,
    )
    assert completion.payload["exit_code"] == 78
    assert completion.payload["fault"] == "config_refused"


def test_the_refusals_own_diagnostic_survives_into_the_result(
    client, publisher, project: Path
) -> None:
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8R9",
        command=("echo 'ACCEPTANCE-SELECTION-ERROR: behaviour owns no check'; "
                 "exit 78"),
        behaviour_id=BEHAVIOUR_ON_SHARED_FILE,
    )
    assert "ACCEPTANCE-SELECTION-ERROR" in completion.payload["summary"]
    assert "owns no check" in completion.payload["summary"]


def test_an_ordinary_failing_test_is_still_evidence(
    client, publisher, project: Path
) -> None:
    """The conservative half: only 78 means "nothing ran". A normal red keeps
    flowing through as a real result."""
    completion = _run_one(
        client, project, publisher,
        run_id="run-01J5AB3CDEF4GH5JK6MN7PQ8RA",
        command="echo '--- FAIL: TestThing'; exit 1",
        behaviour_id=BEHAVIOUR_ON_SHARED_FILE,
    )
    assert completion.payload["exit_code"] == 1
    assert "fault" not in completion.payload
