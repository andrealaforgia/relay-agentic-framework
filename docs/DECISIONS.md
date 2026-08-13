# Design Decisions

This document records why Relay v2 is shaped the way it is. The predecessor is
`~/dev/agentic-working-model` (referred to as **v1**); v2 is a greenfield rewrite that ports v1's
proven ideas and deletes its documented failure modes.

## D1 — Models propose, deterministic code disposes

**The** structural change vs v1. v1 asked LLMs to remember orchestration rules: the interpreter
maintained a roadmap checklist and computed "% done", playbooks contained anti-stall protocols,
observers managed their own git diff cursors. v2 moves all of that into two non-LLM processes:

- **coordinator** — behaviour/story/iteration state machines, dispatch, gate fan-out and
  accounting, timers/escalation, progress arithmetic, roadmap validation.
- **toolgate** — deterministic execution of tests, suites, and mutation tools; publishes
  `run.completed` evidence with a `run_id` that verdicts must cite.

This generalizes v1's own best principle ("rules in one data file, enforced in code — never by the
model's memory") from message validation to orchestration. Consequences: playbooks shrink to ~80
lines of role identity; stalls are timer events; progress is `len(done)/len(total)`; "the model
forgot to send its message" is detected mechanically.

## D2 — One stream per swarm, planes as a field

`relay:<swarm>:ledger` is simultaneously the bus, the append-only ledger, and the audit log
(`XADD` = persist + publish, kept from v1's eventbus). Logical separation ("topics") is the
`plane` + `type` fields, enforced by the contract — not separate physical streams. Rationale:
total order is the audit product; per-topic streams would require a second "ledger write", a dual
write whose crash window forks truth. Fan-out cost is irrelevant at this volume.

**Direct communication (the sentinel question) is a field, not a second system**: `plane: control`
on the same stream. Corrections are direct-addressed, consumed like mail, but sit in the same total
order — no off-ledger channel exists. The `correction.issued` payload schema is structurally
incapable of carrying work content, so the control plane cannot become a covert work channel. The
coordinator ignores control messages for work state.

## D3 — Events are the only source of truth

All work state is `fold(events) → state`. There is no state document: two write paths that can
disagree are exactly the drift class that produced v1's three drifted topology copies. Snapshots
exist only as verified caches (event-id-at-seq checked against the ledger; mismatch → discard
loudly, full replay). Replay doubles as the resume mechanism, the test harness, and the post-mortem
tool. Volume (10³–10⁴ events/iteration) makes full replay milliseconds.

## D4 — One contract file, drift impossible by construction

`contract/relay-contract.yaml` is the ONLY definition of roles, planes, edges, message types, and
per-type payload schemas. Enforced at runtime before every XADD and after every read; Pydantic
models and PROTOCOL.md are generated from it and byte-diffed in CI; every message type must carry
at least one example payload (test-enforced); every envelope carries a `contract_hash` so two
machines can never silently run different rules. v1's drift (root topology.json vs
relay/topology.json vs inline orchestrator copy vs stale message.schema.json) is unrepresentable.

## D5 — Typed references, no `refs: string[]`

v1 threaded lineage through a comma-joined string list and matched commit SHAs by substring. v2
makes every reference a typed, schema-required field: `iteration_id` (`I2`), `story_id` (`I2.S3`),
`behaviour_id` (`I2.S3.B1` / `I2.INT`), `commit_sha` (full 40-char, pattern-validated), `run_id`,
`gate_id`, `in_reply_to` (event ULID).

## D6 — The specifier replaces the examiner; the toolgate replaces the courier

v1's hardest-won lesson: the builder must never grade its own homework. v2 keeps the independence
but sharpens the artifact — an executable failing acceptance test IS the expectation, and re-running
it IS the judgment. Red is verified by an actual toolgate run (exit ≠ 0) before the builder is ever
dispatched; a verdict must cite a green `run_id` that exists on the ledger. The courier (re-running
the accumulated suite) never needed a model: the toolgate runs suites; provenance became mechanical.

## D7 — Roster optimisation

Requirements named nine assistants. v2 ships seven LLM roles: examiner → specifier (D6); courier →
toolgate (D6); reaper (mutation) → merged into **qa** — mutation *execution* is deterministic
(toolgate), and judging survivors is test-quality work, the same realm as the Farley review; the
progress heartbeat → coordinator arithmetic. Roles stay distinct in the contract (clean realms,
clean audit) but one worker process may host several gate roles — process count is a deployment
choice, not an architecture change.

## D8 — Gates block the work, not the builder

