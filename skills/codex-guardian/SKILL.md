---
name: codex-guardian
description: Use when Codex reconnects, loses streams, stalls, compacts poorly, loops without progress, or needs recoverable checkpoints for long work. Trigger on messages such as "stream disconnected before completion", "failed to send websocket request", "responses_websocket", "unknown conversation", "websocket closed before response.completed", or "Error running remote compact task".
---

# Codex Guardian

Codex Guardian turns unreliable long-running Codex work into smaller recoverable phases. It combines three layers:

1. A project-local skill workflow for diagnosis, checkpointing, and recovery.
2. A watcher and diagnostic script for quick log summaries.
3. A fuller CLI for preflight checkpoints, resume prompts, and guarded command execution.

## Workflow

Use the smallest layer that fits the problem:

- **Install or activation check**: run `scripts/codex_guardian.py install-check`. It verifies required scripts, references, and the redacted fixture corpus, not just `SKILL.md`.
- **Early failure detection**: run `scripts/codex_guardian.py watch --once` before or during risky work. Add `--check-reachability` when local endpoint reachability should stop the slice before broader work continues. Add `--check-service-status` when degraded upstream status should stop the slice before broader work continues.
- **Recovery bundle**: add `--recovery-report --project .` to `watch` when a stream may die. Use `--doctor` instead when a triggered watcher bundle should include status, reachability, upstream service status, environment, and connection triage files.
- **Full recovery bundle now**: run `scripts/codex_guardian.py recover-now --project .` whenever you need the full doctor-grade bundle immediately, even without a fresh actionable watch result.
- **Manual recovery bundle**: run `scripts/codex_guardian.py bundle --project .` when you need the lower-level bundle command. Add `--doctor` when the bundle should include status, reachability, upstream service status, environment, and connection triage files.
- **Connection health split**: run `scripts/codex_guardian.py health --hours 1` to separate auth/session failure, app-state churn, transport, compaction, and no-progress failures. Add `--check-reachability` to include a separate live Codex endpoint probe in the same report, and `--check-service-status` to include upstream status evidence.
- **Local reachability check**: run `scripts/codex_guardian.py reachability` to test DNS and HTTP/TLS reachability for the Codex endpoint without reading logs.
- **Upstream status check**: run `scripts/codex_guardian.py service-status` to check the configured Statuspage-style endpoint without treating a failed check as proof of an outage.
- **Connection triage boundary**: run `scripts/codex_guardian.py connection-triage --project . --hours 1` when you need local recovery actions, recovery attention, and an explicit direct-fix boundary. Add `--check-reachability` when the same boundary report needs live DNS and HTTP/TLS evidence, and `--check-service-status` when it needs upstream status evidence.
- **Current recovery state**: run `scripts/codex_guardian.py status --project . --hours 1` to summarize health, checkpoint, latest bundle, restart marker, and post-restart state.
- **One-step recovery decision**: run `scripts/codex_guardian.py doctor --project . --hours 1` when you want the health classification, overdue-checkpoint check, recovery bundle, and next local actions in one report. Add `--check-reachability` when logs are quiet but Codex still cannot connect. Add `--check-service-status` when upstream service status should affect the same decision. Add `--mark-restart` when you are about to follow a restart recommendation and want the post-restart check seeded automatically.
- **Fast diagnosis**: run `scripts/diagnose_codex_streams.py` or `scripts/codex_guardian.py diagnose`.
- **Fixture soak test**: run `scripts/codex_guardian.py self-test` before trusting a changed install.
- **Local validation**: run `scripts/codex_guardian.py validate-skill` to check required files and frontmatter when PyYAML is unavailable.
- **Clean package**: run `scripts/codex_guardian.py package --output-dir dist` before sharing or installing elsewhere. It fails before packaging if a required runtime, reference, or fixture file is missing.
- **Durable preflight**: run `scripts/codex_guardian.py preflight` before substantial work.
- **Automatic long-task preflight**: run `scripts/codex_guardian.py auto-preflight --estimated-minutes N` before work where the duration is uncertain. It writes preflight only when the estimate crosses the threshold.
- **Task checkpointing or recovery**: run `scripts/codex_guardian.py checkpoint` and `scripts/codex_guardian.py resume-prompt`.
- **Guarded execution**: run `scripts/codex_guardian.py wrap -- ...` around a non-interactive command.

Always prefer read-only diagnosis first. Do not delete, truncate, or rewrite `.codex` logs, sessions, databases, or auth files.

