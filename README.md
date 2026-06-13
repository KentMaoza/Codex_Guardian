# Codex Guardian Skill

Codex Guardian helps recover from Codex reconnect loops, WebSocket stream failures, remote compaction failures, app sidecar stalls, and ambiguous long-task state.

It ships as an Agent Skill with three layers:

- Project-local skill workflow: `skills/codex-guardian/SKILL.md`
- Watch and diagnostic script: `skills/codex-guardian/scripts/diagnose_codex_streams.py`
- Full recovery CLI: `skills/codex-guardian/scripts/codex_guardian.py`

## Quick Start

Run a one-shot watcher:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py watch --once --hours 1
```

The watcher exits nonzero for high-severity stream or compaction failures, no-progress loops, repeated app-state failures that require restart, unreadable or overdue active checkpoints, failed local reachability probes when `--check-reachability` is set, and degraded upstream status when `--check-service-status` is set. Failed service-status checks stay `unknown` and do not make watch actionable by themselves. Watch output and recovery bundles include the health block behind that decision, including in `diagnosis.md`.

Write a recovery bundle when the watcher finds a failure:

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

When `--task` is set, `watch` writes a `preflight_done` checkpoint before reporting. If it also writes a recovery bundle, that bundle includes the checkpoint. If the current checkpoint is unreadable or overdue, the watch bundle preserves that attention in its README, diagnosis, and resume prompt.
Use `--doctor` when the watcher should create the full recovery bundle as soon as a failure appears. It implies a recovery bundle and adds `doctor.md`, `status.md`, `reachability.md`, `service-status.md`, `environment.md`, and `connection-triage.md` with matching JSON files.
When `--check-reachability` makes `watch` actionable and `--recovery-report` is set, the bundle includes `reachability.md` and `reachability.json`.
When `--check-service-status` makes `watch` actionable and `--recovery-report` is set, the bundle includes `service-status.md`, `service-status.json`, `connection-triage.md`, and `connection-triage.json`.
When `watch` sees repeated app-state failures and `--mark-restart` is set, it writes `.codex-guardian/restart-marker.json` so `post-restart` can verify the restart afterward. It does not mark restart for transport-only failures. Manual `mark-restart` markers also include structured restart decision metadata so `post-restart` can carry that context forward.
When project recovery status is what makes `watch` actionable, the JSON report includes the same `status` object and the recovery bundle includes `status.md`, `status.json`, `connection-triage.md`, and `connection-triage.json`.

Run a diagnosis:

```bash
python3 skills/codex-guardian/scripts/diagnose_codex_streams.py --hours 12
```

Separate auth/session, app-state, and transport failures:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py health --hours 1
```

`health` reports `restart_codex_now`, `restart_recommended`, a structured `restart_decision` object, and the same `direct_fix_boundary` used by `connection-triage`: `3/10` direct-fix ceiling, `9/10` recovery-tooling ceiling, highest local recovery command, and the external repair reason. `auth_session` means the first action is to sign in or refresh the Codex session. `restart_codex_now` is the urgent repeated app-state-only rule; a single app-state event preserves state and watches for repeat before restart. Mixed transport and app-state failures use `restart_recommended: true` with `restart_timing: after_state_preserved`.
Add `--check-reachability` when you also want a live DNS and HTTP/TLS probe in the same report. The health `issue_type` stays log-derived; reachability appears as a separate object and makes the command exit nonzero when the local probe fails.
Add `--check-service-status` when you also want upstream status evidence in the same report. A degraded upstream status makes `health` exit nonzero; a failed status check is `unknown` and does not prove an outage.

Check whether this machine can resolve and reach the Codex HTTP path:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py reachability
```

`reachability` checks DNS first, then the configured HTTP or HTTPS endpoint. It reports DNS, TLS, reset, and timeout failures with the same transport families used by `health`. The result describes the current process network context, so sandboxing, proxy, or approval settings can change it. Use `--dns-only` when you want a quick local resolver check without opening an HTTP request.

Check whether the configured upstream status endpoint reports an outage:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py service-status
```

`service-status` reads a Statuspage-style JSON endpoint, defaulting to OpenAI status. A degraded status means the next action is to preserve state and wait or retry later. A failed status check is reported as `unknown`, not as proof of an upstream outage.

Show what Guardian can locally fix versus what belongs to Codex/OpenAI, app, auth, backend, or network layers:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py connection-triage \
  --project . \
  --hours 1
