# Relay Protocol

**GENERATED from `contract/relay-contract.yaml` — do not edit.**
Contract version 1, hash `383318bda3428fe4d346800895154a4d85ee43383710aa05620500056c277060`.

## Roles

- Assistants: interpreter, analyst, specifier, builder, reviewer, qa, security, sentinel
- Humans: owner
- Infra: coordinator, toolgate, system

## Edges

| Plane | Edge | Message types |
|---|---|---|
| chat | `coordinator>owner` | `progress.reported` |
| chat | `interpreter>owner` | `checkpoint.reached`, `pr.announced`, `questions.asked`, `roadmap.proposed`, `update.shared` |
| chat | `owner>interpreter` | `answers.given`, `decision.made`, `feedback.given`, `instruction.given`, `problem.stated` |
| plan | `coordinator>interpreter` | `decision.requested`, `iteration.finished`, `pause.ordered`, `pr.opened`, `resume.ordered`, `roadmap.rejected`, `stall.detected`, `story.completed` |
| plan | `interpreter>coordinator` | `iteration.aborted`, `iteration.started`, `pr.approved`, `roadmap.committed` |
| work | `analyst>interpreter` | `questions.raised`, `recon.completed`, `stories.written` |
| work | `builder>coordinator` | `behaviour.built`, `error.raised` |
| work | `coordinator>analyst` | `pause.ordered`, `recon.requested`, `resume.ordered` |
| work | `coordinator>builder` | `build.requested`, `pause.ordered`, `resume.ordered`, `rework.requested` |
| work | `coordinator>specifier` | `judgement.requested`, `pause.ordered`, `resume.ordered`, `spec.requested` |
| work | `interpreter>analyst` | `analysis.requested`, `answers.relayed` |
| work | `specifier>coordinator` | `acceptance.judged`, `error.raised`, `spec.written` |
| gate | `coordinator>qa` | `gate.requested`, `pause.ordered`, `resume.ordered` |
| gate | `coordinator>reviewer` | `gate.requested`, `pause.ordered`, `resume.ordered` |
| gate | `coordinator>security` | `gate.requested`, `pause.ordered`, `resume.ordered` |
| gate | `qa>coordinator` | `gate.judged` |
| gate | `reviewer>coordinator` | `gate.judged` |
| gate | `security>coordinator` | `gate.judged` |
| run | `coordinator>toolgate` | `run.requested` |
| run | `toolgate>coordinator` | `run.completed` |
| control | `analyst>sentinel` | `correction.acknowledged` |
| control | `builder>sentinel` | `correction.acknowledged` |
| control | `coordinator>sentinel` | `pause.ordered`, `resume.ordered` |
| control | `interpreter>sentinel` | `correction.acknowledged` |
| control | `qa>sentinel` | `correction.acknowledged` |
| control | `reviewer>sentinel` | `correction.acknowledged` |
| control | `security>sentinel` | `correction.acknowledged` |
| control | `sentinel>analyst` | `correction.issued` |
| control | `sentinel>builder` | `correction.issued` |
| control | `sentinel>interpreter` | `correction.issued`, `escalation.raised` |
| control | `sentinel>qa` | `correction.issued` |
| control | `sentinel>reviewer` | `correction.issued` |
| control | `sentinel>security` | `correction.issued` |
| control | `sentinel>specifier` | `correction.issued` |
| control | `specifier>sentinel` | `correction.acknowledged` |
| system | `analyst>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `builder>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `coordinator>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `interpreter>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `owner>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `qa>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `reviewer>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `security>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `sentinel>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `specifier>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |
| system | `toolgate>system` | `contract.upgraded`, `gap.detected`, `message.quarantined`, `session.started`, `worker.failed`, `worker.started`, `worker.stopped` |

## Message types

One JSON Schema per type lives in `contract/schema/`.

| Type | Plane | Required payload fields |
|---|---|---|
| `acceptance.judged` | work | `behaviour_id`, `verdict`, `run_id` |
| `analysis.requested` | work | `problem` |
| `answers.given` | chat | `question_id`, `answers` |
| `answers.relayed` | work | `question_id`, `answers` |
| `behaviour.built` | work | `behaviour_id`, `story_id`, `iteration_id`, `commit_sha`, `attempt` |
| `build.requested` | work | `behaviour_id`, `spec_commit_sha`, `test_paths` |
| `checkpoint.reached` | chat | `kind`, `subject_id`, `gate_id`, `summary` |
| `contract.upgraded` | system | `old_hash`, `new_hash` |
| `correction.acknowledged` | control | `finding_id` |
| `correction.issued` | control | `finding_id`, `subject_event_id`, `rule_id`, `required_remedy` |
| `decision.made` | chat | `gate_id`, `decision` |
| `decision.requested` | plan | `gate_id`, `subject_id`, `reason` |
| `error.raised` | work | `kind`, `detail` |
| `escalation.raised` | control | `role`, `finding_ids`, `reason` |
| `feedback.given` | chat | `text` |
| `gap.detected` | system | `expected_seq`, `observed_seq` |
| `gate.judged` | gate | `gate_id`, `verdict`, `findings` |
| `gate.requested` | gate | `gate_id`, `gate`, `subject_kind`, `subject_id`, `commit_sha`, `base_sha` |
| `instruction.given` | chat | `text` |
| `iteration.aborted` | plan | `iteration_id`, `reason` |
| `iteration.finished` | plan | `iteration_id`, `summary` |
| `iteration.started` | plan | `iteration_id` |
| `judgement.requested` | work | `behaviour_id`, `commit_sha`, `run_id` |
| `message.quarantined` | system | `reason` |
| `pause.ordered` | control | `role`, `reason` |
| `pr.announced` | chat | `iteration_id`, `pr_url` |
| `pr.approved` | plan | `iteration_id`, `gate_id` |
| `pr.opened` | plan | `iteration_id`, `pr_url` |
| `problem.stated` | chat | `text` |
| `progress.reported` | chat | `iteration_id`, `behaviours_done`, `behaviours_total` |
| `questions.asked` | chat | `question_id`, `questions` |
| `questions.raised` | work | `question_id`, `questions` |
| `recon.completed` | work | `brief_path`, `risk_areas` |
| `recon.requested` | work | `commit_sha` |
| `resume.ordered` | control | `role` |
| `rework.requested` | work | `behaviour_id`, `attempt`, `findings` |
| `roadmap.committed` | plan | `roadmap`, `intake` |
| `roadmap.proposed` | chat | `roadmap`, `narrative`, `gate_id` |
| `roadmap.rejected` | plan | `reasons` |
| `run.completed` | run | `run_id`, `kind`, `commit_sha`, `exit_code`, `duration_s`, `output_digest` |
| `run.requested` | run | `run_id`, `kind`, `commit_sha` |
| `session.started` | system | `role`, `session_ref` |
| `spec.requested` | work | `behaviour_id`, `iteration_id`, `ac_text`, `kind`, `base_sha` |
| `spec.written` | work | `behaviour_id`, `test_paths`, `commit_sha`, `touches` |
| `stall.detected` | plan | `subject_id`, `waiting_on`, `since_ts` |
| `stories.written` | work | `stories` |
| `story.completed` | plan | `story_id`, `summary` |
| `update.shared` | chat | `text` |
| `worker.failed` | system | `role`, `kind`, `detail` |
| `worker.started` | system | `role`, `host`, `pid`, `worker_version`, `contract_hash` |
| `worker.stopped` | system | `role`, `host`, `pid` |