Codex Guardian cannot directly patch Codex app internals, OpenAI backend availability, auth/session bugs, WebSocket transport, or local network failures. Treat it as a recovery and decision layer: it preserves work, classifies likely failure type, creates resume material, checks local reachability and upstream status when asked, points auth/session failures to reauth, and tells you when a restart is the right next local action.

When the user asks whether Guardian can "fix" Codex connection problems, be precise: it can automate local recovery decisions and state preservation, but a true transport/app/backend fix must happen in Codex or the surrounding system. Use `connection-triage` to show the local action boundary and use `doctor` as the highest-level local recovery command.

## Before Substantial Work

Create a durable phase file before editing, running a long command, or loading broad context:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py preflight \
  --project . \
  --task "Fix reconnect recovery docs" \
  --next-action "Edit only SKILL.md and run tests" \
  --touched skills/codex-guardian/SKILL.md
```

`preflight` writes `.codex-guardian/current.json` with `phase: preflight_done`, the next action, touched files, a `git status: clean` or `git status: dirty` fact when available, touched-file existence facts, outside-project warnings, and a 15-minute checkpoint deadline.

Use `auto-preflight` when a task runner or agent startup path has an estimate but should not write checkpoints for trivial work:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py auto-preflight \
  --project . \
  --task "Fix reconnect recovery docs" \
  --next-action "Edit one named file and verify it" \
  --estimated-minutes 20 \
  --touched skills/codex-guardian/SKILL.md
```

It writes the same `preflight_done` checkpoint when the estimate is at or above `--threshold-minutes`, defaulting to 10. For shorter tasks it reports that no checkpoint was created and leaves the current checkpoint untouched. Use `--force` when a short task still has high recovery cost.

For long tasks, work in 10 to 15 minute slices. Each slice must end with one of:

- a new checkpoint naming the phase reached,
- a verified test or command result,
- a recovery prompt if the stream or command failed,
- a stop report if no file edit or verified result happened.

To detect an empty work slice, record a fingerprint at the start and compare it at the end:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --task "Fix reconnect recovery docs" \
  --phase write_started \
  --next-action "Edit SKILL.md" \
  --touched skills/codex-guardian/SKILL.md \
  --fingerprint

python3 skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --task "Fix reconnect recovery docs" \
  --phase write_done \
  --next-action "Run tests or report no progress" \
  --touched skills/codex-guardian/SKILL.md \
  --fingerprint \
  --compare-fingerprint
```

If the fingerprint is unchanged, the checkpoint status becomes `no_progress`.

## Failure Handling

When a stream or reconnect failure appears:

1. Capture evidence with `diagnose`.
2. Run `health --hours 1` when the failure mode is unclear.
3. Check whether files changed with the smallest safe command for the project, usually `git status --short`.
4. If task state is unclear, create or update a checkpoint before doing more work.
5. Generate a resume prompt or `bundle --project .` so the next Codex run knows exactly what to read, what not to reread, and what to verify first.
6. Continue in a smaller slice.

## Restart Decision Rules

Use `health` to avoid treating every failure as a network problem:

- `issue_type: auth_session`: authentication, authorization, token, or session errors. Checkpoint active work, sign in or refresh the Codex session, then retry. Restart the app only if it still holds stale auth after reauth.
- `issue_type: app_state`: `unknown_conversation`, `turn/start` timeout, or MCP timeout without transport errors. A single event means checkpoint and retry one small action; repeated events set `restart_codex_now` and mean restart Codex after preserving state.
- `issue_type: transport`: stream disconnects, WebSocket send failures, idle timeouts, closed streams, broken pipes, or explicit `responses_websocket` failures. Verify file state, checkpoint, and resume from a smaller prompt before retrying broad work.
- `issue_type: mixed`: app-state churn and transport failures are both present. Write a recovery bundle, checkpoint active work, then restart Codex after preserving state.
- `issue_type: compaction`: remote compact or compact endpoint failures. Stop adding context and resume in a smaller slice.
- `issue_type: no_progress`: repeated rereads without verified edits. Name one next file or behavior, checkpoint it, and stop if the next slice makes no verified progress.

Read `restart_codex_now` as the urgent repeated app-state-only rule. Read `restart_recommended` plus `restart_timing` for the broader local restart decision; mixed failures should show `restart_recommended: true` and `restart_timing: after_state_preserved`. The health JSON also includes `restart_decision` with the exact decision, first action, timing, marker recommendation, and state-preservation requirement.

Use `references/failure-taxonomy.md` when classifying an unfamiliar failure.

## Checkpoint Pattern

For substantial tasks, create a checkpoint at each durable boundary:

- `preflight_started`
- `preflight_done`
- `write_started`
- `write_done`
- `validation_started`
- `validation_done`
- `final_report_started`
- `completed`
- `blocked`

Each checkpoint should name:

- the current task,
- the current phase,
- touched files,
- verified facts,
- next action,
- blockers or unknowns.

Example:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --phase preflight_done \
  --task "Fix reconnect recovery docs" \
  --touched README.md \
  --verified "No source files changed yet" \
  --next-action "Edit only README.md and run markdown check"
```

