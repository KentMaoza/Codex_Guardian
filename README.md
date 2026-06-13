# Codex Guardian

Codex Guardian is a Codex skill for bad connection days.

It helps when Codex reconnects, loses a stream, gets stuck after compaction, reports an unknown conversation, or leaves you unsure what changed before the failure. It does not repair Codex itself. It gives you checkpoints, recovery bundles, health checks, and a cleaner next action so you can continue without guessing.


## Install for Codex

Copy this block into Terminal:

```bash
git clone https://github.com/KentMaoza/Codex_Guardian.git
cd Codex_Guardian
python3 skills/codex-guardian/scripts/codex_guardian.py install-check --install
python3 skills/codex-guardian/scripts/codex_guardian.py install-check
```

If you already installed an older copy and want to replace it:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py install-check --install --force
```

That installs the skill into:

```text
~/.codex/skills/codex-guardian
```

If Codex is already open, start a new Codex session after installing so the skill can be loaded for new work.

If you prefer the release archive instead of `git clone`:

```bash
curl -L https://github.com/KentMaoza/Codex_Guardian/releases/download/Codex_Guardian-V.0/codex-guardian.tar.gz -o codex-guardian.tar.gz
mkdir -p ~/.codex/skills
tar -xzf codex-guardian.tar.gz -C ~/.codex/skills
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py install-check
```

## Quick start for Codex trouble

When Codex feels unstable, or before a long task where reconnect would be painful, run this from the project folder:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode both \
  --task "Describe the Codex task" \
  --next-action "Continue in one small verified slice" \
  --estimated-minutes 20 \
  --check-reachability \
  --check-service-status
```

Use this when you want the full recovery bundle immediately:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py recover-now \
  --project . \
  --hours 1
```

Use this before a long Codex task when you only want preflight:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode before-task \
  --task "Describe the work here" \
  --next-action "What Codex should do first" \
  --estimated-minutes 20
```

Use this after a reconnect or restart when you only want recovery:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode after-reconnect \
  --task "Recover the interrupted Codex task" \
  --next-action "Open the recovery bundle first" \
  --check-reachability \
  --check-service-status
```

## What it does

Codex Guardian gives you a local safety layer around Codex work:

- It checks Codex logs for connection, compaction, auth, app-state, and no-progress problems.
- It writes checkpoints before risky work.
- It has one `autocast` command for preflight plus reconnect recovery.
- It creates recovery bundles with a diagnosis, status, redacted event samples, and a resume prompt.
- It separates local reachability, upstream service status, auth/session trouble, transport failures, and app-state churn.
- It tells you when restart is the next local move, and when reauth or smaller retry is more appropriate.
- It avoids treating quoted failure text in assistant output as a real connection failure.
- It can print hook or startup-script command templates with `integration-template`.

## Pros

- Useful when Codex disconnects in the middle of work.
- Helps you avoid losing track of what changed.
- Gives a simple `doctor` command instead of forcing you to inspect logs manually.
- Gives a simple `autocast` command when you want preflight and reconnect recovery together.
- Makes restart decisions clearer, especially for repeated `unknown_conversation` or mixed failures.
- Keeps public recovery reports safer by redacting home paths, emails, tokens, conversation IDs, thread IDs, UUID-like IDs, and long opaque IDs.
- Includes tests, fixture logs, self-checks, and package validation.

## Contra

- It is not a direct fix for Codex app bugs, OpenAI backend issues, auth bugs, WebSocket transport problems, or your local network.
- It does not silently install a Codex hook or plugin into global app config.
- The optional watcher is a long-running terminal process unless you run it with `--once`.
- It cannot guarantee that a failed Codex task will resume perfectly.
- It reads local Codex logs, so the diagnosis is only as good as the available logs.
- It writes recovery files into `.codex-guardian/` inside the project you choose.
- It is a power tool. If you only need to ask Codex one small question, you probably do not need it.

## Best command for each situation

| Situation | Command |
| --- | --- |
| You want automatic preflight plus reconnect recovery | `autocast --project . --mode both --estimated-minutes 20` |
| You only want automatic preflight | `autocast --project . --mode before-task --estimated-minutes 20` |
| You only want after-reconnect recovery | `autocast --project . --mode after-reconnect --check-reachability --check-service-status` |
| You want one recovery decision | `doctor --project . --hours 1` |
| Codex cannot connect but logs are unclear | `doctor --project . --hours 1 --check-reachability --check-service-status` |
| You want a full bundle now | `recover-now --project . --hours 1` |
| You are about to start a long task | `preflight --project . --task "..." --next-action "..."` |
| You just restarted Codex | `post-restart --project . --hours 1` |
| You want to know current recovery state | `status --project . --hours 1` |
| You want copy-paste hook or startup commands | `integration-template --project .` |
| You only want log classification | `health --hours 1` |
| You only want endpoint reachability | `reachability` |
| You only want OpenAI service status | `service-status` |

All commands below assume you are using the installed skill:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py <command>
```

