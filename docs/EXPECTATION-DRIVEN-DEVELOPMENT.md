# Expectation-driven delivery

Relay's expectation protocol connects owner input to versioned story obligations,
real execution receipts and answers to property questions written before building.
It is implemented directly in Relay, with deterministic gates and test-first changes.

## Workflow

1. The interpreter commits a roadmap with `protocol_version: 2`. New roadmaps sent
   through `relay-send` default to version 2.
2. Before specification/build dispatch, the analyst preserves the story's approved
   expectations, cites exact owner-source quotes and writes scoped property questions.
3. The specifier commits executable property checks. QA reviews that commit for
   relevance, coverage, boundaries and tautologies. Missing checks or a failed review
   keep implementation closed.
4. Existing acceptance-test-driven behaviour delivery runs. Builder completions are
   claims; the builder cannot issue execution receipts through the model CLI.
5. The toolgate executes every expectation, story integration check and property at
   one candidate commit. The initial adapter supports pytest file/node selectors.
6. The analyst receives the original questions and execution receipt. Every question
   requires `supported`, `contradicted` or `insufficient_evidence`, a reference to
   that receipt and an explanation of what it establishes. Only all-supported
   answers release the story.
7. The final iteration candidate is checked again, including the iteration integration
   test. A changed commit or changed obligation set requires new execution and answers,
   even if a previous story was accepted.

Failed behaviour returns to the builder with actual execution output. Insufficient
verification returns to the specifier with the original questions intact. The
corrected checks receive another independent QA review. Environment or provenance
failures escalate rather than being treated as product failures. Dispatch retries
and verification attempts are bounded by `max_attempts`; silence never approves.
A story-level `drop` is reported as accepted risk, not verification. Replan explicitly
to remove or revise scope before continuing that story.

## Codex questions and Claude self-review

For protocol-2 stories, Relay uses two models behind the Analyst role. By default,
`story.preparation.requested` uses Codex in a read-only sandbox with high reasoning
effort. `story.validation.requested` uses Claude Opus with high effort to review
the implementation against the original questions and the actual execution receipt.
Normal analysis and reconnaissance retain the role's configured runner.

Question authoring uses the model selected by the Codex CLI with high reasoning
effort. No model ID is hard-coded. These defaults apply without extra configuration;
to make them explicit in the project's `.relay/relay.toml`, use:

```toml
[roles.analyst.triggers."story.preparation.requested"]
runner = "codex"
effort = "high"
sandbox = "read-only"

[roles.analyst.triggers."story.validation.requested"]
runner = "claude"
model = "opus"
effort = "high"
```

To select a particular Codex model instead, add its exact ID as `model` in the
preparation trigger section. Both CLIs must be installed
and authenticated on the analyst worker host. Restart the workers after changing
configuration. No new worker or gate is needed. Existing protocol-2 stories already
prepared keep their questions; an explicit approved replan is required to regenerate
them. Historical protocol-1 roadmaps do not acquire these stages automatically.

Codex returns structured preparation data. Relay checks the story/version, exact
expectations and source quotations before publishing it. Material ambiguity is
reported as an error instead of invented requirements. Codex does not need Redis
access to publish questions. The coordinator still validates and preserves the
obligations before any builder starts.

Claude receives those same question IDs and texts, the current receipt and a
checkout pinned to its candidate commit. Its session is separate from question
authoring. Every question needs a supported, contradicted or insufficient-evidence
answer; self-review is not an instruction to approve. A failed model invocation or
invalid output never substitutes for questions or evidence. Turn usage records
identify the configured model, or report it as unknown when no model was supplied.

## Receipt contents and guarantees

A receipt contains the story/version, run/request identities, candidate SHA,
obligation digest, actual command, setup output, runtime versions, test file digests,
executed case IDs/outcomes, expectation-to-case coverage, stdout/stderr and exit
status. The toolgate records the receipt after execution. No signing keys or
signature verification are required. Receipts are embedded in the append-only ledger,
so remote readers and JSONL exports carry their contents without depending on a
worker's local log path.

The coordinator rejects missing receipts, wrong commits or requests,
stale versions, unrelated answers and incomplete coverage. Skips, xfails, missing
cases and a successful command that ran no required checks cannot count as proof.
Verification files are compared with their specification commits; changing a
protected check cannot silently turn it green. Raw output remains evidence data,
not instructions to the analyst.

Run references are also validated for legacy acceptance/setup/mutation/property
runs. Duplicate delivery is harmless. Conflicting execution results are contested,
not overwritten, and require fresh verification. Ledger replay ignores events
already folded, preserving story versions across coordinator restarts.