```

`connection-triage` reports the immediate health issue type and a separate `recovery_attention` value from the project status, so a clean current log can still point to an unstable post-restart marker or checkpoint problem. It also includes a structured `direct_fix_boundary` with a `3/10` direct-fix ceiling, a `9/10` recovery-tooling ceiling, the highest local recovery command, and the reason true transport/app/backend repair is outside Guardian's scope. The escalation packet names the redacted evidence to preserve when the problem is outside Guardian's local recovery scope.
Add `--check-reachability` when the boundary report should include live DNS and HTTP/TLS evidence. The health classification stays log-derived, reachability is reported separately, and the command exits nonzero if the local probe fails.
Add `--check-service-status` when the boundary report should include upstream service evidence. A degraded upstream status appears as `recovery_attention: upstream_degraded`; a failed status check remains `unknown`.

Summarize the current project recovery state:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py status \
  --project . \
  --hours 1
```

`status` reports current health, the active checkpoint, unreadable or overdue checkpoint state, latest recovery bundle, restart marker, and post-restart state when a marker exists. It also includes `fresh_recovery_bundle_recommended` and ordered `next_actions`, so a resumed session knows whether to inspect the checkpoint, create a fresh full bundle, restart Codex, or verify post-restart state next.

Create a health-based action plan:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py doctor \
  --project . \
  --task "Recover active task" \
  --touched README.md \
  --mark-restart \
  --hours 1
```

Add `--check-reachability` when logs are quiet but Codex still cannot connect. In that mode, `doctor` probes the Codex endpoint, treats local DNS/TLS/HTTP failure as attention, and writes the result into `reachability.md` and `reachability.json` inside the recovery bundle. Add `--check-service-status` when the same one-step decision should include upstream service status; degraded upstream status creates a recovery bundle, while a failed status check remains `unknown`. `watch --check-reachability --recovery-report` uses the same reachability probe to stop early and preserve reachability files.

Add `--task` and `--touched` when `doctor` is the entry point for a longer task. It writes a `preflight_done` checkpoint even when the sampled logs are healthy, and includes that checkpoint if a recovery bundle is needed.

When `doctor` recommends a restart and `--mark-restart` is set, it writes the restart marker before you restart Codex. After restart, verify whether app-state errors continued:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py post-restart --project . --hours 1
```

When a project restart marker exists, `post-restart` expands the effective log lookback if needed so the marker time is covered. It reports `no_activity` when no Codex log activity appears after the marker and `transport_unreliable` when app-state errors stopped but transport errors remain, including DNS, TLS, reset, timeout, and WebSocket transport families. Its JSON and markdown reports include the marker path, source command, issue type, restart timing, and restart decision fields.
Restart markers written by `watch`, `bundle`, or `doctor` include the source command, issue type, restart timing, and restart reason from the health decision or post-restart status decision.

Create a recovery bundle on demand:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py recover-now \
  --project . \
  --hours 1 \
  --task "Recover active task" \
  --touched README.md
```

`recover-now` is the shortest path to a full recovery bundle. It writes the doctor-grade bundle now, including status, reachability, service status, environment, connection triage, diagnosis, events, checkpoint when `--task` is set, and a resume prompt. It also writes a restart marker when the health classifier recommends a restart, unless `--no-mark-restart` is set.
Status, doctor, and connection-triage next actions prefer `recover-now` when they recommend a full recovery bundle.

Use `bundle --doctor` when you want the same full bundle with the lower-level bundle command:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py bundle \
  --project . \
  --hours 1 \
  --doctor \
  --mark-restart \
  --task "Recover active task" \
  --touched README.md
```

Each bundle includes `README.md` and `manifest.json` so a resumed session can see what to open first. With `--doctor`, the bundle also includes `doctor.md`, `doctor.json`, `status.md`, `status.json`, `reachability.md`, `reachability.json`, `service-status.md`, `service-status.json`, `environment.md`, `environment.json`, `connection-triage.md`, and `connection-triage.json`. The status files capture the current checkpoint, latest bundle, restart marker, post-restart state, and next actions for the resumed session. The reachability files capture DNS and HTTP/TLS evidence for the Codex endpoint at bundle time, from the current process network context. The service-status files capture the configured Statuspage-style upstream status and treat failed checks as `unknown`, not as proof of an outage. The environment files capture the Codex home, Python and platform details, Codex CLI presence, probe endpoint, and read-only log source evidence for `logs_2.sqlite` and desktop Codex logs. Use `--reachability-endpoint`, `--reachability-timeout`, `--reachability-dns-only`, `--service-status-endpoint`, and `--service-status-timeout` when the bundle needs specific probe targets. The JSON command response includes the same `status`, `reachability`, and `service_status` objects. The bundle preserves unreadable or overdue current-checkpoint attention in the README, diagnosis, status, doctor artifacts, and resume prompt. In doctor bundles, `resume-prompt.txt` tells the next session to open `status.md` first, then `doctor.md`. With `--task`, it first writes and bundles a `preflight_done` checkpoint; with `--mark-restart`, it writes a restart marker when the health classifier recommends restarting Codex or project status shows post-restart app-state instability.

