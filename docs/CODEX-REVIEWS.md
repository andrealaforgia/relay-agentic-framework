# Claude implementation with Codex review

Relay can keep Claude for implementation and the existing reviewer while running
an additional, independent Codex reviewer in parallel.
The existing `code_review` gate runs after an implemented behaviour's acceptance
check passes, before that behaviour can complete. This includes story and iteration
integration behaviours. A failed gate sends findings back for rework and another
review. It works with both roadmap protocols and does not require the pytest
evidence adapter.

## Enable the optional parallel reviewer

Keep the existing `[roles.reviewer]` section on Claude. If you previously switched
it to Codex, restore `runner = "claude"` and its Claude model (for example `sonnet`).
Install and authenticate the Codex CLI on the worker host, then add this separate
section to `.relay/relay.toml`:

```toml
[roles.codex_reviewer]
runner = "codex"
sandbox = "read-only"
effort = "high"
# Optionally select a model available to your Codex CLI:
# model = "<your Codex model>"
```

With no model specified, Codex uses its CLI default. Claude model aliases such as
`sonnet` are not Codex model names. Relay does not enforce a dollar budget for
Codex turns; turn deadlines and bounded correction attempts still apply.

Add the `codex_review` entry under `per_behaviour` in `.relay/gates.yaml`, keeping
all existing entries. A typical configuration is:

```yaml
per_behaviour:
  - { gate: code_review, role: reviewer, timeout_s: 1800, retries: 1 }
  - { gate: codex_review, role: codex_reviewer, timeout_s: 1800, retries: 1 }
  - { gate: test_design, role: qa, timeout_s: 1800, retries: 1 }
```

Both reviews are dispatched for the same candidate without waiting for each other.
Both must pass before completion; a blocking verdict from either triggers rework.
Their findings are tracked separately and both reports reach the interpreter.
The gate is optional to enable, but once enabled it is a completion requirement.

Close the old chat and restart from the project directory:

```sh
relay down
relay up
relay chat --new
```

`relay up` starts the extra worker automatically because the policy names it.
Use `relay tail codex_reviewer` for its activity, `relay tail reviewer` for the
existing reviewer and `relay watch --events` for both reports. A missing Codex
executable or failed review never becomes an automatic pass. Already-completed
behaviours are not retroactively reviewed. To disable the extra gate, remove its
policy entry and restart the workers.

Switching the existing reviewer itself to `runner = "codex"` remains supported,
but is an alternative to running two independent reviewers.

## What is delivered

Codex reads a detached checkout at the exact requested commit and reviews the diff
from `base_sha` to `commit_sha`. Each structured review starts with a fresh session.
It returns a JSON verdict with findings, a summary and explicit review limitations.
The worker validates that result and publishes `gate.judged` to the coordinator.
Codex does not need network access to Redis or permission to edit the checkout.

The worker forwards the full review, commit range and original verdict event ID to
the interpreter as `update.shared`. Both passes and failures are delivered. Reports
include the model recorded for the turn, or `unknown` when unavailable. Restarting
between verdict publication and report delivery resumes delivery without asking the
model to review again. The interpreter presents findings without weakening them;
the coordinator still decides whether the gate releases or blocks work.

A pass cannot carry major/blocking findings, and a fail requires actionable blocking
findings. Invalid JSON, a wrong gate ID, missing review limitations or a failed runner
cannot become a structured verdict. Corrections are bounded; exhaustion is reported
as a worker failure. Existing finding-disposition rules still apply on re-review.

This makes delivery and gate enforcement deterministic. It cannot guarantee that a
model notices every defect or judges every finding correctly. The reviewer must say
what it inspected, what it actually executed and what remains unverified.