If you are running from this repository instead, use:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py <command>
```

## The honest fix boundary

Codex Guardian is recovery tooling, not a patch for Codex internals.

Highest realistic score as direct connection fix: `3/10`.

Highest realistic score as Codex recovery tooling: `9/10`.

It can preserve state, classify the likely failure type, check reachability and service status, produce a resume prompt, and point you to restart or reauth when that is the right local action. The real fix for a broken Codex app state, backend outage, auth defect, WebSocket bug, or network problem has to happen outside this skill.

## Common flows

### Before a long task

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode before-task \
  --task "Build the feature" \
  --next-action "Inspect the target files first" \
  --estimated-minutes 20
```

`autocast --mode before-task` creates a checkpoint only when the estimate crosses the long-task threshold. The default threshold is 10 minutes.

### When Codex disconnects

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode after-reconnect \
  --task "Recover the interrupted task" \
  --next-action "Open the recovery bundle first"
```

If recovery attention is needed, open the generated `.codex-guardian/recovery/.../README.md` first. It tells the next Codex session what to read and what to do next.

### Optional background watcher

Run this only when you want a terminal process to keep watching:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py watch \
  --project . \
  --doctor \
  --mark-restart \
  --check-reachability \
  --check-service-status
```

For a script or hook that must return quickly, add `--once`.

### Integration template

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py integration-template --project .
```

This prints copy-paste commands for before-task autocast, after-reconnect autocast, combined autocast, one-shot watcher use, and the optional background watcher.

### When Codex recommends a restart

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py mark-restart --project .
```

Restart Codex, then run:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py post-restart --project . --hours 1
```

### When a command is risky

Wrap a non-interactive command so Guardian records the state before and after it runs:

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py wrap \
  --project . \
  --task "Run one bounded Codex exec task" \
  -- codex exec "Do one bounded task and stop with a concise report."
```

## Command reference

### Autocast

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py autocast \
  --project . \
  --mode both \
  --task "Guard this Codex task" \
  --next-action "Continue in one small verified slice" \
  --estimated-minutes 20
```

Runs automatic preflight, reconnect recovery, or both depending on `--mode`.

### Diagnose

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py diagnose --hours 12 --format markdown
```

Summarizes local Codex stream and reconnect failures.

### Watch

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py watch --once --hours 1
```

Stops when it sees actionable failures. Add `--recovery-report --project .` to write a bundle when a failure appears.

### Health

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py health --hours 1
```

Classifies the current issue as `auth_session`, `app_state`, `transport`, `mixed`, `compaction`, `no_progress`, or healthy.

### Connection triage

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py connection-triage \
  --project . \
  --hours 1 \
  --check-reachability \
  --check-service-status
```

Shows the local recovery actions and the direct-fix boundary.

### Bundle

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py bundle \
  --project . \
  --hours 1 \
  --doctor
```

Writes a recovery bundle. `--doctor` adds status, reachability, service-status, environment, and connection-triage files.

### Checkpoint

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py checkpoint \
  --project . \
  --task "Example task" \
  --phase write_done \
  --verified "The edit was made" \
  --next-action "Run validation"
```

Writes a durable checkpoint into `.codex-guardian/checkpoints/`.

### Resume prompt

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py resume-prompt --project .
```

Prints a prompt for the next Codex session based on the latest checkpoint.

### Integration template

```bash
python3 ~/.codex/skills/codex-guardian/scripts/codex_guardian.py integration-template --project .
```

Prints command templates for manual startup, reconnect recovery, one-shot hook-like use, and the optional background watcher.

## Safety and privacy

The diagnostic commands are read-only by default.

Project recovery files are written under:

```text
.codex-guardian/
```

Do not publish raw `.codex` logs, sessions, databases, auth files, or memory files.

Public reports redact home paths, tokens, emails, conversation IDs, thread IDs, UUID-like IDs, and long opaque IDs. Still review any report before sharing it outside your machine.

## For maintainers

Run the test suite:

```bash
python3 -m unittest tests/test_codex_guardian.py
```

Validate the skill files and frontmatter:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py validate-skill --skill-dir skills/codex-guardian
```

Run the fixture soak test:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py self-test
```

Build the release package:

```bash
python3 skills/codex-guardian/scripts/codex_guardian.py package --output-dir dist
```

The package command writes:

```text
dist/codex-guardian.tar.gz
dist/codex-guardian-package.json
```

The package excludes `.DS_Store`, `__pycache__`, and `.pyc` files. It fails before packaging if a required runtime, reference, or fixture file is missing.

It's Free, use it to built with codex, Happy CODEX MAXXING!!