Use `preflight` for the first checkpoint because it automatically records git state. Use `checkpoint` after each slice to record the phase transition and validation result.

## Recovery Prompts

Generate a resume prompt after a failure:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py resume-prompt --project .
```

The prompt should direct Codex to:

- trust the checkpoint over stale chat assumptions,
- verify file state before editing,
- read only the recorded touched files first,
- continue from the recorded phase,
- stop with a report if the checkpoint contradicts current files.

Use `references/recovery-prompts.md` for reusable prompt shapes.

## Log Watch And Diagnosis

Run a one-shot watch before risky work:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py watch --once --hours 1
```

`watch` reads `~/.codex/logs_2.sqlite` and Codex desktop app logs. It exits with status `1` when it sees an actionable failure pattern or active-checkpoint problem so the caller can stop and recover before continuing. Actionable patterns include high-severity stream, auth/session, or compaction failures, no-progress loops, repeated app-state events that set `restart_codex_now`, unreadable or overdue active checkpoints, failed local reachability probes when `--check-reachability` is set, and degraded upstream status when `--check-service-status` is set. Failed service-status checks stay `unknown` and do not make watch actionable by themselves. Watch reports and recovery bundles include the same health block that explains the decision, including in `diagnosis.md`.

To write a recovery bundle:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py watch \
  --once \
  --hours 1 \
  --project . \
  --recovery-report \
  --mark-restart \
  --task "Recover active task" \
  --touched README.md
```

The bundle is written under `.codex-guardian/recovery/` and includes `README.md`, `manifest.json`, a diagnosis report, redacted event samples, the current checkpoint when present, and a resume prompt.
When `--task` is set, `watch` first writes a `preflight_done` checkpoint with git status, touched-file facts, and a checkpoint deadline. If `watch` creates a recovery bundle, the bundle includes that checkpoint. If the current checkpoint is unreadable or overdue, the watch bundle preserves that attention in its README, diagnosis, and resume prompt.
Use `--doctor` when the watcher should create the full recovery bundle as soon as a failure appears. It implies a recovery bundle and adds `doctor.md`, `status.md`, `reachability.md`, `service-status.md`, `environment.md`, and `connection-triage.md` with matching JSON files.
When `--check-reachability` makes `watch` actionable and `--recovery-report` is set, the bundle includes `reachability.md` and `reachability.json`.
When `--check-service-status` makes `watch` actionable and `--recovery-report` is set, the bundle includes `service-status.md`, `service-status.json`, `connection-triage.md`, and `connection-triage.json`.
If project recovery status is what makes `watch` actionable, the report includes the same `status` object, and the recovery bundle includes `status.md`, `status.json`, `connection-triage.md`, and `connection-triage.json`.
When repeated app-state failures make `restart_codex_now` true and `--mark-restart` is set, `watch` also writes `.codex-guardian/restart-marker.json` so `post-restart` can verify the restart afterward. It does not mark restart for transport-only failures.
Restart markers written by `watch`, `bundle`, or `doctor` include the source command, issue type, restart timing, and restart reason from the health decision or post-restart status decision.

To write the same bundle on demand:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py recover-now \
  --project . \
  --hours 1 \
  --task "Recover active task" \
  --touched README.md
```

`recover-now` writes the full doctor-grade recovery bundle immediately, including status, reachability, service status, environment, connection triage, diagnosis, events, checkpoint when `--task` is set, and a resume prompt. It writes a restart marker when the health classifier recommends a restart unless `--no-mark-restart` is set.
Status, doctor, and connection-triage next actions prefer `recover-now` when they recommend a full recovery bundle.

Use `bundle --doctor` when you need the lower-level bundle command:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py bundle \
  --project . \
  --hours 1 \
  --doctor \
  --mark-restart \
  --task "Recover active task" \
  --touched README.md
