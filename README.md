# Relay Agentic Framework

A multi-assistant delivery framework where a human's problem becomes shippable increments
through acceptance-test-driven development — coordinated by deterministic code, executed by
AI assistants, and recorded on an auditable event ledger.

**Core creed: models propose, deterministic code disposes.** LLM assistants analyse, plan,
specify, code, and review. Everything else — sequencing, gate accounting, state machines,
progress, resume — is plain code folding over an append-only event stream. No rule ever
depends on a model remembering it.

## The shape

Everything rides one Redis Stream per swarm — simultaneously message bus, append-only
ledger, and audit trail. Who may say what to whom is defined in one contract file and
enforced in code before every write and after every read.

| Role | Kind | Job |
|---|---|---|
| **owner** | you | states the problem, answers questions, approves roadmaps, plans, and PRs |
| **interpreter** | Claude session (`relay chat`) | your sole conversation partner; domain language only |
| **curator** | Claude session (`relay learn`) | interviews you + scans the code into curated knowledge |
| **planner** | Claude session (`relay plan`) | agrees the technical change plan with you, per iteration |
| **analyst** | worker | problem analysis in a question loop, then prioritized user stories |
| **specifier** | worker | one failing acceptance test per behaviour, before any code; judges completion |
| **builder** | worker | makes the tests pass — red, green, refactor, small continuous commits |
| **reviewer / qa / security** | workers | blocking quality gates (per behaviour / story / iteration) |
| **coordinator / toolgate** | Python, no model | state machines, dispatch, gates; deterministic test & mutation runs |

## Install

From a checkout (recommended — edits take effect immediately):

```bash
git clone https://github.com/andrealaforgia/relay-agentic-framework
cd relay-agentic-framework
make install        # redis if missing, dev env, and `relay` on your PATH
```

Or without a checkout: `uv tool install git+https://github.com/andrealaforgia/relay-agentic-framework`.

Requirements: `claude` (Claude Code CLI, authenticated), `git`, and `gh` if you want PRs
opened for you (`make install` takes care of `redis-server`). Assistants run on Claude by
default; any worker role can run on OpenAI Codex instead (`runner = "codex"` in
`relay.toml`). `make help` lists the other targets (`test`, `typecheck`, `contract`, …).

## The two workflows

### Greenfield: from a problem to shipped increments

```bash
relay up ~/code/myproject     # first run auto-initializes: git repo, relay.toml,
                              # gate policy, permission profiles, redis, workers
cd ~/code/myproject
relay chat                    # terminal 1 — talk to the Interpreter
relay watch                   # terminal 2 — the live board
```

1. **State your problem** in plain language in `relay chat`.
2. **Answer the analyst's questions.** They arrive in the conversation; each round is
   generated from the previous answers, until no material ambiguity remains.
3. **Approve the roadmap** — ordered iterations, each a shippable vertical slice, each
   holding stories whose acceptance criteria become individually-tested behaviours.
4. **Approve each iteration's change plan** in `relay plan` (see below; on by default).
5. **Watch it build.** Every behaviour: failing acceptance test written by the specifier →
   red proven by an actual run → built → green proven → code review ∥ test-design review →
   accepted by the specifier citing the machine-run evidence. Stories end with a mutation
   gate (when configured), iterations with a security gate.
6. **Decide at checkpoints.** Every story tells you how to try it; every iteration ends
   with continue / re-plan / stop / open a PR (`gh`, merging stays yours).

### Taking over an existing codebase

```bash
cd ~/code/legacy-app
relay learn                   # FIRST — before any swarm exists
relay up .
relay chat                    # then proceed as above
```

`relay learn` is the difference between "an AI edited our repo" and "the team's knowledge
survived the handover" — run it while the people who know the codebase are still around.

## The three sessions

All three are **native Claude Code sessions** — real TUI, interrupts, slash commands —
opened with a role's playbook and wired to the swarm. All three keep their conversation
across invocations (`--new` starts over).

### `relay chat` — the Interpreter

Your standing conversation with the swarm. It speaks your domain language, never
technicalities. Analyst questions, story completions, checkpoints, and escalations are
injected into the conversation as they happen; whatever you type is recorded on the ledger
as the owner's words before the model acts on it. If everything goes quiet, `relay watch`
shows who is doing what — or just ask the Interpreter; it can check its own mail.

### `relay learn` — the Curator

An offboarding interview where the codebase scan writes the questions.

- **Scans** via subagents: architecture and entry points, seams and untested areas, git
  history (hotspots, co-change coupling, bus factor).
- **Interviews you hypothesis-first** — never "tell me about auth", always "this module is
  unchanged in 14 months but highly complex: (a) stable and load-bearing — my guess,
  (b) dead, (c) feared?" in rounds of 3–7, highest stakes first.
- **Writes as it learns** into `docs/relay/knowledge/` — `brief.md`, `domain.md`,
  `invariants.md`, `conventions.md`, `risk-map.md`, `open-questions.md` — committing when
  nothing material remains open. Your answers override its reading of the code;
  contradictions are surfaced, never silently resolved.

The knowledge is not decoration: every assistant is briefed from its own slice of it
(the specifier gets invariants and conventions, security gets the risk map, …), risk areas
gate uncharacterized changes, and reconnaissance is skipped when knowledge exists.
Needs no swarm and no Redis — it is safe as the very first command on a raw clone.

