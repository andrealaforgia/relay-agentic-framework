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
`--tmux`). v1's AppleScript/TUI-footer *scraping* — its worst silent failure — is unrepresentable: no rule
ever depends on parsing rendered output, and relay never borrows a terminal it did not create.
(Supplying a session's input is a different thing and is how the Interpreter is woken — see D18.)
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

## D15 — Cost is a ledger fact, not a log line

The token-burn investigation had to be done as forensics over Claude Code's
session transcripts, and those transcripts have since been deleted with the
project they measured — the evidence for the framework's most expensive
incident no longer exists. Transcripts are also attributed by guesswork (a
regex over the prompt) and say nothing about which behaviour the spend served.

So every model turn now publishes `usage.reported` on the system plane, against
the work item it was serving: the tier that actually billed, the four token
counters, the cost, whether the session started cold, and how many agentic
loops the invocation spent. Cost per behaviour, per gate, per role, per model
is a fold like everything else that is true (D3), it survives `relay destroy`,
and it is auditable. The Interpreter has no worker loop to report from, so its
Stop hook reads what its own session spent from the transcript and publishes
the same event — the one unbounded, opus-priced session must not be the
invisible one.

Two consequences worth stating. `relay costs` reads the ledger by default
(`--transcripts` keeps the old per-API-call view). And the worker's own
bookkeeping is on the system plane, which the verify-don't-trust check now
skips: a silent model must never be able to pass verification on our
paperwork.

Cache reads bill at a tenth of input and cache writes at 1.25x, so the report
also states billed input equivalents and cache warmth. Raw token totals
flatter whichever strategy writes more and reads less, which is exactly the
comparison the session-rotation cap needs to be judged on.

## D16 — Rediscovery is the bill; hand context down and cap the loop

Forensics on the tic-tac-toe run: $21.99 for eight behaviours, of which
specification and gates were 64%. The single priciest turn was a specifier
writing ONE acceptance test — 37 agentic loops, 3.48M cache-read tokens, seven
minutes, $2.39. Cache writes on that turn were 18k. The cache was working
perfectly; the cost was the loop count, because every loop re-sends the whole
accumulated context. A turn's price grows quadratically with how much it had
to go and find out.

So three changes, all deterministic, none relying on a model remembering
anything:

- **Briefings** (`workers/briefing.py`): a gate is handed the diff the
  coordinator already knows how to compute; a builder is handed the text of
  the acceptance test it must satisfy; every worker is handed the
  reconnaissance brief that Iteration 0 paid a model to write and that,
  until now, nobody read. Bounded, and truncation is announced.
  Measured on a real behaviour from the tic-tac-toe run: a code-review turn
  went from 16 loops and 653,651 read tokens to 1 loop and 22,461 — same
  verdict, a quarter of the cost.
- **Effort** (`--effort`, per role in relay.toml): the direct control on loop
  count. The builder keeps headroom; judging a behaviour-sized diff does not
  need it.
- **A hard per-turn ceiling** (`--max-budget-usd`, per role): the turn stops
  and the worker fails loudly. A budget stop is never retried — correcting it
  would spend the same money again — so it goes straight to `worker.failed`
  and the DLQ. Fail closed, applied to money.

Two supporting fixes. The prompt is now assembled static-first (playbook,
then trigger, then briefing, then sentinel corrections), because a cache
prefix is a byte prefix and the old order put the volatile part first. And
cost is estimated from tokens where the runner reports none (`pricing.py`),
so the Interpreter stops reporting $0.00 — cache writes are priced at 2x, not
1.25x, because Claude Code uses the one-hour TTL, which reconciles exactly
against measured turn costs.

## D17 — Rework goes to whoever can act on it

The same forensics turned up a routing defect that cost more than any single
inefficiency. EVERY failed gate was dispatched to the builder as
`rework.requested`, including `test_design` — a gate that judges the tests the
SPECIFIER wrote and that the builder's own playbook forbids it from touching.
The findings were not carried either: the payload said "gate test_design
failed — see the verdicts on the ledger". So the builder was asked, three
times, to make a change it was not allowed to make, without being told what
the change was. It replied "unchanged from attempt 1", then "unchanged from
attempts 1-2", and the behaviour blocked. Three behaviours died this way in
one iteration, each burning three builder turns plus re-gating.

Rework now routes by realm — `test_design` and `mutation` to the specifier,
everything else to the builder — and carries the gate's actual findings
instead of a summary of its own name. The projection keeps the failing gate
and its findings so the coordinator has something real to forward.

## D18 — Waking the Interpreter: hold its input, knock, never deliver

Measured stall (sandtris, 15 Aug): the Interpreter dispatched
`analysis.requested` at 20:59:39, its turn ended at 20:59:55, the Analyst
answered at 21:02:51 — and the questions sat unread all evening. A Claude Code
session reads its mail only at its own turn boundaries or when the Owner
types, and the playbook rule that covers this ("use `relay-inbox --wait` after
dispatching work") is exactly the kind of rule D1 says we never rely on.

Nothing can wake a session from outside. Its control socket
(`/tmp/cc-socks/<pid>.sock`) answers neither JSON-RPC, MCP initialize, LSP
framing nor a WebSocket upgrade; macOS refuses `TIOCSTI` on a terminal you do
not own; an MCP server cannot start a turn; `claude agents` has no send. The
only way in is to hold the session's input, which relay can do because relay
starts the session.

So `relay chat` no longer execs Claude Code and vanishes. It forks it under a
pty and relays bytes untouched — the Owner sees the real interface — while a
watcher reads the ledger for undelivered Interpreter mail and types a nudge.

Three properties make this v1's instinct without v1's failure. Relay owns the
pty it created, so there is no window to find and no permissions to ask for.
Nothing parses what the session prints; the trigger is a ledger event.
And the nudge carries NO CONTENT — delivery still goes through `relay-inbox`,
so the ack, the ledger record and the audit are untouched, and a nudge that
misses costs nothing because the mail is still queued for the next keystroke.
A polling loop inside the session was rejected on measurement: at 45-80k
tokens per API call, a 30-second poll costs $3-5/hour to sit idle.

Belt and braces, and free: when the swarm owes the Interpreter a reply, the
Stop hook now blocks on Redis for up to five minutes rather than letting the
session go idle. Waiting costs no tokens at all.

## D19 — An integration behaviour per story, made by code

D18's sibling. The Owner was getting something to try at the end of each
ITERATION, because the integration behaviour — the one that says "open it and
play it" — existed only at iteration level. A story that cannot be
demonstrated is not a vertical slice, so every story now ends with its own
`I1.S1.INT`, created by the coordinator exactly as the iteration's is, and
`story.completed` waits for it. The iteration keeps its own, which proves the
stories work TOGETHER rather than each on its own.

This also closes the gap in D-story-checking: the story's INT builder owes
`how_to_run`, so `story.completed` reliably carries commands the Owner can run.

Integration behaviours are code's, never a model's. A roadmap that writes one
is rejected with a reason that says so, and the projection skips it on the way
in so a stray one cannot produce a second row for the same id — which is
exactly the duplicate `I1.INT` seen on the sandtris board.

The cost is honest and worth stating: one extra spec-build-gate cycle per
story. On a 14-story roadmap that is 14 more behaviours. If that proves too
expensive, the lever is fewer, larger stories — not a story you cannot try.

## D20 — The unit of transaction is not the unit of discipline

Two iterations of sandtris cost $258.99. The striking part is where: the gates
— reviewer, qa, security, the entire safety net — were $23.50, nine percent.
The builder, which writes the product, was fifteen percent. The rest went on
coordination and on acquiring context five times per behaviour: a specifier
context, a builder context, two gate contexts and a judgement context, each
paying ~34k of harness floor and then rediscovering the codebase, for a slice
like "the piece moves right".

Elephant Carpaccio assumes a slice is nearly free to take. Under a machine it
has a fixed setup cost, so slicing thinner stops being free and starts being
the dominant term.

So granularity is now policy (`spec_granularity`, `build_granularity`,
default `story` in the shipped policy). At story granularity the specifier is
handed every criterion of a story and writes all the failing tests in one
turn; the builder is handed all of them once they are red and satisfies them
one at a time in one warm session.

What does not change, and this is the point: one failing test per criterion,
one commit and one `behaviour.built` per behaviour, gates on the diff, red
verified by a real run, the specifier still never satisfying its own
expectation, and the Owner still trying something at the end of every story.
The discipline is per behaviour. Only the transaction is per story.

Set both back to `behaviour` to compare — the ledger will say which is
cheaper, and `relay costs --by-behaviour` makes it a measurement rather than
an argument.

## D21 — An escalation must have a way back

scopa ran overnight and stopped at 23:55 with two behaviours blocked, both
escalated to the Owner, and nine hours of silence. The escalation was correct
— fail closed, ask the human, spend nothing while waiting. What was missing:

    $ grep -rn "decision.made" src/relay/coordinator/
    $   (nothing)

`BLOCKED` was a one-way door. The coordinator escalated, the Owner answered in
chat, and nothing consumed the answer. Awake or asleep made no difference.
`decision.made` now travels interpreter>coordinator and carries `retry` (back
to work with a fresh attempt budget) or `drop` (it will not ship this
iteration), against a `subject_id`.

The second block was not a decision at all. The builder implemented "computer
plays automatically" and reported that two earlier acceptance tests now failed,
because they assert the table grows by exactly one card when the Owner plays.
An older expectation invalidated by a later behaviour is ordinary in ATDD — you
amend the test. Here the builder may not touch tests and the specifier was
never asked, so it escalated a question the Owner could not usefully answer.
`error.raised` gains kind `spec_conflict`, which routes to the specifier as
rework with the detail as a finding.

And a bug of my own, found while diagnosing: story-granularity dispatch set the
batch's states in the dispatcher's memory but the projection only moved the
behaviour named in the payload. The live run was fine; any restart re-dispatched
finished work. State is a fold over the ledger (D3) or it is nothing — the fold
now moves every behaviour a batched request covers.

## Phased roadmap

- **Phase 0** — contract kernel + bus spine, all self-tested, no LLM anywhere.
- **Phase 1** — thin end-to-end slice: coordinator, toolgate, chain assistants (interpreter,
  analyst, specifier, builder), chat + watch, specifier verdict as the only gate.
- **Phase 2** — reviewer/qa/security gates, rework loop, PR flow, legacy intake.
- **Phase 3** — sentinel + control plane, CodexRunner, multi-machine hardening, `--tmux`.
- **Deferred** — RelayUI (web progress radiator; cheap by design — a read-only stream consumer
  reusing the coordinator's projection), HMAC provenance, documenter role.