Executed tests establish evidence within their declared scope. Generated cases do
not prove an unbounded theorem. An independently reviewed test can still embody a
mistaken interpretation of the owner's intent. Semantic completeness and subjective
qualities still need analyst/QA judgement or owner clarification.

Receipts are execution records, not cryptographic attestations. Relay trusts the
toolgate and ledger access controls; anyone who can write arbitrary ledger events
could fabricate a consistent receipt. `relay-send` refuses infrastructure
impersonation and mismatched actor roles, but that is not an OS or Redis access
boundary. The pytest collector also runs in the test process, so deliberately
hostile project code could interfere with its report. Use reviewed checks and a
trusted test toolchain.

## Configure test execution

The first adapter needs pytest in the project's test toolchain. Receipt signing,
OpenSSL and private/public key provisioning are not required.

Configure an isolated runner image containing the approved toolchain and cached
dependencies:

```sh
export RELAY_EVIDENCE_IMAGE='your-registry/relay-tests@sha256:<64-hex-image-digest>'
```

The image must be pinned by digest. Containers have no network, a read-only root
filesystem, dropped capabilities, no privilege escalation and mounts only for the
temporary checkout, collector (read-only) and results. Redis credentials are never
mounted or forwarded. Timed-out containers are removed. When an image is configured,
ordinary toolgate commands also use it.

For trusted local fixtures, `RELAY_EVIDENCE_ALLOW_NATIVE=1` explicitly permits native
subprocess execution. This mode is recorded in the receipt and does not provide
filesystem isolation. Evidence execution still requires either an image or explicit
native permission. Removing signing does not remove this execution boundary.

Configure the approved plan's commands, or the toolgate's local fallback:

```toml
[commands]
acceptance_test = "python -m pytest -q {test_paths}"
evidence = "python -m pytest -q --hypothesis-seed=0"
# setup = "<offline bootstrap using dependencies available in the image>"
```

`evidence` is an argv prefix, not a shell program. Relay adds its collector and
exact selectors. Omit the Hypothesis option for projects using exhaustive tests
without Hypothesis. With generated properties, commit bounded settings in the
property tests and use a recorded fixed seed. Checks must name files or pytest
nodes, not directories/options. Keep property and acceptance checks in separate
files. Adding another test runner requires another real result adapter; do not
simulate pytest output.

Before starting a protocol-2 engagement, run `relay doctor --evidence` from the
project directory. This checks the launch environment for an execution mode and
validates the configured image digest format. It does not run a container or prove
that the project's toolchain works. Restart the toolgate after environment changes;
checking your shell does not change an already-running worker's environment.

## Inspect, repeat and evaluate

```sh
relay evidence I1.S1
relay evidence I1.S1 --out story-evidence.json
relay evidence-rerun I1.S1 --out repeated-receipt.json
relay audit
relay export --out ledger.jsonl
make evaluate
```

`evidence` includes source references, the original questions, receipts, missing
answers and recorded story-specific token/cost/time metrics. Shared iteration work
is excluded from those metrics. `status` includes each expectation story's status.
`audit` additionally checks projected evidence integrity.

`evidence-rerun` repeats the latest recorded recipe at its recorded commit on
the current toolgate host. It requires the execution setup, but no signing keys.
It writes a separate execution result, makes no model calls and does not modify the
ledger or silently recertify the story. Its result proves the recorded candidate,
not an uncommitted working tree.

`make evaluate` runs deterministic guard scenarios and a real CLI delivery fixture,
writing `.relay/evaluation.xml` with test outcomes and elapsed times. The fixture's
buggy implementation passes an acceptance example, fails an exhaustive property
check, is repaired through rework and then earns a successful execution receipt and
analyst acceptance. These tests call no models. They validate protocol enforcement;
they do not estimate a production model's false-completion rate.

## Compatibility and migration

The event contract is version 2; delivery protocol is separately declared on each
roadmap. Historical roadmaps with no `protocol_version` retain legacy semantics
and are shown as `legacy_unverified`. No historical receipt or positive property
answer is manufactured. Existing optional envelope routing remains accepted when
absent; supplied routing must agree with its recorded request.

A newly approved protocol-2 roadmap creates fresh story versions and clears prior
completion credit for its stories. Use that explicit replan to migrate an existing
engagement or amend expectations/questions. Old events remain available for audit.
There is no silent in-place weakening of prepared expectations or property questions.
Historical inconsistent evidence can now be flagged during replay/audit; no ledger
entries are rewritten.

The implemented adapter and fault-injection tests are exercised with local native
fixtures. Docker isolation needs a live daemon and provisioned image to validate in
the target deployment; no container execution is claimed by those native tests.

Historical `signature` fields remain readable for ledger compatibility but are ignored.
New execution receipts do not contain signatures.