The builder's turn ends at `behaviour.built`; the coordinator holds pending-gate state and fans out
reviewer ∥ qa. DECIDED granularity (policy-configurable in `policies/gates.yaml`): review + qa per
behaviour, mutation per story, security per iteration. Timeouts re-dispatch once then escalate to
the owner — **no timeout ever auto-approves; human gates never expire; fail closed.**

## D9 — Git: branch per iteration, serialized writers, pinned reads

Iteration branch `relay/<swarm>/i<N>` created by deterministic code. Specifier and builder both
write it, but the state machine guarantees they never hold the pen concurrently. Gate roles read
via detached worktrees pinned to the message's full SHA — reviewing the wrong code is physically
impossible. Commits carry `[I2.S3.B1]` subjects + `Relay-Behaviour`/`Relay-Event` trailers so
ledger↔git is greppable in both directions. PR at iteration end via `gh pr create`, on owner
approval only; merging stays human.

## D10 — Headless runners; autonomy over per-tool prompts

Assistants are headless worker processes (`claude -p --resume`, `codex exec`) behind a `Runner`
protocol; viewer terminals are read-only stream consumers (`relay watch`, `relay tail`, optional
`--tmux`). v1's AppleScript/TUI-footer scraping — its worst silent failure — is unrepresentable.
Claude sessions run with `--dangerously-skip-permissions` by default (Andrea's call, 2026-08-13):
in headless mode a denied tool is a silent stall, and allowlists can never anticipate every
command a builder legitimately needs — the guardrails that matter are the contract-validated
bus, the deterministic gates, and the audit, not per-tool prompts. Per-role permission profiles
still exist and can be re-enabled with `skip_permissions = false` in `relay.toml`; the
interpreter's profile is always loaded regardless, because it carries the relay-inbox hooks.
The model's only output channel is `relay-send` (validated publish); nothing parses model stdout.

## D11 — The framework tests itself without any LLM

v1 had zero self-tests. v2's suite runs with zero model calls: contract/drift tests, bus unit
tests, a full-relay integration test driven by a table-driven `FakeRunner` (with misbehaviour
modes: publish_nothing, publish_garbage, crash, double_publish), chaos tests (SIGKILL at every
phase boundary → exact resume), and replay tests over recorded/adversarial ledger fixtures. Every
real incident becomes a fixture.

## D12 — Existing codebases are a first-class mode

A repo with history triggers Iteration 0 (reconnaissance → `codebase-brief.md`); the roadmap
grammar supports characterization behaviours; the coordinator refuses to dispatch a build whose
declared `touches[]` intersect uncharacterized risk areas. "Never touch legacy code without
characterization tests" is a dispatcher rule, not a playbook sentence.

## D13 — The Interpreter is a live session, not a headless worker

Andrea's feedback after the first live run: headless `claude -p` made the
owner's conversation partner a black box. `relay chat` now hosts the
Interpreter as a persistent stream-json Claude session: replies stream in
live, context spans the engagement (resumable), and bus events are fed into
the conversation. Owner utterances are recorded on the ledger BEFORE the
model sees them; the Interpreter's formal moves still go through relay-send.
The interpreter→owner conversational leg lives in the session transcript
rather than the ledger — the formal artifacts (roadmap commits, decisions,
checkpoints) remain fully audited. Chat closed = interpreter offline; its
mail waits. Every other worker streams its activity (tool calls, text) into
its log and presence status, so `relay watch` answers "is it stuck?" at a
glance.

## D14 — Sentinel: mechanical first, model second; corrections are worker duty

The sentinel's cheap, deterministic checks live in code (verdicts citing
runs that never completed, gate verdicts for unknown gates, sequence gaps)
and publish corrections without a model. Only the semantic realm audit
("provenance, not vocabulary") spends model turns, in batches. The culprit's
WORKER acks corrections mechanically and injects them into the next model
turn — an ignored sentinel is structurally impossible, and repeat offenders
escalate to the interpreter (once per role). `pause.ordered` is enforced by
the worker loop: parked work stays in the PEL, `resume` drains it.

## Phased roadmap

- **Phase 0** — contract kernel + bus spine, all self-tested, no LLM anywhere.
- **Phase 1** — thin end-to-end slice: coordinator, toolgate, chain assistants (interpreter,
  analyst, specifier, builder), chat + watch, specifier verdict as the only gate.
- **Phase 2** — reviewer/qa/security gates, rework loop, PR flow, legacy intake.
- **Phase 3** — sentinel + control plane, CodexRunner, multi-machine hardening, `--tmux`.
- **Deferred** — RelayUI (web progress radiator; cheap by design — a read-only stream consumer
  reusing the coordinator's projection), HMAC provenance, documenter role.
