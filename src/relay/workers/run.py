"""python -m relay.workers.run — the single entrypoint every detached worker
process starts from. Role → worker class + runner binding happens here, from
the project's relay.toml."""

from __future__ import annotations

import argparse
import signal
import sys
import tomllib
from pathlib import Path
from types import FrameType

from relay.coordinator.main import Coordinator
from relay.runners.base import Runner
from relay.runners.claude import ClaudeRunner
from relay.workers.base import Worker
from relay.workers.chain import ChainWorker
from relay.workers.toolgate import Toolgate

CHAIN_ROLES = ("interpreter", "planner", "analyst", "specifier", "builder", "reviewer", "qa", "security")
FRAMEWORK_ROOT = Path(__file__).resolve().parents[3]


def resolve_policy_path(project: Path) -> Path | None:
    """Project's own gate policy wins; the framework default otherwise."""
    for candidate in (project / ".relay" / "gates.yaml", FRAMEWORK_ROOT / "policies" / "gates.yaml"):
        if candidate.exists():
            return candidate
    return None


def _stale_after_s(project: Path) -> float:
    """When a claimed request is old enough to be a duplicate rather than a
    rescue.

    Discarding one is only safe once the coordinator has certainly re-sent it,
    so this must clear every deadline the coordinator actually supervises on —
    not just dispatch_timeout_s. Gates are re-dispatched on their own
    GateSpec.timeout_s (_redispatch_gates), which the policy file openly
    invites projects to raise; keying on the wrong one silently inverts the
    premise and starts discarding gate requests hours before anything re-sends
    them. The shipped defaults clear it only by coincidence.
    """
    from relay.coordinator.policy import Policy
    from relay.workers.base import CLAIM_STALE_AFTER_S

    path = resolve_policy_path(project)
    policy = Policy.load(path) if path else Policy()
    supervised = [policy.dispatch_timeout_s,
                  *(g.timeout_s for g in (*policy.per_behaviour, *policy.per_story,
                                          *policy.per_iteration))]
    return max(CLAIM_STALE_AFTER_S, max(supervised) * 1.5)


def _load_config(project: Path) -> dict[str, object]:
    from relay.cli.context import config_path

    config = config_path(project)
    if config.exists():
        return tomllib.loads(config.read_text())
    return {}


WRITING_ROLES = ("specifier", "builder")


# Opus where a better model changes what you live with: the analyst's reading
# of the problem shapes every story and behaviour after it, and the
# Interpreter is the Owner's entire experience of the swarm and writes the
# roadmap (Andrea's call, 2026-08-19 — its long context costs real money at
# opus rates, and it is worth it). Gates stay on sonnet deliberately: a cheap
# gate that waves things through does not save money, it defers a bug.
ROLE_DEFAULT_MODELS = {"analyst": "opus", "interpreter": "opus", "planner": "opus"}
# Effort caps the agentic loop, and loop count is what makes a turn expensive:
# every loop re-sends the whole accumulated context. The builder writes the
# code and keeps the headroom; judging a behaviour-sized diff does not need it.
ROLE_DEFAULT_EFFORT = {
    "builder": "high",
    "specifier": "medium", "analyst": "medium", "interpreter": "medium", "planner": "medium",
    "reviewer": "medium", "qa": "medium", "security": "medium",
}
# A hard per-turn ceiling, generous enough that only a runaway hits it — the
# observed spread on a toy project was $0.20-$2.40 a turn. Fail closed: the
# turn stops, the worker fails loudly, and a human decides.
ROLE_DEFAULT_BUDGET_USD = {
    "builder": 3.0, "specifier": 2.5, "analyst": 2.5, "planner": 4.0,
    "reviewer": 1.5, "qa": 1.5, "security": 1.5,
}


def _role_config(config: dict[str, object], role: str) -> dict[str, object]:
    roles_cfg = config.get("roles")
    return dict((roles_cfg.get(role) or {}) if isinstance(roles_cfg, dict) else {})


