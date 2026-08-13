# Relay Protocol

**GENERATED from `contract/relay-contract.yaml` — do not edit.**
Contract version 1, hash `2839da2ba86c3434d45186ce9828de17b3766626394b40692f16ba306d67fe86`.

## Roles

- Assistants: interpreter, analyst, specifier, builder, reviewer, qa, security, sentinel
- Humans: owner
- Infra: coordinator, toolgate, system

## Edges

| Plane | Edge | Message types |
|---|---|---|
| chat | `coordinator>owner` | `chat.progress` |
| chat | `interpreter>owner` | `chat.checkpoint`, `chat.pr_link`, `chat.question`, `chat.result`, `chat.roadmap_proposed` |
| chat | `owner>interpreter` | `chat.answer`, `chat.decision`, `chat.feedback`, `chat.instruction`, `chat.problem` |
| plan | `coordinator>interpreter` | `control.pause`, `control.resume`, `plan.iteration_ready`, `plan.owner_decision_needed`, `plan.roadmap_rejected`, `plan.stall_alert`, `plan.story_done` |
| plan | `interpreter>coordinator` | `plan.iteration_aborted`, `plan.iteration_started`, `plan.pr_approved`, `plan.roadmap_committed` |
| work | `analyst>interpreter` | `work.question_raised`, `work.recon_report`, `work.stories_ready` |
| work | `builder>coordinator` | `work.built`, `work.error` |
| work | `coordinator>analyst` | `control.pause`, `control.resume`, `work.recon_requested` |
| work | `coordinator>builder` | `control.pause`, `control.resume`, `work.build_requested`, `work.rework_requested` |
| work | `coordinator>specifier` | `control.pause`, `control.resume`, `work.judgement_requested`, `work.spec_requested` |
| work | `interpreter>analyst` | `work.analysis_requested`, `work.answers` |
| work | `specifier>coordinator` | `work.acceptance_verdict`, `work.error`, `work.spec_ready` |
| gate | `coordinator>qa` | `control.pause`, `control.resume`, `gate.requested` |
| gate | `coordinator>reviewer` | `control.pause`, `control.resume`, `gate.requested` |
| gate | `coordinator>security` | `control.pause`, `control.resume`, `gate.requested` |
| gate | `qa>coordinator` | `gate.verdict` |
| gate | `reviewer>coordinator` | `gate.verdict` |
| gate | `security>coordinator` | `gate.verdict` |
| run | `coordinator>toolgate` | `run.requested` |
| run | `toolgate>coordinator` | `run.completed` |
| control | `analyst>sentinel` | `control.ack` |
| control | `builder>sentinel` | `control.ack` |
| control | `coordinator>sentinel` | `control.pause`, `control.resume` |
| control | `interpreter>sentinel` | `control.ack` |
| control | `qa>sentinel` | `control.ack` |
| control | `reviewer>sentinel` | `control.ack` |
| control | `security>sentinel` | `control.ack` |
| control | `sentinel>analyst` | `control.correction` |
| control | `sentinel>builder` | `control.correction` |
| control | `sentinel>interpreter` | `control.correction`, `sentinel.escalation` |
| control | `sentinel>qa` | `control.correction` |
| control | `sentinel>reviewer` | `control.correction` |
| control | `sentinel>security` | `control.correction` |
| control | `sentinel>specifier` | `control.correction` |
| control | `specifier>sentinel` | `control.ack` |
| system | `analyst>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `builder>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `coordinator>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `interpreter>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `owner>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `qa>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `reviewer>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `security>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `sentinel>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `specifier>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |
| system | `toolgate>system` | `system.contract_upgraded`, `system.dlq_routed`, `system.gap_detected`, `system.runner_session_started`, `system.worker_error`, `system.worker_started`, `system.worker_stopped` |

## Message types

One JSON Schema per type lives in `contract/schema/`.

| Type | Plane | Required payload fields |
|---|---|---|
| `chat.answer` | chat | `question_id`, `answers` |
| `chat.checkpoint` | chat | `kind`, `subject_id`, `gate_id`, `summary` |
| `chat.decision` | chat | `gate_id`, `decision` |
| `chat.feedback` | chat | `text` |
| `chat.instruction` | chat | `text` |
| `chat.pr_link` | chat | `iteration_id`, `pr_url` |
| `chat.problem` | chat | `text` |
| `chat.progress` | chat | `iteration_id`, `behaviours_done`, `behaviours_total` |
| `chat.question` | chat | `question_id`, `questions` |
| `chat.result` | chat | `text` |
| `chat.roadmap_proposed` | chat | `roadmap`, `narrative`, `gate_id` |
| `control.ack` | control | `finding_id` |
| `control.correction` | control | `finding_id`, `subject_event_id`, `rule_id`, `required_remedy` |
| `control.pause` | control | `role`, `reason` |
| `control.resume` | control | `role` |
| `gate.requested` | gate | `gate_id`, `gate`, `subject_kind`, `subject_id`, `commit_sha`, `base_sha` |
| `gate.verdict` | gate | `gate_id`, `verdict`, `findings` |
| `plan.iteration_aborted` | plan | `iteration_id`, `reason` |
| `plan.iteration_ready` | plan | `iteration_id`, `summary` |
| `plan.iteration_started` | plan | `iteration_id` |
| `plan.owner_decision_needed` | plan | `gate_id`, `subject_id`, `reason` |
| `plan.pr_approved` | plan | `iteration_id`, `gate_id` |
| `plan.roadmap_committed` | plan | `roadmap`, `intake` |
| `plan.roadmap_rejected` | plan | `reasons` |
| `plan.stall_alert` | plan | `subject_id`, `waiting_on`, `since_ts` |
| `plan.story_done` | plan | `story_id`, `summary` |
| `run.completed` | run | `run_id`, `kind`, `commit_sha`, `exit_code`, `duration_s`, `output_digest` |
| `run.requested` | run | `run_id`, `kind`, `commit_sha` |
| `sentinel.escalation` | control | `role`, `finding_ids`, `reason` |
| `system.contract_upgraded` | system | `old_hash`, `new_hash` |
| `system.dlq_routed` | system | `reason` |
| `system.gap_detected` | system | `expected_seq`, `observed_seq` |
| `system.runner_session_started` | system | `role`, `session_ref` |
| `system.worker_error` | system | `role`, `kind`, `detail` |
| `system.worker_started` | system | `role`, `host`, `pid`, `worker_version`, `contract_hash` |
| `system.worker_stopped` | system | `role`, `host`, `pid` |
| `work.acceptance_verdict` | work | `behaviour_id`, `verdict`, `run_id` |
| `work.analysis_requested` | work | `problem` |
| `work.answers` | work | `question_id`, `answers` |
| `work.build_requested` | work | `behaviour_id`, `spec_commit_sha`, `test_paths` |
| `work.built` | work | `behaviour_id`, `story_id`, `iteration_id`, `commit_sha`, `attempt` |
| `work.error` | work | `kind`, `detail` |
| `work.judgement_requested` | work | `behaviour_id`, `commit_sha`, `run_id` |
| `work.question_raised` | work | `question_id`, `questions` |
| `work.recon_report` | work | `brief_path`, `risk_areas` |
| `work.recon_requested` | work | `commit_sha` |
| `work.rework_requested` | work | `behaviour_id`, `attempt`, `findings` |
| `work.spec_ready` | work | `behaviour_id`, `test_paths`, `commit_sha`, `touches` |
| `work.spec_requested` | work | `behaviour_id`, `iteration_id`, `ac_text`, `kind`, `base_sha` |
| `work.stories_ready` | work | `stories` |
