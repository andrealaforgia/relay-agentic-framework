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

CHAIN_ROLES = ("interpreter", "analyst", "specifier", "builder", "reviewer", "qa", "security")
FRAMEWORK_ROOT = Path(__file__).resolve().parents[3]


def resolve_policy_path(project: Path) -> Path | None:
    """Project's own gate policy wins; the framework default otherwise."""
    for candidate in (project / ".relay" / "gates.yaml", FRAMEWORK_ROOT / "policies" / "gates.yaml"):
        if candidate.exists():
            return candidate
    return None


def _load_config(project: Path) -> dict[str, object]:
    from relay.cli.context import config_path

    config = config_path(project)
    if config.exists():
        return tomllib.loads(config.read_text())
    return {}


WRITING_ROLES = ("specifier", "builder")


ROLE_DEFAULT_MODELS = {"interpreter": "opus", "sentinel": "opus"}


def _runner_for(role: str, config: dict[str, object], project: Path) -> Runner:
    from relay.cli.profiles import settings_path

    roles_cfg = config.get("roles")
    role_cfg = (roles_cfg.get(role) or {}) if isinstance(roles_cfg, dict) else {}
    runner_name = role_cfg.get("runner", "claude")
    model = role_cfg.get("model")
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
    return ClaudeRunner(model=model, settings_path=resolved, skip_permissions=skip)


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
        worker = Toolgate(args.swarm, project, commands=commands)
    elif args.role == "sentinel":
        from relay.workers.sentinel import SentinelWorker

        worker = SentinelWorker(
            args.swarm,
            runner=_runner_for(args.role, config, project),
            playbook_path=_playbook_dir() / "sentinel.md",
            workspace=project,
            state_dir=state_dir,
        )
    elif args.role in CHAIN_ROLES:
        worker = ChainWorker(
            args.swarm, args.role,
            runner=_runner_for(args.role, config, project),
            playbook_path=_playbook_dir() / f"{args.role}.md",
            workspace=project,
            state_dir=state_dir,
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