```

With `--doctor`, `bundle` also writes `doctor.md`, `doctor.json`, `status.md`, `status.json`, `reachability.md`, `reachability.json`, `service-status.md`, `service-status.json`, `environment.md`, `environment.json`, `connection-triage.md`, and `connection-triage.json` into the bundle. The status files capture the current checkpoint, latest bundle, restart marker, post-restart state, and next actions for the resumed session. The reachability files capture DNS and HTTP/TLS evidence for the Codex endpoint at bundle time, from the current process network context. The service-status files capture the configured Statuspage-style upstream status and treat failed checks as `unknown`, not as proof of an outage. The environment files capture the Codex home, Python and platform details, Codex CLI presence, probe endpoint, and read-only log source evidence for `logs_2.sqlite` and desktop Codex logs. Use `--reachability-endpoint`, `--reachability-timeout`, `--reachability-dns-only`, `--service-status-endpoint`, and `--service-status-timeout` when the bundle needs specific probe targets. The JSON command response includes the same `status`, `reachability`, and `service_status` objects. The bundle preserves unreadable or overdue current-checkpoint attention in the README, diagnosis, status, doctor artifacts, and resume prompt. Its `resume-prompt.txt` tells the next session to open `status.md` first, then `doctor.md`. With `--task`, it first writes and bundles a `preflight_done` checkpoint. With `--mark-restart`, it writes a restart marker when the health classifier recommends restarting Codex or project status shows post-restart app-state instability.

To classify the current situation:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py health --hours 1
```

The report includes `restart_decision` plus the same structured `direct_fix_boundary` used by `connection-triage`: `3/10` direct-fix ceiling, `9/10` recovery-tooling ceiling, highest local recovery command, and the external repair reason. Add `--check-reachability` when you need the same report to include a live DNS and HTTP/TLS probe. The health `issue_type` remains log-derived; reachability appears separately and makes the command exit nonzero when the local probe fails. Add `--check-service-status` when the same report should include upstream service status; degraded upstream status makes `health` exit nonzero, while a failed status check remains `unknown`.

To check local DNS and HTTP/TLS reachability for the Codex path:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py reachability
```

`reachability` defaults to `https://chatgpt.com/backend-api/codex/responses`. It checks DNS before the HTTP/TLS probe and classifies DNS, TLS, reset, and timeout failures with the same transport family names used by `health`. A reachable endpoint does not prove the Codex app session is healthy; it only rules out the most direct local reachability failure from the current process network context. Use `--dns-only` for a resolver-only check.

To check the configured upstream service status endpoint:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py service-status
```

`service-status` defaults to the OpenAI Statuspage JSON endpoint. It reports `operational`, `degraded`, or `unknown`. A failed status check is `unknown`; use `reachability` and `health` before treating it as an upstream outage.

To show the local direct-fix boundary with optional live endpoint evidence:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py connection-triage \
  --project . \
  --hours 1 \
  --check-reachability
```

`connection-triage --check-reachability` keeps the health issue type log-derived, adds a separate reachability section, and exits nonzero when the local DNS or HTTP/TLS probe fails. Its report includes a structured `direct_fix_boundary` with a `3/10` direct-fix ceiling, a `9/10` recovery-tooling ceiling, the highest local recovery command, and the reason true transport/app/backend repair is outside Guardian's scope. It also includes an escalation packet naming the redacted evidence to preserve when the problem is outside Guardian's local recovery scope. Add `--check-service-status` to include upstream service evidence; degraded upstream status appears as `recovery_attention: upstream_degraded`, while a failed status check remains `unknown`.

To summarize the current project recovery state:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py status \
  --project . \
  --hours 1
```

`status` reports the current health, active checkpoint, unreadable or overdue checkpoint state, latest recovery bundle, restart marker, and post-restart state when a marker exists. It also includes `fresh_recovery_bundle_recommended` and ordered `next_actions`, so a resumed session can tell whether to inspect the checkpoint, create a fresh full bundle, restart Codex, or verify post-restart state next. Use it after reconnects or restarts when you need to know which Guardian artifact to open next.

To classify, preserve state, and get the next action plan:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py doctor \
  --project . \
  --task "Recover active task" \
  --touched README.md \
  --mark-restart \
  --hours 1
```

