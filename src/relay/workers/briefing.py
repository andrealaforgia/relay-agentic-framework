"""What deterministic code already knows, handed to the model up front.

The framework's most expensive habit is rediscovery. A gate turn is given two
SHAs and spends its first loops reconstructing the diff by hand; a build turn
is given test paths and spends its first loops reading them; every turn
re-derives what the reconnaissance iteration already wrote down. Each of those
loops re-sends the entire accumulated context, so the cost of a turn grows
quadratically with how much it had to go and find out.

None of it needs a model. The coordinator has the SHAs, the payload has the
paths, and the brief is a file on disk. So we fetch them here, in code, and
put them in the prompt — the same principle as D1, applied to context rather
than control flow.

Everything is bounded and truncation is announced: a briefing that silently
swallowed half a diff would be worse than none at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DIFF_BUDGET = 40_000        # characters — a behaviour-sized diff, not a repo dump
TEST_BUDGET = 20_000
BRIEF_BUDGET = 6_000
KNOWLEDGE_BUDGET = 8_000
PLAN_BUDGET = 8_000

# Curated knowledge (docs/relay/knowledge/, written with the humans in
# `relay learn`), sliced per role: each assistant gets the files that change
# what it would do, not the whole library.
KNOWLEDGE_DIR = Path("docs") / "relay" / "knowledge"
PLANS_DIR = Path("docs") / "relay" / "plans"
ROLE_KNOWLEDGE: dict[str, tuple[str, ...]] = {
    "interpreter": ("domain.md", "brief.md"),
    "analyst": ("brief.md", "domain.md", "risk-map.md"),
    "planner": ("brief.md", "invariants.md", "risk-map.md", "conventions.md"),
    "specifier": ("invariants.md", "conventions.md", "domain.md"),
    "builder": ("conventions.md", "invariants.md", "brief.md"),
    "reviewer": ("invariants.md", "conventions.md"),
    "qa": ("conventions.md",),
    "security": ("risk-map.md", "brief.md"),
}


def _clip(text: str, budget: int, what: str) -> str:
    if len(text) <= budget:
        return text
    return text[:budget] + f"\n… [{what} truncated at {budget:,} chars — read the rest yourself]"


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def diff_briefing(workspace: Path, base_sha: str, commit_sha: str) -> str:
    """The change under review, already computed. Gates open with this."""
    if not (base_sha and commit_sha):
        return ""
    stat = _git(workspace, "diff", "--stat", f"{base_sha}..{commit_sha}")
    diff = _git(workspace, "diff", f"{base_sha}..{commit_sha}")
    if not diff:
        return ""
    return (
        f"\n\n== The change under review ({base_sha[:8]}..{commit_sha[:8]}) ==\n"
        f"{stat}\n{_clip(diff, DIFF_BUDGET, 'diff')}\n"
        "(This is the diff you were going to run yourself. Read further only "
        "where you need the surrounding code.)"
    )


def tests_briefing(workspace: Path, test_paths: list[str]) -> str:
    """The acceptance test a builder must satisfy — its actual text."""
    parts = []
    for rel in test_paths[:5]:
        path = workspace / rel
        try:
            parts.append(f"--- {rel} ---\n{path.read_text()}")
        except OSError:
            continue
    if not parts:
        return ""
    return (
        "\n\n== The acceptance test you must satisfy ==\n"
        + _clip("\n\n".join(parts), TEST_BUDGET, "tests")
        + "\n(Never edit these files.)"
    )


def project_briefing(workspace: Path) -> str:
    """The reconnaissance brief, if this project has one. Written once by the
    analyst and then, until now, read by nobody."""
    brief = workspace / "docs" / "codebase-brief.md"
    try:
        text = brief.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    return (
        "\n\n== What we already know about this codebase ==\n"
        + _clip(text, BRIEF_BUDGET, "brief")
    )


def knowledge_briefing(workspace: Path, role: str) -> str:
    """This role's slice of the curated knowledge, if the project has any."""
    parts = []
    for name in ROLE_KNOWLEDGE.get(role, ()):
        try:
            text = (workspace / KNOWLEDGE_DIR / name).read_text().strip()
        except OSError:
            continue
        if text:
            parts.append(f"--- {name} ---\n{text}")
    if not parts:
        return ""
    return (
        "\n\n== Curated knowledge (agreed with the humans — treat as ground truth) ==\n"
        + _clip("\n\n".join(parts), KNOWLEDGE_BUDGET, "knowledge")
    )


def _iteration_of(payload: dict[str, object]) -> str:
    """Derive the iteration from any typed id in the payload (I2.S3.B1 -> I2)."""
    for key in ("iteration_id", "behaviour_id", "story_id", "subject_id"):
        value = str(payload.get(key) or "")
        if value.startswith("I") and value[1:2].isdigit():
            return value.split(".", 1)[0]
    return ""


def plan_briefing(workspace: Path, payload: dict[str, object]) -> str:
    """The Owner-approved change plan for this work's iteration. Binding:
    a diff that strays from it is a finding, not a surprise."""
    iteration = _iteration_of(payload)
    if not iteration:
        return ""
    try:
        text = (workspace / PLANS_DIR / f"{iteration}.md").read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    return (
        f"\n\n== The approved change plan for {iteration} (binding — deviations "
        f"are escalations, never improvisations) ==\n"
        + _clip(text, PLAN_BUDGET, "plan")
    )


def build(workspace: Path, role: str, type_: str, payload: dict[str, object]) -> str:
    """The whole briefing for one trigger, in prompt order."""
    parts = [knowledge_briefing(workspace, role), project_briefing(workspace)]
    if role in ("specifier", "builder", "reviewer"):
        parts.append(plan_briefing(workspace, payload))
    if type_ == "gate.requested":
        parts.append(diff_briefing(workspace, str(payload.get("base_sha") or ""),
                                   str(payload.get("commit_sha") or "")))
    elif type_ in ("build.requested", "rework.requested"):
        paths = payload.get("test_paths")
        if isinstance(paths, list):
            parts.append(tests_briefing(workspace, [str(p) for p in paths]))
    return "".join(p for p in parts if p)
