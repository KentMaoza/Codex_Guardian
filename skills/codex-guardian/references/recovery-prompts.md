# Recovery Prompts

## Resume From Checkpoint

```text
Use this checkpoint as the source of continuity:

[paste checkpoint summary]

First verify current file state with the smallest safe command. Do not run broad search. Read only [files]. If current files contradict the checkpoint, stop and report the conflict. Otherwise continue from [phase] and complete [next action].
```

## Recover After Stream Failure

```text
The previous Codex stream failed before completion. Do not assume the last visible message reflects the final project state.

First:
1. Check current file state.
2. Identify whether any intended edits already happened.
3. Compare that state with this checkpoint: [checkpoint].

Then either continue the next action or stop with a concise recovery report.
```

## Prevent A Preflight Loop

```text
This is a bounded recovery slice. Do not reread general instructions unless a required file is missing. Read only:

- [file 1]
- [file 2]

Target phase: [phase]
Expected output: [output]
Stop after this slice and report exactly what changed and what was verified.
```
