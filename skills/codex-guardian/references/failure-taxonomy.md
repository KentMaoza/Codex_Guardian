# Failure Taxonomy

Use this taxonomy to classify Codex reliability failures without overfitting to one machine.

## Stream Transport

Symptoms:

- `stream disconnected before completion`
- `stream disconnected - retrying sampling request`
- `failed to send websocket request`
- `responses_websocket` with `error`, `failed`, `closed`, or `timeout`
- `idle timeout waiting for websocket`
- `websocket closed by server before response.completed`
- `Broken pipe`
- startup prewarm failures with TLS record alerts such as `BadRecordMac`

Likely action:

- Verify current file state.
- Avoid retrying the same broad prompt.
- Resume from a checkpoint with a smaller slice.

## Auth And Session

Symptoms:

- `401 Unauthorized`
- `403 Forbidden`
- `authentication required`
- `authentication failed`
- `session expired`
- `token expired`

Likely action:

- Preserve task state first.
- Sign in again or refresh the Codex session.
- Restart the app only if it still reports stale auth after reauth.

## Remote Compaction

Symptoms:

- `Error running remote compact task`
- `/backend-api/codex/responses/compact`
- failures near a full context window

Likely action:

- Stop adding context.
- Create a task-state checkpoint.
- Start a smaller follow-up thread or resume with a prompt that names only the needed files.

## App State Or Sidecar Churn

Symptoms:

- `Received turn/started for unknown conversation`
- `Received turn/completed for unknown conversation`
- `turn/start` timeout
- desktop app accepts a retry later without other changes

Likely action:

- Reduce concurrent Codex work.
- Restart the app after active work completes.
- Preserve task state outside the chat before retrying.

## No Progress Loop

Symptoms:

- Same files are reread repeatedly.
- Logs mention `no progress loop`.
- Context compacts before writes.
- Long task runs without file changes or useful output.

Likely action:

- Write a checkpoint.
- Force the next prompt to read only named files.
- Stop if no file edit or verified result happens within the next slice.
