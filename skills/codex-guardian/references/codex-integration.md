# Codex Integration

Codex Guardian supports three practical integration levels:

1. Skill trigger: install the skill into `~/.codex/skills/codex-guardian` so Codex can load the workflow instructions in new sessions.
2. Command trigger: run `autocast` before a long task, after a reconnect, or both.
3. Watcher trigger: run `watch` only when you intentionally want a terminal process to monitor Codex logs.

It does not rewrite Codex app hook, plugin, auth, session, or transport internals. If a future Codex hook or plugin schema is available in the local environment, wire the commands below to that hook.

## Before A Long Codex Task

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode before-task \
  --task "Describe the Codex task" \
  --next-action "Inspect the target files first" \
  --estimated-minutes 20
```

This writes a `preflight_done` checkpoint when the task estimate crosses the threshold.

## After Reconnect Or Restart

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode after-reconnect \
  --task "Recover the interrupted Codex task" \
  --next-action "Open the recovery bundle first" \
  --check-reachability \
  --check-service-status
```

This runs the doctor-grade recovery path, writes a recovery bundle when attention is needed, and returns a nonzero exit code when the caller should stop and recover.

## Combined Guard

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode both \
  --task "Guard this Codex task" \
  --next-action "Continue in one small verified slice" \
  --estimated-minutes 20 \
  --check-reachability \
  --check-service-status
```

Use this when a wrapper, project startup script, or manual habit should do both preflight and reconnect recovery in one bounded command.

## Optional Background Watcher

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py watch \
  --project . \
  --doctor \
  --mark-restart \
  --check-reachability \
  --check-service-status
```

This runs until stopped. Use it only in a terminal where a long-lived process is acceptable.

For scripts or hooks that must return quickly, use one-shot mode:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py watch \
  --project . \
  --once \
  --doctor \
  --mark-restart \
  --check-reachability \
  --check-service-status
```

## Generate Local Commands

Run this from a project to print commands with the current project path:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py integration-template --project .
```