`doctor` writes a recovery bundle whenever health status, checkpoint state, or project recovery status needs attention. It exits `0` when no known failure pattern, checkpoint problem, or post-restart recovery problem is found, and `1` when it created a recovery plan for a detected issue.
When `--check-reachability` is provided, `doctor` also probes the Codex endpoint. Local DNS/TLS/HTTP failure counts as attention, and the bundle includes `reachability.md` and `reachability.json`.
When `--check-service-status` is provided, `doctor` also probes the configured upstream status endpoint. Degraded upstream status counts as attention and creates a recovery bundle; a failed status check remains `unknown` and does not prove an outage.
When `--task` is provided, `doctor` first writes a `preflight_done` checkpoint with git status, touched-file facts, and a checkpoint deadline. If `doctor` creates a recovery bundle, it includes that checkpoint in the bundle.
When `doctor` creates the bundle, it writes `doctor.md`, `doctor.json`, `status.md`, `status.json`, `reachability.md`, `reachability.json`, `service-status.md`, `service-status.json`, `environment.md`, `environment.json`, `connection-triage.md`, and `connection-triage.json`. The JSON command response includes the same `status` object as `status.json` and the same `service_status` object written into the bundle. When `bundle --doctor` creates the bundle, its JSON response includes the same `status`, `reachability`, and `service_status` objects written into the bundle. If the reachability probe fails, the ordered actions point you to `reachability.md` before retrying Codex. Open `status.md` for the current recovery state, then use `doctor.md` for the ordered local actions.
When `--mark-restart` is provided and the health assessment or project status recommends a restart, `doctor` also writes `.codex-guardian/restart-marker.json`; after restarting Codex, run `post-restart --project .` from the same project.

You can also write a marker manually before following a restart recommendation. Manual markers include structured restart decision metadata, so `post-restart` can carry the same decision context forward. After restart, verify from that marker:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py mark-restart --project .
python3 skills/codex-guardian/scripts/codex_guardian.py post-restart --project . --hours 1
```

`post-restart` exits `0` when post-marker Codex log activity exists and no app-state or transport errors are found. It exits `1` when app-state errors continued, transport errors remain, or no post-marker activity was seen. Transport errors include DNS, TLS, reset, timeout, and WebSocket transport families. Use `--since` only when you need to check from a manual timestamp instead of the project marker.
When a project marker is used, `post-restart` treats `--hours` as a minimum and expands the effective lookback if needed so the marker time is covered. Its JSON and markdown reports include the marker path, source command, issue type, restart timing, and restart decision fields.

Run the quick script:

```bash
python3 skills/codex-guardian/scripts/diagnose_codex_streams.py --hours 12
```

Or use the full CLI:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py diagnose --hours 12 --format markdown
```

The diagnosis reads local Codex logs and reports patterns such as:

- `responses_websocket` failures
- `stream disconnected before completion`
- `stream disconnected - retrying sampling request`
- `failed to send websocket request`
- auth/session failures such as `401 Unauthorized`, `authentication required`, or `session expired`
- `idle timeout waiting for websocket`
- `websocket closed by server before response.completed`
- startup prewarm TLS record alerts such as `BadRecordMac`
- `Error running remote compact task`
- `Received turn/... for unknown conversation`
- `turn/start` timeouts
- no-progress loops where the same files are reread without verified edits

The matcher ignores streamed `response.output_text.delta` content, even on a `responses_websocket` target, because assistant text can quote failure words without indicating a transport failure.

Public reports redact home paths, tokens, emails, conversation IDs, thread IDs, UUID-like IDs, and long opaque IDs. Before sharing any output publicly, apply `references/privacy-redaction.md`.

## Guarded Command Wrapper

Use `wrap` for non-interactive commands where losing output or state would be costly:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py wrap \
  --project . \
  --task "Run one bounded Codex exec task" \
  --touched README.md \
  -- codex exec "Perform the bounded task and stop with a concise report."
```

The wrapper creates an automatic `preflight_done` checkpoint with git status, touched-file facts, the command, and a checkpoint deadline before the command runs. It then captures exit status, writes a finish or failed checkpoint, and prints a recovery prompt when the command fails.

Do not use `wrap` as a substitute for fixing unstable network conditions. Treat it as state protection.

## Design Rules

- Keep public diagnostics generic. Do not include user names, private paths, transcripts, auth tokens, memory files, or proprietary project content.
- Prefer 10 to 15 minute task slices over bigger retries.
- Use `install-check` before assuming the skill is active in `~/.codex/skills`.
- Use `validate-skill` when the official validator cannot import PyYAML; it checks required files and frontmatter.
- Use `self-test` after edits to exercise the fixture failure corpus plus basic and doctor recovery bundle artifact paths.
- Keep `fixtures/redacted-real-log-corpus.json` redacted and representative. It should include transport, app-state, auth/session, compaction, no-progress true positives, retry-warning and startup-prewarm variants, plus quoted payload false positives that protect the classifier from counting assistant, request, or goal text as failures. `self-test` enforces those classifier-family, real-log-target, and quoted-payload coverage gates.
- Use `package` to create a clean distributable archive instead of copying local clutter.
- Fix ambiguity before continuing. If the current file state conflicts with the checkpoint, stop and report the conflict.
- Use the scripts as helpers, not as authority. Current files and live command output beat old checkpoints.