def _runner_for(
    role: str,
    config: dict[str, object],
    project: Path,
    override: dict[str, object] | None = None,
) -> Runner:
    """The brain for this role — or for one kind of work it does.

    Tiering by role alone is coarse: a specifier writing a fresh acceptance
    test and the same specifier re-checking a green run are not the same job.
    `override` patches the role's settings for one trigger type.
    """
    from relay.cli.profiles import settings_path

    role_cfg = _role_config(config, role)
    role_cfg.update(override or {})
    runner_name = role_cfg.get("runner", "claude")
    raw_model = role_cfg.get("model")
    model = str(raw_model) if raw_model else None
    if runner_name == "codex":
        from relay.runners.codex import CodexRunner

        sandbox = "workspace-write" if role in WRITING_ROLES else "read-only"
        return CodexRunner(sandbox=str(role_cfg.get("sandbox") or sandbox), model=model)
    if runner_name != "claude":
        raise SystemExit(f"unknown runner '{runner_name}' (claude | codex)")
    settings = role_cfg.get("settings")  # explicit override wins
    default_settings = settings_path(project, role)
    resolved = Path(str(settings)) if settings else (
        default_settings if default_settings.exists() else None
    )
    skip = bool(role_cfg.get("skip_permissions", True))
    # NEVER let a worker inherit the user's personal default model: an
    # unconfigured role once silently ran the priciest tier. Explicit always.
    model = model or ROLE_DEFAULT_MODELS.get(role, "sonnet")
    effort = role_cfg.get("effort") or ROLE_DEFAULT_EFFORT.get(role)
    budget = role_cfg.get("max_budget_usd", ROLE_DEFAULT_BUDGET_USD.get(role))
    return ClaudeRunner(
        model=model, settings_path=resolved, skip_permissions=skip,
        effort=str(effort) if effort else None,
        max_budget_usd=float(str(budget)) if budget else None,
    )


def per_trigger_runners(
    role: str, config: dict[str, object], project: Path
) -> dict[str, Runner]:
    """[roles.specifier.triggers] in relay.toml — one brain per kind of work."""
    triggers = _role_config(config, role).get("triggers")
    if not isinstance(triggers, dict):
        return {}
    return {
        str(trigger): _runner_for(role, config, project, override=dict(override))
        for trigger, override in triggers.items()
        if isinstance(override, dict)
    }


def _playbook_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "roles"


def main() -> int:
    parser = argparse.ArgumentParser(prog="relay-worker")
    parser.add_argument("--swarm", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".relay")
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    config = _load_config(project)
    state_dir = args.state_root / args.swarm / args.role

    worker: Worker | Coordinator
    if args.role == "coordinator":
        worker = Coordinator(args.swarm, project, policy_path=resolve_policy_path(project))
    elif args.role == "toolgate":
        commands_raw = config.get("commands")
        commands = commands_raw if isinstance(commands_raw, dict) else {}
        toolgate_cfg = config.get("toolgate")
        toolgate_cfg = toolgate_cfg if isinstance(toolgate_cfg, dict) else {}
        worker = Toolgate(
            args.swarm, project,
            commands={k: str(v) for k, v in commands.items() if isinstance(v, str)},
            stale_after_s=_stale_after_s(project),
            inherit_login_path=bool(toolgate_cfg.get("login_path", True)),
        )
    elif args.role in CHAIN_ROLES:
        worker = ChainWorker(
            args.swarm, args.role,
            runner=_runner_for(args.role, config, project),
            playbook_path=_playbook_dir() / f"{args.role}.md",
            workspace=project,
            state_dir=state_dir,
            runners=per_trigger_runners(args.role, config, project),
            stale_after_s=_stale_after_s(project),
        )
    else:
        raise SystemExit(f"unknown role: {args.role}")

    def _stop(_sig: int, _frame: FrameType | None) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
