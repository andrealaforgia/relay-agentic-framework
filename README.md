# Relay Agentic Framework

A multi-assistant delivery framework where a human's problem becomes shippable increments through
acceptance-test-driven development — coordinated by deterministic code, executed by AI assistants,
and recorded on an auditable event ledger.

**Core creed: models propose, deterministic code disposes.** LLM assistants analyse, plan, specify,
code, and review. Everything else — sequencing, gate accounting, state machines, timers, progress,
resume — is plain code folding over an append-only event stream. No rule ever depends on a model
remembering it.

## Quickstart

```bash
uv tool install git+https://github.com/andrealaforgia/relay-agentic-framework
relay init --project ~/code/myproject     # writes relay.toml + per-role permission profiles
relay up   --project ~/code/myproject --swarm acme
relay chat --swarm acme                   # terminal 1: talk to the Interpreter
relay watch --swarm acme                  # terminal 2: live swarm feed + progress
```

Taking over an **existing codebase**? Start with `relay learn`: it scans the code, then interviews
you (or its current developers) hypothesis by hypothesis, writing the confirmed understanding into
committed knowledge files (`docs/relay/knowledge/`) that every assistant is briefed from. And with
plan mode on (`plan_required`, the default), no iteration builds anything until you have reviewed
and approved its technical change plan in `relay plan` — the plan is then injected, binding, into
every specifier, builder, and reviewer turn.

State a problem in `relay chat`. The swarm analyses it (asking you questions), proposes a roadmap of
shippable iterations, and — once you approve — delivers it behaviour by behaviour on an iteration
branch, each behaviour driven by an independently-authored failing acceptance test, each gated by
code review and test-quality review, each visible live in `relay watch`. At the end of every
iteration you get a checkpoint: feedback, continue, re-plan, or open a PR.

## The shape

```
owner ⇄ chat CLI ⇄ interpreter ⇄ analyst           conversational plane (domain language only)
             interpreter ⇄ coordinator              plan plane
coordinator → specifier → builder → coordinator     work plane
coordinator → reviewer / qa / security              gate plane
toolgate → coordinator                              run evidence (deterministic test/mutation runs)
```

| Role | Kind | Job |
|---|---|---|
| owner | human | states the problem, answers questions, approves roadmap/checkpoints/PRs |
| interpreter | LLM (opus) | sole owner interface; roadmap of Iteration → Story → AC-behaviours + INT |
| analyst | LLM | problem analysis (Q&A loop) → prioritized INVEST user stories |
| specifier | LLM | authors the failing acceptance test per behaviour; judges completion |
| builder | LLM | ATDD red-green-refactor; small continuous commits to the iteration branch |
| reviewer / qa / security | LLM | blocking quality gates (per behaviour / story / iteration) |
| coordinator | Python | state machines, dispatch, gates, timers, progress — no LLM |
| toolgate | Python | runs tests/suites/mutation tools; publishes machine-verified evidence |

Everything rides one Redis Stream per swarm (`relay:<swarm>:ledger`) — simultaneously message bus,
append-only ledger, and audit log. The contract (`contract/relay-contract.yaml`) is the single
source of truth for who may say what to whom; it is enforced in code before every write and after
every read, and the generated models/docs are drift-tested in CI.

## Hard guarantees

- **Exact resume**: all state is a fold over the ledger; `relay down && relay up` (or `kill -9`,
  or a dead machine) resumes precisely where work stopped.
- **Existing codebases**: a reconnaissance iteration maps the codebase first; legacy areas are
  characterization-tested before they may be changed — enforced by the coordinator, not by advice.
- **Independence**: the builder never grades its own homework; red is verified by an actual failing
  run; verdicts must cite real, machine-executed run evidence.
- **Fail closed**: no timeout ever auto-approves; human gates never expire; nothing is dropped
  silently (dead-letter queue + escalation).

## Status

Phases 0–3 are complete: contract kernel + bus spine; the end-to-end delivery loop
(coordinator, toolgate, specifier/builder, live-session Interpreter in `relay chat`);
the quality gates (reviewer, qa incl. mutation-survivor judgment, security) with PR flow
and legacy intake; Codex runner, per-role Redis ACLs, and `--tmux`. A full engagement runs
with zero model calls in the test suite (`tests/integration/test_full_relay_no_llm.py`),
including exact resume after a mid-engagement crash. The realm-auditing control plane
(previously "sentinel") is removed pending a redesign — see `docs/DECISIONS.md`.
Deferred: RelayUI (web progress radiator), HMAC message provenance.
See `docs/DECISIONS.md` for design rationale and `docs/OPERATIONS.md` for running it,
single-machine or clustered.