### `relay plan` — the Planner

Before an iteration builds anything, you and the planner agree **how** the codebase will
change. This is deliberately the one developer-facing surface: technical detail belongs here.

- Drafts `docs/relay/plans/<iteration>.md`: changes by module, **what will not change**,
  approach and sequencing, risks tied to your invariants, rejected alternatives.
- Refines it with you decision by decision — the document is always the current agreement.
- On your explicit approval it commits the plan and publishes `plan.committed` — **that
  event is the gate**: with `plan_required: true` (the shipped default in
  `.relay/gates.yaml`) the coordinator dispatches nothing for an unplanned iteration, and
  the interpreter tells you the one action needed.
- The plan rides into every specifier, builder, and reviewer turn of the iteration,
  binding: a diff that strays from it is a finding; a discovery that breaks it is an
  escalation back to you, never a silent improvisation.

## Running the swarm

| Command | What it does |
|---|---|
| `relay up <folder>` | start (auto-initialize on first run). `--roles builder,toolgate` for a subset, `--tmux` for a one-window board + tails, `--windows` (macOS) for one Terminal per assistant |
| `relay down` | stop the processes; the ledger keeps everything — `relay up .` resumes exactly, even mid-behaviour, even after `kill -9` |
| `relay watch` | the live board: per-assistant activity with elapsed time, behaviour states, event feed. `--events` shows every ledger event from the start as a table; `--tokens` shows live spend per role and per turn |
| `relay tail <role>` | one assistant's log: every tool call and turn as it happens |
| `relay costs` | what the engagement cost, folded from the ledger — per role, with cache warmth and cold starts. `--by-behaviour` attributes spend to work items instead |
| `relay status` | ledger depth, dead-letter count, who is alive |
| `relay pause <role>` / `relay resume <role>` | freeze a role's work intake (mail parks safely) and release it |
| `relay destroy` | remove every trace of a swarm — workers, ledger, Redis keys, local state (asks first; the project folder is untouched) |

All commands are project-centric like git: run them anywhere inside the project and they
find `relay.toml` by walking up. `--swarm` overrides everywhere.

## Trust, audit, and money

- `relay audit` re-validates the entire ledger against the contract — topology, payload
  schemas, sequence gaps, dangling replies, contract drift. A clean audit proves the
  ledger could only have been produced by a correctly-enforcing publisher.
- `relay export --out ledger.jsonl` snapshots the ledger verbatim (backups, incident
  capture, replayable fixtures).
- `relay costs` folds what the engagement actually cost from the ledger — per role, or per
  work item with `--by-behaviour`, with cache hits against cold starts. Every model turn
  publishes its billable footprint against the behaviour it served, so this survives
  `relay destroy`; per-turn budget ceilings fail closed. For the live view while the swarm
  runs, use `relay watch --tokens`.
- `relay doctor` preflights the setup: Redis reachable, append-only persistence on,
  ledger audit clean.

The standing guarantees: **no timeout ever auto-approves; human gates never expire;
nothing is dropped silently** (off-contract input is dead-lettered *and* recorded on the
ledger); the builder never grades its own homework; red and green are proven by real runs.

## Configuration

**`relay.toml`** (written by `relay up`, committed):

```toml
[swarm]
name = "myproject"

[commands]                       # how the toolgate runs things, per project
acceptance_test = "uv run pytest -q {test_paths}"
# mutation = "uv run mutmut run"  # enables the per-story mutation gate

[roles.builder]
runner = "claude"                 # or "codex" (sandboxed per role)
# model = "opus"                  # per-role model override
# skip_permissions = false        # fall back to generated permission profiles
```

**`.relay/gates.yaml`** (project copy wins): which gates block at which granularity
(review + test-design per behaviour, mutation per story, security per iteration),
`plan_required`, attempt limits, and how much work goes into one model transaction
(`spec_granularity` / `build_granularity`: `story` batches a story's behaviours into one
turn; `behaviour` is one turn per slice).

**What lives in your repo:** `docs/relay/knowledge/` (curated understanding),
`docs/relay/plans/` (approved change plans), the iteration branches
(`relay/<swarm>/i<n>`), and commits tagged `[I2.S3.B1]` so ledger ↔ git is greppable in
both directions.

## Multiple machines

The hub is Redis; workers go where their work is. Put hub and hosts on a tailnet, give
each host its own clone plus the runner CLI, generate per-role credentials with
`relay acl --swarm x --out acls.sh` (scoped keys, no admin commands), and start role
subsets per host:

```bash
REDIS_HOST=hub relay up ~/clones/app --swarm acme --roles builder,specifier,toolgate
REDIS_HOST=hub relay chat --swarm acme        # your laptop: just the conversation
```

Failover needs no ceremony: start the same role anywhere and it claims the dead
consumer's pending work automatically. Details in `docs/OPERATIONS.md`.

## Going deeper

- `docs/DECISIONS.md` — why the framework is shaped this way, decision by decision
- `docs/PROTOCOL.md` — the full message catalog (generated from the contract)
- `docs/OPERATIONS.md` — day-2 operations, single-machine and clustered
- `relay contract show` / `relay contract gen` — the contract itself and its artifacts
