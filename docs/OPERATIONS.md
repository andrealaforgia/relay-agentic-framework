# Operating Relay

## Single machine (the default)

```bash
relay up ~/code/myproject        # auto-init + local redis (AOF) + workers
cd ~/code/myproject
relay chat                       # terminal 1 — the Interpreter, live
relay watch                      # terminal 2 — activity board + event feed
relay up . --tmux                # alternative: one tmux session, watch + per-role tails
```

Stopping and resuming: `relay down` kills processes only — all state is the
ledger. `relay up .` resumes exactly, including mid-behaviour. `kill -9`, a
reboot, or a crashed worker resume the same way (PEL redelivery + replay).

Diagnostics, in the order to reach for them:

| Question | Command |
|---|---|
| Is anything alive / working / stuck? | `relay status`, `relay watch`, `/status` in chat |
| What is a specific assistant doing? | `relay tail <role>` (streamed tool calls, turn durations) |
| Did anything get dead-lettered? | `relay status` (dlq count), then the `relay:<swarm>:dlq` stream |
| Is the ledger internally consistent? | `relay audit` |
| What has this engagement cost, and where? | `relay costs`, `relay costs --by-behaviour` |
| What is it spending right now? | `relay watch --tokens` |
| The Interpreter has not reacted to mail | it is woken automatically; `RELAY_STOP_WAIT_S=0` disables the hook wait |
| A role is burning too much | lower `effort` / `max_budget_usd` for it in `.relay/relay.toml` |
| Preflight after changes | `relay doctor` |
| Freeze a misbehaving role | `relay pause <role>` / `relay resume <role>` (work parks in the PEL) |

## Multiple machines

The hub is Redis; workers go where their work is. Every worker host needs:
its own clone of the target project, this framework installed
(`uv tool install relay-agentic-framework`), and the runner CLI (`claude` /
`codex`) authenticated.

1. **Network**: put hub and hosts on a tailnet (recommended — WireGuard
   encryption and device identity for free). Bind Redis to the tailnet
   interface only; never 0.0.0.0, never a forwarded port.
2. **Credentials**: on the hub, apply per-role ACLs:
   `relay acl --swarm acme --out acls.sh` → run its lines in redis-cli →
   `ACL SAVE` → delete the file. Each host gets only its roles' credentials
   via `REDIS_USERNAME`/`REDIS_PASSWORD` env.
3. **Start subsets per host** (the union must cover all roles exactly once):
   ```bash
   # beefy build box
   REDIS_HOST=hub.tailnet relay up ~/clones/myproject --swarm acme \
       --roles builder,specifier,toolgate
   # anywhere
   REDIS_HOST=hub.tailnet relay up ~/clones/myproject --swarm acme \
       --roles coordinator,analyst,reviewer,qa,security
   # your laptop: just the conversation
   REDIS_HOST=hub.tailnet relay chat --swarm acme
   ```
4. **Code locality**: writing roles (specifier, builder) and the toolgate
   need the clone they work in kept current — messages pin exact SHAs, and
   the toolgate refuses a SHA it doesn't have. A `git fetch` loop or push
   webhook per host suffices; gates always work in detached worktrees at the
   pinned SHA, so host checkout state never affects verdicts.

**Failover**: consumer groups make roles portable. If a host dies, start the
same role anywhere (same swarm, same env): the newcomer XAUTOCLAIMs the dead
consumer's pending work and continues. Nothing is reassigned by hand.

## The rules that keep operations safe

- No timeout ever auto-approves; human gates never expire (fail closed).
- Nothing is dropped silently: off-contract input and give-ups land in the
  DLQ **and** as `message.quarantined` on the ledger.
- The audit (`relay audit`) re-validates every entry: it proves the ledger
  could only have been produced by a correctly-enforcing publisher — or
  shows exactly where it wasn't.
- Claude roles run with `--dangerously-skip-permissions` by default: headless
  denial is a silent stall, and the real guardrails are the contract-validated
  bus, the deterministic gates, and the audit. Set
  `skip_permissions = false` per role in `relay.toml` to use the generated
  permission profiles instead. Codex roles run sandboxed (`read-only` for
  gates, `workspace-write` for specifier/builder).
