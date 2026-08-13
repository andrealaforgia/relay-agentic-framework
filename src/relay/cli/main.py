"""The `relay` CLI. Named flags only (--swarm, --project) — positional pairs
whose order you can swap are how v1 grew two launchers with opposite argument
orders."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from relay import __version__
from relay.bus import dlq as dlq_mod
from relay.bus.client import get_client
from relay.bus.keys import ledger_key, presence_key
from relay.contract import ContractValidator, load_contract
from relay.contract.codegen import write_artifacts
from relay.ledger.audit import audit_ledger
from relay.ledger.export import export_jsonl

app = typer.Typer(no_args_is_help=True, help="Relay Agentic Framework")
contract_app = typer.Typer(no_args_is_help=True, help="Contract tooling")
app.add_typer(contract_app, name="contract")
console = Console()

SwarmOpt = typer.Option(..., "--swarm", help="Swarm name")


@app.callback()
def _version_callback() -> None:
    """Relay Agentic Framework CLI."""


@contract_app.command("gen")
def contract_gen() -> None:
    """Regenerate contract/schema/*.json and docs/PROTOCOL.md from the contract."""
    contract = load_contract()
    paths = write_artifacts(contract)
    console.print(f"[green]✓[/green] contract {contract.contract_hash[:12]} — {len(paths)} artifacts written")


@contract_app.command("show")
def contract_show() -> None:
    """Summarize the loaded contract."""
    c = load_contract()
    console.print(f"version {c.version}, hash [bold]{c.contract_hash}[/bold]")
    console.print(f"{len(c.message_types)} message types, {len(c.edges)} edges, planes: {', '.join(c.planes)}")


@app.command()
def audit(swarm: str = SwarmOpt) -> None:
    """Re-validate the whole ledger against the contract. Exit 1 on findings."""
    client = get_client()
    report = audit_ledger(client, ContractValidator(load_contract()), swarm)
    if report.ok:
        console.print(f"[green]✓[/green] {report.entries} entries, no findings")
        raise typer.Exit(0)
    table = Table(title=f"Audit findings — swarm '{swarm}' ({report.entries} entries)")
    table.add_column("seq")
    table.add_column("rule")
    table.add_column("detail")
    for f in report.findings:
        table.add_row(str(f.seq), f.rule, f.detail)
    console.print(table)
    raise typer.Exit(1)


@app.command()
def export(
    swarm: str = SwarmOpt,
    out: Path = typer.Option(..., "--out", help="Output JSONL path"),
) -> None:
    """Export the ledger as JSONL (fixtures, backups, incident capture)."""
    count = export_jsonl(get_client(), swarm, out)
    console.print(f"[green]✓[/green] {count} entries → {out}")


@app.command()
def status(swarm: str = SwarmOpt) -> None:
    """Ledger depth, DLQ depth, live presence."""
    client = get_client()
    entries = client.xlen(ledger_key(swarm))
    dead = dlq_mod.dlq_depth(client, swarm)
    console.print(f"ledger: {entries} entries   dlq: {'[red]' if dead else ''}{dead}{'[/red]' if dead else ''}")
    pattern = presence_key(swarm, "*", "*")
    live = sorted(str(k) for k in client.scan_iter(match=pattern))
    if live:
        console.print("live workers:")
        for key in live:
            console.print(f"  • {key.rsplit(':', 1)[-1]}")
    else:
        console.print("live workers: none")


@app.command()
def doctor(swarm: str = SwarmOpt) -> None:
    """Preflight checks: Redis reachable, AOF on, ledger audit clean."""
    failures = 0
    try:
        client = get_client()
        client.ping()
        console.print("[green]✓[/green] redis reachable")
    except Exception as e:  # noqa: BLE001 — every failure is a finding here
        console.print(f"[red]✗[/red] redis unreachable: {e}")
        raise typer.Exit(1) from None

    appendonly = client.config_get("appendonly").get("appendonly")
    if appendonly == "yes":
        console.print("[green]✓[/green] AOF persistence on")
    else:
        console.print("[red]✗[/red] AOF is off — losing the ledger tail on power loss is a correctness bug")
        failures += 1

    report = audit_ledger(client, ContractValidator(load_contract()), swarm)
    if report.ok:
        console.print(f"[green]✓[/green] ledger audit clean ({report.entries} entries)")
    else:
        console.print(f"[red]✗[/red] ledger audit: {len(report.findings)} findings (run `relay audit`)")
        failures += 1

    raise typer.Exit(1 if failures else 0)


@app.command()
def up(
    swarm: str = SwarmOpt,
    project: Path = typer.Option(..., "--project", help="Target project directory"),
    roles: str = typer.Option("", "--roles", help="Comma-separated subset (default: all Phase-1 roles)"),
) -> None:
    """Start the swarm's workers as detached processes."""
    from relay.cli import procs

    project = project.expanduser().resolve()
    selected = [r.strip() for r in roles.split(",") if r.strip()] or list(procs.PHASE1_ROLES)
    for role in selected:
        pid = procs.start_worker(swarm, role, project)
        console.print(f"[green]✓[/green] {role} (pid {pid}) — log: {procs.logfile(swarm, role)}")
    console.print(f"\nnext:  relay chat --swarm {swarm}   and   relay watch --swarm {swarm}")


@app.command()
def down(swarm: str = SwarmOpt) -> None:
    """Stop the swarm's workers (by pidfile — the ledger keeps everything)."""
    from relay.cli import procs

    for role in list(procs.running_roles(swarm)) or list(procs.PHASE1_ROLES):
        if procs.stop_worker(swarm, role):
            console.print(f"[green]✓[/green] {role} stopped")


@app.command()
def chat(swarm: str = SwarmOpt) -> None:
    """Talk to the Interpreter. Async: replies render as they arrive."""
    from relay.cli.chat import OwnerChat

    OwnerChat(swarm).run()


@app.command()
def watch(swarm: str = SwarmOpt) -> None:
    """Live board: assistant liveness, behaviour states, event feed."""
    from relay.cli.watch import watch as watch_loop

    try:
        watch_loop(swarm)
    except KeyboardInterrupt:
        pass


@app.command()
def tail(
    swarm: str = SwarmOpt,
    role: str = typer.Option(..., "--role", help="Assistant role to follow"),
) -> None:
    """Follow one assistant's worker log."""
    import time as _time

    from relay.cli import procs

    log = procs.logfile(swarm, role)
    console.print(f"[dim]{log}[/dim]")
    with log.open() as f:
        try:
            while True:
                line = f.readline()
                if line:
                    console.print(line.rstrip())
                else:
                    _time.sleep(0.5)
        except KeyboardInterrupt:
            pass


@app.command()
def init(project: Path = typer.Option(..., "--project", help="Target project directory")) -> None:
    """Write a starter relay.toml into the target project."""
    project = project.expanduser().resolve()
    if not project.is_dir():
        console.print(f"[red]✗[/red] not a directory: {project}")
        raise typer.Exit(1)
    gitignore = project / ".gitignore"
    marker = ".relay/"
    if not gitignore.exists() or marker not in gitignore.read_text():
        with gitignore.open("a") as f:
            f.write(f"\n# relay swarm runtime state\n{marker}\n")
        console.print(f"[green]✓[/green] added {marker} to {gitignore}")
    config = project / "relay.toml"
    if config.exists():
        console.print(f"[yellow]•[/yellow] {config} already exists — leaving it untouched")
        raise typer.Exit(0)
    config.write_text(
        "# Relay swarm configuration for this project\n"
        f'# generated by relay {__version__}\n\n'
        "[swarm]\n"
        'default_branch = "main"\n\n'
        "[roles.interpreter]\n"
        'runner = "claude"\n'
        'model = "opus"\n\n'
        "[roles.analyst]\n[roles.specifier]\n[roles.builder]\n"
        'runner = "claude"\n'
    )
    console.print(f"[green]✓[/green] wrote {config}")


if __name__ == "__main__":
    app()