Run the local fixture soak test:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py self-test
```

`self-test` exercises the inline soak log plus `skills/codex-guardian/fixtures/redacted-real-log-corpus.json`, a redacted real-log-shaped corpus covering transport, retry warnings, startup prewarm failures, app-state, auth/session, compaction, no-progress, and quoted payload false positives from assistant, request, and goal text. It now fails if the corpus stops covering required classifier families, loses the real-log-shaped targets that made earlier bugs visible, or drops the false-positive fixtures that prevent quoted text from being counted as failures. It also writes basic and doctor recovery bundles so install checks cover the full recovery artifact path.

Create a preflight checkpoint before substantial work:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py preflight \
  --project . \
  --task "Example task" \
  --next-action "Edit one named file and verify it" \
  --touched README.md
```

Preflight records git state and whether each touched path exists, is missing, or points outside the project.

Let Guardian decide whether an estimated task is long enough to need preflight:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py auto-preflight \
  --project . \
  --task "Example task" \
  --next-action "Edit one named file and verify it" \
  --estimated-minutes 20 \
  --touched README.md
```

`auto-preflight` writes the same `preflight_done` checkpoint when `--estimated-minutes` is at or above `--threshold-minutes`, defaulting to 10. For shorter tasks it reports `created_preflight_checkpoint: false` and leaves the current checkpoint untouched. Use `--force` when a small task still needs a checkpoint because the state would be costly to lose.

Create a manual checkpoint after a slice:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --task "Example task" \
  --phase preflight_done \
  --verified "No files edited yet" \
  --next-action "Edit only the named file and verify it"
```

Record and compare a no-progress fingerprint:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --task "Example task" \
  --phase write_started \
  --next-action "Edit README.md" \
  --touched README.md \
  --fingerprint

python3 skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --task "Example task" \
  --phase write_done \
  --next-action "Report result" \
  --touched README.md \
  --fingerprint \
  --compare-fingerprint
```

Generate a recovery prompt:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py resume-prompt --project .
```

Wrap a non-interactive command:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py wrap \
  --project . \
  --task "Guarded command" \
  --touched README.md \
  -- codex exec "Do one bounded task and stop."
```

`wrap` automatically records a preflight checkpoint before the command runs, including git status, touched-file facts, the command, and a checkpoint deadline.

## Safety

The diagnostic commands are read-only by default. Checkpoints are written under `.codex-guardian/` in the project you choose.

Use 10 to 15 minute slices for long tasks. A reconnect, compaction failure, or WebSocket drop should lose at most one slice of ambiguous state.

Codex Guardian cannot directly repair Codex app, OpenAI backend, auth, network, or WebSocket internals. Its direct-fix boundary is local recovery: diagnose the failure class, preserve state, point auth/session failures to reauth, check local reachability and upstream status when asked, guide restart decisions, and create a safe resume path.

Use `doctor` when you want one decision point. It runs the health classifier, checks checkpoint state and project recovery status, creates a recovery bundle when attention is needed, and prints the next local actions. Add `--check-reachability` when local endpoint reachability should affect the decision. Add `--check-service-status` when upstream service status should affect the decision. Degraded upstream status counts as attention; a failed status check stays `unknown` and does not prove an outage. Add `--mark-restart` when current health or post-restart status recommends a restart and you want the post-restart check seeded automatically. It does not restart apps or alter Codex internals.

When `doctor` creates a bundle, its JSON response includes the current `status` object and the same `service_status` object written into the bundle. When `bundle --doctor` creates a bundle, its JSON response includes the same `status`, `reachability`, and `service_status` objects written into the bundle. If the reachability probe fails, the ordered actions point you to `reachability.md` before retrying Codex. Open `status.md` for the current recovery state, then use `doctor.md` for the ordered local actions. The bundle also includes JSON copies, reachability files, service-status files, environment files, connection triage files, diagnosis, events, checkpoint, and resume prompt.

Public reports redact home paths, tokens, emails, conversation IDs, thread IDs, UUID-like IDs, and long opaque IDs.

Do not publish raw `.codex` logs, sessions, databases, auth files, or memory files. Use `skills/codex-guardian/references/privacy-redaction.md` before sharing diagnostics.

## Install

Check whether the skill is installed:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py install-check
```

`install-check` verifies that the installed skill has the required runnable scripts, references, and redacted fixture corpus, not just `SKILL.md`.

Install it into `~/.codex/skills/codex-guardian` only when missing:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py install-check --install
```

Use `--force` only when you intentionally want to overwrite an existing installed copy.

Validate the required skill files and frontmatter without PyYAML:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py validate-skill
```

Build a clean install archive:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py package --output-dir dist
```

The package command writes `codex-guardian.tar.gz` and `codex-guardian-package.json`, excluding `.DS_Store`, `__pycache__`, and `.pyc` files. The manifest records required skill files and the command fails before packaging if any required runtime, reference, or fixture file is missing.
