#!/usr/bin/env python3
"""Codex Guardian CLI.

Read Codex logs, write task checkpoints, generate recovery prompts, and wrap
non-interactive commands with lightweight state tracking.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Optional
import urllib.error
import urllib.parse
import urllib.request


PATTERNS: list[tuple[str, str, str]] = [
    ("websocket_send_failed", "high", "failed to send websocket request"),
    ("websocket_idle_timeout", "high", "idle timeout waiting for websocket"),
    ("websocket_closed", "high", "websocket closed by server before response.completed"),
    ("broken_pipe", "high", "Broken pipe"),
    ("remote_compaction_failed", "high", "Error running remote compact task"),
    ("compact_endpoint_failed", "high", "/backend-api/codex/responses/compact"),
    ("unknown_conversation", "medium", "unknown conversation"),
    ("mcp_request_timeout", "medium", "mcp_request_timeout"),
]

DEFAULT_LIMIT = 80
DEFAULT_REACHABILITY_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_SERVICE_STATUS_ENDPOINT = "https://status.openai.com/api/v2/status.json"
PACKAGE_EXCLUDES = {".DS_Store"}
REQUIRED_SKILL_FILES = [
    "SKILL.md",
    "scripts/codex_guardian.py",
    "scripts/diagnose_codex_streams.py",
    "fixtures/redacted-real-log-corpus.json",
    "references/failure-taxonomy.md",
    "references/privacy-redaction.md",
    "references/recovery-prompts.md",
]
NORMAL_RESPONSE_EVENT_RE = re.compile(
    r'"type"\s*:\s*"response\.(?:created|in_progress|completed|output_text\.|content_part\.|output_item\.|reasoning_summary|function_call_arguments\.)',
    re.I,
)
RESPONSES_WEBSOCKET_FAILURE_RE = re.compile(
    r"(?:responses_websocket[^\n]{0,80}\b(?:error|failed|failure|closed|timeout)\b|"
    r"\b(?:error|failed|failure|closed|timeout)\b[^\n]{0,80}responses_websocket)",
    re.I,
)
STREAM_DISCONNECT_RE = re.compile(r"\bstream disconnected\b", re.I)
NETWORK_FAILURE_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "dns_resolution_failed",
        "high",
        re.compile(
            r"\b(?:dns error|failed to lookup address information|name or service not known|nodename nor servname provided)\b",
            re.I,
        ),
        "DNS resolution failure",
    ),
    (
        "tls_handshake_failed",
        "high",
        re.compile(
            r"\b(?:tls handshake failed|certificate verify failed|invalid peer certificate|handshake failure|badrecordmac|bad_record_mac)\b",
            re.I,
        ),
        "TLS handshake or record failure",
    ),
    (
        "connection_reset",
        "high",
        re.compile(r"\b(?:connection reset by peer|econnreset|connection reset)\b", re.I),
        "connection reset",
    ),
    (
        "request_timeout",
        "high",
        re.compile(
            r"\b(?:error sending request for url|reqwest|request)\b[^\n]{0,160}\b(?:operation timed out|request timed out|deadline has elapsed)\b",
            re.I,
        ),
        "request timeout",
    ),
]
AUTH_SESSION_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "auth_session_failed",
        "high",
        re.compile(
            r"\b(?:401|403)\b[^\n]{0,160}\b(?:unauthorized|forbidden|authentication|required|token|session)\b|"
            r"\b(?:authentication required|authentication failed|not authenticated|session expired|token expired|invalid auth(?:entication)?)\b",
            re.I,
        ),
        "authentication or session failure",
    ),
]
TRANSPORT_CODES = [
    "stream_disconnect",
    "websocket_send_failed",
    "websocket_idle_timeout",
    "websocket_closed",
    "broken_pipe",
    "responses_websocket_failure",
    "dns_resolution_failed",
    "tls_handshake_failed",
    "connection_reset",
    "request_timeout",
]
APP_STATE_CODES = ["unknown_conversation", "turn_start_timeout", "mcp_request_timeout"]
COMPACTION_CODES = ["remote_compaction_failed", "compact_endpoint_failed"]
NO_PROGRESS_CODES = ["no_progress_loop"]
AUTH_SESSION_CODES = ["auth_session_failed"]
QUOTED_AGENT_HISTORY_RE = re.compile(
    r"(?:The following is the Codex agent history|>>> TRANSCRIPT(?: DELTA)? START|Treat the transcript[^\n]+untrusted evidence)",
    re.I,
)
CODE_FIXTURE_QUOTE_RE = re.compile(
    r"(?:def\s+test_[A-Za-z0-9_]+\s*\(|write_sqlite_log(?:_rows)?\s*\(|result\s*=\s*run_cli\s*\()",
    re.I,
)
STREAM_EVENT_TOKEN_USAGE_RE = re.compile(
    r"codex_core::stream_events_utils[\s\S]+codex\.turn\.token_usage\.input_tokens",
    re.I,
)
APP_SERVER_GOAL_PAYLOAD_RE = re.compile(
    r"app_server\.request\{[^\n}]*thread/goal/set[\s\S]+codex\.turn\.token_usage\.input_tokens",
    re.I,
)
OUTBOUND_RESPONSE_CREATE_RE = re.compile(
    r"websocket request:\s*\{\\?\"type\\?\"\s*:\s*\\?\"response\.create\\?\"",
    re.I,
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def redact(text: str, home: Optional[Path] = None) -> str:
    if not text:
        return ""
    home = home or Path.home()
    redacted = text.replace(str(home), "~")
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]", redacted)
    redacted = re.sub(r"session[_-]?token[=:]\S+", "session_token=[REDACTED]", redacted, flags=re.I)
    redacted = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", redacted, flags=re.I)
    redacted = re.sub(r"\b(conversationId|threadId|conversation_id|thread_id)=\S+", r"\1=[REDACTED_ID]", redacted, flags=re.I)
    redacted = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "[REDACTED_ID]",
        redacted,
        flags=re.I,
    )
    redacted = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9_\-]{31,}(?![A-Za-z0-9])", "[REDACTED_ID]", redacted)
    return redacted


def match_events(text: str) -> list[dict[str, str]]:
    if NORMAL_RESPONSE_EVENT_RE.search(text):
        return []
    lowered = text.lower()
    matches = []
    for code, severity, needle in PATTERNS:
        if needle.lower() in lowered:
            matches.append({"code": code, "severity": severity, "needle": needle})
    if "turn/start" in lowered and "timeout" in lowered:
        matches.append({"code": "turn_start_timeout", "severity": "medium", "needle": "turn/start + timeout"})
    if (
        RESPONSES_WEBSOCKET_FAILURE_RE.search(text)
    ):
        matches.append({"code": "responses_websocket_failure", "severity": "high", "needle": "responses_websocket + failure"})
    if STREAM_DISCONNECT_RE.search(text):
        matches.append({"code": "stream_disconnect", "severity": "high", "needle": "stream disconnected"})
    for code, severity, pattern, needle in NETWORK_FAILURE_PATTERNS:
        if pattern.search(text):
            matches.append({"code": code, "severity": severity, "needle": needle})
    for code, severity, pattern, needle in AUTH_SESSION_PATTERNS:
        if pattern.search(text):
            matches.append({"code": code, "severity": severity, "needle": needle})
    if (
        "no-progress loop" in lowered
        or ("no progress" in lowered and "loop" in lowered)
        or ("reread" in lowered and "same files" in lowered and any(word in lowered for word in ("repeatedly", "without edits", "without file changes")))
    ):
        matches.append({"code": "no_progress_loop", "severity": "medium", "needle": "no progress loop"})
    return matches


def is_quoted_agent_history(text: str) -> bool:
    if QUOTED_AGENT_HISTORY_RE.search(text):
        return True
    if STREAM_EVENT_TOKEN_USAGE_RE.search(text):
        return True
    if APP_SERVER_GOAL_PAYLOAD_RE.search(text):
        return True
    if OUTBOUND_RESPONSE_CREATE_RE.search(text):
        return True
    telemetry_target = "codex_core::stream_events_utils" in text or "codex_core::session::handlers" in text
    return bool(telemetry_target and CODE_FIXTURE_QUOTE_RE.search(text))


def sqlite_schema_event(db_path: Path, message: str) -> list[dict[str, Any]]:
    return [{
        "source": redact(str(db_path)),
        "time": now_utc(),
        "level": "ERROR",
        "target": "sqlite",
        "message": redact(message),
        "matches": [{"code": "sqlite_schema_unsupported", "severity": "medium", "needle": "sqlite schema"}],
    }]


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def first_column(columns: set[str], candidates: tuple[str, ...]) -> Optional[str]:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def parse_sqlite_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.fromtimestamp(int(text), timezone.utc)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def load_sqlite_events(codex_home: Path, hours: int, limit: int) -> list[dict[str, Any]]:
    db_path = codex_home / "logs_2.sqlite"
    if not db_path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    events: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        schema_rows = con.execute("pragma table_info(logs)").fetchall()
        if not schema_rows:
            return sqlite_schema_event(db_path, "Could not inspect logs table schema")
        columns = {str(row[1]) for row in schema_rows}
        time_col = first_column(columns, ("ts", "timestamp", "created_at", "time", "created"))
        level_col = first_column(columns, ("level", "severity", "log_level"))
        target_col = first_column(columns, ("target", "module", "source", "logger"))
        body_col = first_column(columns, ("feedback_log_body", "message", "body", "log", "text"))
        if not body_col:
            return sqlite_schema_event(db_path, f"Unsupported logs table schema: {', '.join(sorted(columns))}")
        select_time = quote_identifier(time_col) if time_col else "null"
        select_level = quote_identifier(level_col) if level_col else "'LOG'"
        select_target = quote_identifier(target_col) if target_col else "'sqlite'"
        select_body = f"coalesce({quote_identifier(body_col)}, '')"
        order_by = quote_identifier(time_col) + " desc" if time_col else "rowid desc"
        rows = con.execute(
            f"""
            select {select_time}, {select_level}, {select_target}, {select_body}
            from logs
            order by {order_by}
            limit ?
            """,
            (max(limit * 40, 500),),
        ).fetchall()
    except sqlite3.Error as exc:
        return [{
            "source": str(db_path),
            "time": now_utc(),
            "level": "ERROR",
            "target": "sqlite",
            "message": f"Could not read logs_2.sqlite: {exc}",
            "matches": [{"code": "sqlite_read_failed", "severity": "medium", "needle": "sqlite"}],
        }]
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass

    for ts, level, target, body in rows:
        event_time = parse_sqlite_time(ts)
        if event_time and event_time < cutoff:
            continue
        haystack = f"{target}\n{body}"
        if is_quoted_agent_history(haystack):
            continue
        matches = match_events(haystack)
        if not matches:
            continue
        events.append({
            "source": redact(str(db_path)),
            "time": event_time.replace(microsecond=0).isoformat() if event_time else now_utc(),
            "level": level,
            "target": target,
            "message": redact(str(body)[:1000]),
            "matches": matches,
        })
        if len(events) >= limit:
            break
    return events


def count_sqlite_activity_since(codex_home: Path, since: datetime) -> int:
    db_path = codex_home / "logs_2.sqlite"
    if not db_path.exists():
        return 0
    count = 0
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        schema_rows = con.execute("pragma table_info(logs)").fetchall()
        columns = {str(row[1]) for row in schema_rows}
        time_col = first_column(columns, ("ts", "timestamp", "created_at", "time", "created"))
        if not time_col:
            return 0
        rows = con.execute(
            f"select {quote_identifier(time_col)} from logs order by {quote_identifier(time_col)} desc limit ?",
            (2000,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
    since = utc_datetime(since)
    for (raw_time,) in rows:
        event_time = parse_sqlite_time(raw_time)
        if event_time and utc_datetime(event_time) >= since:
            count += 1
    return count


def desktop_log_dirs(hours: int) -> list[Path]:
    base = Path.home() / "Library" / "Logs" / "com.openai.codex"
    if not base.exists():
        return []
    today = datetime.now().date()
    dirs = []
    for days_back in range(max(1, min(7, (hours // 24) + 2))):
        day = today.toordinal() - days_back
        dt = datetime.fromordinal(day)
        path = base / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
        if path.exists():
            dirs.append(path)
    return dirs


def parse_desktop_log_time(line: str) -> Optional[datetime]:
    stamp = line[:24]
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", stamp):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_desktop_log_events(hours: int, limit: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for directory in desktop_log_dirs(hours):
        for log_path in sorted(directory.glob("*.log"), reverse=True):
            try:
                tail = deque(maxlen=2000)
                with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        tail.append(line.rstrip("\n"))
            except OSError:
                continue
            for line in reversed(tail):
                event_time = parse_desktop_log_time(line)
                if event_time and event_time < cutoff:
                    continue
                if is_quoted_agent_history(line):
                    continue
                matches = match_events(line)
                if not matches:
                    continue
                events.append({
                    "source": redact(str(log_path)),
                    "time": line[:24] if len(line) > 24 else "",
                    "level": "LOG",
                    "target": "desktop-log",
                    "message": redact(line[:1000]),
                    "matches": matches,
                })
                if len(events) >= limit:
                    return events
    return events


def count_desktop_activity_since(hours: int, since: datetime) -> int:
    count = 0
    since = utc_datetime(since)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for directory in desktop_log_dirs(hours):
        for log_path in sorted(directory.glob("*.log"), reverse=True):
            try:
                tail = deque(maxlen=2000)
                with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        tail.append(line.rstrip("\n"))
            except OSError:
                continue
            for line in reversed(tail):
                event_time = parse_desktop_log_time(line)
                if not event_time:
                    continue
                event_time = utc_datetime(event_time)
                if event_time < cutoff:
                    continue
                if event_time >= since:
                    count += 1
    return count


def count_log_activity_since(codex_home: Path, hours: int, since: datetime) -> int:
    return count_sqlite_activity_since(codex_home, since) + count_desktop_activity_since(hours, since)


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    severities = Counter()
    for event in events:
        for match in event.get("matches", []):
            counts[match["code"]] += 1
            severities[match["severity"]] += 1
    recommendations = []
    if counts["remote_compaction_failed"] or counts["compact_endpoint_failed"]:
        recommendations.append("Start a smaller recovery slice. Avoid adding more context before checkpointing the current task state.")
    if (
        counts["stream_disconnect"]
        or counts["websocket_send_failed"]
        or counts["websocket_idle_timeout"]
        or counts["websocket_closed"]
        or counts["dns_resolution_failed"]
        or counts["tls_handshake_failed"]
        or counts["connection_reset"]
        or counts["request_timeout"]
    ):
        recommendations.append("Treat the stream as unreliable. Verify file state, then resume from a checkpoint instead of retrying a broad prompt.")
    if counts["unknown_conversation"]:
        recommendations.append("Restart Codex after active work completes if unknown-conversation events keep appearing.")
    if counts["turn_start_timeout"] or counts["mcp_request_timeout"]:
        recommendations.append("Reduce concurrent Codex work and avoid querying large .codex databases while another turn is starting.")
    if counts["auth_session_failed"]:
        recommendations.append("Preserve state, then sign in or refresh the Codex session before retrying the request.")
    if counts["no_progress_loop"]:
        recommendations.append("Write a checkpoint now, name the next single file or behavior, and stop if the next slice makes no verified progress.")
    if not recommendations:
        recommendations.append("No known failure pattern was found in the sampled logs. Check network, app version, and current OpenAI status.")
    return {
        "event_count": len(events),
        "pattern_counts": dict(counts),
        "severity_counts": dict(severities),
        "recommendations": recommendations,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Codex Guardian Diagnosis",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Codex home: `{report['codex_home']}`",
        f"- Window: last {report['hours']} hour(s)",
        f"- Events sampled: {report['summary']['event_count']}",
        "",
        "## Pattern Counts",
        "",
    ]
    counts = report["summary"]["pattern_counts"]
    if counts:
        for key, value in sorted(counts.items()):
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- No known patterns found.")
    health = report.get("health")
    if health:
        lines += [
            "",
            "## Health",
            "",
            f"- Issue type: `{health['issue_type']}`",
            f"- Restart Codex now: `{str(health['restart_codex_now']).lower()}`",
            f"- Primary action: {health['primary_action']}",
        ]
    lines += reachability_markdown_section(report)
    lines += service_status_markdown_section(report)
    checkpoint_path = report.get("preflight_checkpoint")
    if checkpoint_path:
        lines += [
            "",
            "## Preflight Checkpoint",
            "",
            f"- Path: `{checkpoint_path}`",
            "- The current state was checkpointed before watch reported.",
        ]
    marker = report.get("restart_marker")
    if marker:
        marker_lines = [
            "",
            "## Restart Marker",
            "",
            f"- Path: `{marker['path']}`",
            f"- Created at: `{marker['created_at']}`",
            f"- Reason: {marker['reason']}",
        ]
        if marker.get("restart_decision"):
            marker_lines.append(f"- Restart decision: `{marker['restart_decision']['decision']}`")
            marker_lines.append(f"- Restart first action: `{marker['restart_decision']['first_action']}`")
        marker_lines.append("- After restarting Codex, run `post-restart --project .` from the same project.")
        lines += marker_lines
    lines += ["", "## Recommendations", ""]
    for rec in report["summary"]["recommendations"]:
        lines.append(f"- {rec}")
    lines += ["", "## Sample Events", ""]
    for event in report["events"][:20]:
        codes = ", ".join(match["code"] for match in event["matches"])
        source = redact(event["source"])
        message = event["message"].replace("\n", " ")
        lines.append(f"- `{event.get('time', '')}` `{codes}` `{source}`")
        lines.append(f"  {message[:500]}")
    return "\n".join(lines) + "\n"


def reachability_markdown_section(report: dict[str, Any]) -> list[str]:
    reachability_report = report.get("reachability")
    if not reachability_report:
        return []
    reachability = reachability_report["reachability"]
    return [
        "",
        "## Reachability",
        "",
        f"- Endpoint: `{reachability_report['endpoint']}`",
        f"- Reachability status: `{reachability['status']}`",
        f"- Local network issue: `{str(reachability['local_network_issue']).lower()}`",
        f"- Probe scope: {reachability['probe_scope']}",
        f"- Primary action: {reachability['primary_action']}",
    ]


def service_status_markdown_section(report: dict[str, Any]) -> list[str]:
    service_status_report = report.get("service_status")
    if not service_status_report:
        return []
    status = service_status_report["service_status"]
    return [
        "",
        "## Service Status",
        "",
        f"- Endpoint: `{service_status_report['endpoint']}`",
        f"- Upstream status: `{status['status']}`",
        f"- Upstream issue: `{str(status['upstream_issue']).lower()}`",
        f"- Check failed: `{str(status['check_failed']).lower()}`",
        f"- Primary action: {status['primary_action']}",
    ]


def write_output(text: str, output: Optional[str]) -> None:
    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Wrote {out_path}")
    else:
        print(text, end="")


def parse_endpoint(endpoint: str) -> tuple[urllib.parse.ParseResult, str, int]:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must be an http or https URL with a host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed, parsed.hostname, port


def default_dns_probe(host: str, port: int) -> list[dict[str, str]]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        address = sockaddr[0]
        family_name = "AF_INET6" if family == socket.AF_INET6 else "AF_INET"
        key = (family_name, address)
        if key not in seen:
            addresses.append({"family": family_name, "address": address})
            seen.add(key)
    return addresses


def default_http_probe(endpoint: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(endpoint, method="HEAD", headers={"User-Agent": "codex-guardian/reachability"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status": "reachable", "status_code": response.status, "method": "HEAD"}
    except urllib.error.HTTPError as exc:
        return {"status": "reachable", "status_code": exc.code, "method": "HEAD", "http_error": True}


def reachability_error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        parts.append(reachability_error_text(reason) if isinstance(reason, BaseException) else str(reason))
    return " ".join(part for part in parts if part)


def classify_reachability_error(exc: BaseException) -> dict[str, Any]:
    text = reachability_error_text(exc)
    lowered = text.lower()
    code = "network_unreachable"
    if isinstance(exc, socket.gaierror) or any(
        phrase in lowered
        for phrase in ("dns error", "failed to lookup address information", "name or service not known", "nodename nor servname")
    ):
        code = "dns_resolution_failed"
    elif isinstance(exc, ssl.SSLError) or any(
        phrase in lowered
        for phrase in (
            "tls handshake failed",
            "certificate verify failed",
            "invalid peer certificate",
            "handshake failure",
            "badrecordmac",
            "bad_record_mac",
        )
    ):
        code = "tls_handshake_failed"
    elif isinstance(exc, ConnectionResetError) or "connection reset" in lowered or "econnreset" in lowered:
        code = "connection_reset"
    elif isinstance(exc, TimeoutError) or any(
        phrase in lowered for phrase in ("operation timed out", "request timed out", "deadline has elapsed", "timed out")
    ):
        code = "request_timeout"
    elif isinstance(exc, ConnectionRefusedError) or "connection refused" in lowered:
        code = "connection_refused"
    return {
        "status": "failed",
        "code": code,
        "local_network_issue": True,
        "message": redact(text),
    }


def build_reachability_report(
    endpoint: str,
    timeout: float,
    dns_only: bool = False,
    dns_probe=default_dns_probe,
    http_probe=default_http_probe,
) -> dict[str, Any]:
    parsed, host, port = parse_endpoint(endpoint)
    report: dict[str, Any] = {
        "generated_at": now_utc(),
        "endpoint": endpoint,
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "timeout_seconds": timeout,
        "checks": {
            "dns": {"status": "pending"},
            "http": {"status": "pending"},
        },
        "reachability": {
            "status": "unknown",
            "local_network_issue": False,
            "direct_fix_available": False,
            "probe_scope": "current process network context; sandboxing, proxy, or approval settings can change this result",
            "primary_action": "Run log-based `health` or `doctor` next if Codex is still failing.",
        },
    }
    try:
        addresses = dns_probe(host, port)
    except Exception as exc:  # noqa: BLE001 - CLI report should classify arbitrary platform/network exceptions.
        issue = classify_reachability_error(exc)
        report["checks"]["dns"] = issue
        report["checks"]["http"] = {"status": "skipped", "reason": "DNS probe failed"}
        report["reachability"].update({
            "status": issue["code"],
            "local_network_issue": True,
            "primary_action": "Fix local DNS or network resolution before retrying Codex.",
        })
        return report

    report["checks"]["dns"] = {"status": "ok", "addresses": addresses}
    if dns_only:
        report["checks"]["http"] = {"status": "skipped", "reason": "--dns-only"}
        report["reachability"]["status"] = "dns_reachable"
        return report

    try:
        http_result = http_probe(endpoint, timeout)
    except Exception as exc:  # noqa: BLE001 - CLI report should classify arbitrary platform/network exceptions.
        issue = classify_reachability_error(exc)
        report["checks"]["http"] = issue
        report["reachability"].update({
            "status": issue["code"],
            "local_network_issue": True,
            "primary_action": "Fix the local network, TLS, proxy, or timeout issue before retrying Codex.",
        })
        return report

    report["checks"]["http"] = http_result
    report["reachability"]["status"] = "reachable"
    return report


def render_reachability_markdown(report: dict[str, Any]) -> str:
    reachability = report["reachability"]
    lines = [
        "# Codex Guardian Reachability",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Endpoint: `{report['endpoint']}`",
        f"- Host: `{report['host']}`",
        f"- Status: `{reachability['status']}`",
        f"- Local network issue: `{str(reachability['local_network_issue']).lower()}`",
        f"- Direct fix available: `{str(reachability['direct_fix_available']).lower()}`",
        f"- Probe scope: {reachability['probe_scope']}",
        f"- Primary action: {reachability['primary_action']}",
        "",
        "## Checks",
    ]
    for name, check in report["checks"].items():
        detail = f"- {name}: `{check['status']}`"
        if check.get("code"):
            detail += f" ({check['code']})"
        if check.get("status_code") is not None:
            detail += f" HTTP {check['status_code']}"
        if check.get("message"):
            detail += f" - {check['message']}"
        lines.append(detail)
    return "\n".join(lines) + "\n"


def render_reachability_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_reachability_markdown(report)


def fetch_service_status_json(endpoint: str, timeout: float) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Expected HTTP(S) status endpoint, got: {endpoint}")
    request = urllib.request.Request(endpoint, method="GET", headers={"User-Agent": "codex-guardian/service-status"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(1024 * 1024)
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object from status endpoint")
    return payload


def service_status_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    status_payload = payload.get("status")
    if not isinstance(status_payload, dict):
        return {
            "status": "unknown",
            "indicator": None,
            "description": None,
            "upstream_issue": False,
            "check_failed": True,
            "direct_fix_available": False,
            "primary_action": "Could not read service status payload. Use local health and reachability checks before retrying.",
        }
    indicator = str(status_payload.get("indicator") or "").lower()
    description = str(status_payload.get("description") or "")
    operational = indicator in {"none", "operational"} or description.lower() == "all systems operational"
    upstream_issue = not operational
    return {
        "status": "operational" if operational else "degraded",
        "indicator": indicator or None,
        "description": description or None,
        "upstream_issue": upstream_issue,
        "check_failed": False,
        "direct_fix_available": False,
        "primary_action": (
            "Preserve state and wait or retry later; this is outside local Codex Guardian repair scope."
            if upstream_issue
            else "No upstream status issue reported by the configured status endpoint."
        ),
    }


def build_service_status_report(
    endpoint: str = DEFAULT_SERVICE_STATUS_ENDPOINT,
    timeout: float = 5.0,
    status_probe: Optional[Any] = None,
) -> dict[str, Any]:
    report = {
        "schema": "codex-guardian.service-status.v1",
        "generated_at": now_utc(),
        "endpoint": endpoint,
        "service_status": {
            "status": "unknown",
            "indicator": None,
            "description": None,
            "upstream_issue": False,
            "check_failed": False,
            "direct_fix_available": False,
            "primary_action": "Check service status before retrying broad Codex work.",
        },
    }
    probe = status_probe or fetch_service_status_json
    try:
        payload = probe(endpoint, timeout)
    except Exception as exc:
        report["service_status"].update({
            "status": "unknown",
            "check_failed": True,
            "error": redact(str(exc)),
            "primary_action": "Could not check upstream status. Use local health and reachability checks before retrying.",
        })
        return report
    report["service_status"].update(service_status_from_payload(payload))
    return report


def render_service_status_markdown(report: dict[str, Any]) -> str:
    status = report["service_status"]
    lines = [
        "# Codex Guardian Service Status",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Endpoint: `{report['endpoint']}`",
        f"- Status: `{status['status']}`",
        f"- Upstream issue: `{str(status['upstream_issue']).lower()}`",
        f"- Check failed: `{str(status['check_failed']).lower()}`",
        f"- Direct fix available: `{str(status['direct_fix_available']).lower()}`",
        f"- Primary action: {status['primary_action']}",
    ]
    if status.get("indicator"):
        lines.append(f"- Indicator: `{status['indicator']}`")
    if status.get("description"):
        lines.append(f"- Description: {status['description']}")
    if status.get("error"):
        lines.append(f"- Error: {status['error']}")
    return "\n".join(lines) + "\n"


def render_service_status_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_service_status_markdown(report)


def build_report(codex_home: Path, hours: int, limit: int, include_desktop: bool = True) -> dict[str, Any]:
    events = load_sqlite_events(codex_home, hours, limit)
    if include_desktop:
        events.extend(load_desktop_log_events(hours, max(0, limit - len(events))))
    return {
        "generated_at": now_utc(),
        "codex_home": redact(str(codex_home)),
        "hours": hours,
        "summary": summarize(events),
        "events": events,
    }


def render_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_markdown(report)


def has_actionable_failure(report: dict[str, Any]) -> bool:
    summary = report["summary"]
    counts = summary["pattern_counts"]
    health = report.get("health") or health_assessment(summary)
    checkpoint_attention = report.get("checkpoint_attention") or {}
    status = report.get("status") or {}
    checkpoint_needs_attention = bool(
        checkpoint_attention.get("checkpoint_read_error") or checkpoint_attention.get("checkpoint_overdue")
    )
    status_needs_attention = bool(status.get("fresh_recovery_bundle_recommended"))
    reachability_attention = reachability_needs_attention(report.get("reachability"))
    service_status_attention = service_status_needs_attention(report.get("service_status"))
    return bool(
        summary["severity_counts"].get("high")
        or counts.get("no_progress_loop")
        or health["restart_codex_now"]
        or checkpoint_needs_attention
        or status_needs_attention
        or reachability_attention
        or service_status_attention
    )


def restart_decision_record(
    issue_type: str,
    restart_codex_now: bool,
    restart_recommended: bool,
    restart_timing: str,
    restart_reason: str,
) -> dict[str, Any]:
    if restart_codex_now:
        decision = "restart_now_after_checkpoint"
        first_action = "checkpoint"
    elif issue_type == "mixed":
        decision = "restart_after_state_preserved"
        first_action = "recover_now"
    elif issue_type == "post_restart_still_unstable":
        decision = "restart_after_state_preserved"
        first_action = "recover_now"
    elif issue_type == "manual_restart":
        decision = "manual_restart_after_checkpoint"
        first_action = "restart_codex"
    elif issue_type == "app_state":
        decision = "watch_for_repeat_before_restart"
        first_action = "checkpoint"
    elif issue_type == "auth_session":
        decision = "reauth_first"
        first_action = "reauth"
    elif issue_type == "transport":
        decision = "do_not_restart_first"
        first_action = "checkpoint_and_small_retry"
    elif issue_type == "compaction":
        decision = "smaller_context_first"
        first_action = "checkpoint"
    elif issue_type == "no_progress":
        decision = "single_next_slice_first"
        first_action = "name_one_next_file"
    else:
        decision = "no_restart_needed"
        first_action = "continue"
    return {
        "decision": decision,
        "first_action": first_action,
        "should_restart": restart_recommended,
        "timing": restart_timing,
        "marker_recommended": restart_recommended,
        "state_preservation_required": issue_type != "healthy",
        "reason": restart_reason,
    }


def health_assessment(summary: dict[str, Any]) -> dict[str, Any]:
    counts = summary["pattern_counts"]
    transport_patterns = [code for code in TRANSPORT_CODES if counts.get(code)]
    app_state_patterns = [code for code in APP_STATE_CODES if counts.get(code)]
    compaction_patterns = [code for code in COMPACTION_CODES if counts.get(code)]
    no_progress_patterns = [code for code in NO_PROGRESS_CODES if counts.get(code)]
    auth_session_patterns = [code for code in AUTH_SESSION_CODES if counts.get(code)]

    transport_unreliable = bool(transport_patterns)
    app_state_unstable = bool(app_state_patterns)
    auth_session_unhealthy = bool(auth_session_patterns)
    if auth_session_unhealthy:
        issue_type = "auth_session"
    elif transport_unreliable and app_state_unstable:
        issue_type = "mixed"
    elif transport_unreliable:
        issue_type = "transport"
    elif compaction_patterns:
        issue_type = "compaction"
    elif no_progress_patterns:
        issue_type = "no_progress"
    elif app_state_unstable:
        issue_type = "app_state"
    else:
        issue_type = "healthy"

    repeated_app_state = any(counts.get(code, 0) >= 2 for code in APP_STATE_CODES)
    restart_codex_now = bool(repeated_app_state and not transport_unreliable)
    restart_recommended = bool(restart_codex_now or issue_type == "mixed")
    if restart_codex_now:
        restart_timing = "now_after_checkpoint"
        restart_reason = "Repeated app-state failures appeared without transport failures."
    elif issue_type == "mixed":
        restart_timing = "after_state_preserved"
        restart_reason = "Mixed transport and app-state failures appeared; preserve state before restarting Codex."
    else:
        restart_timing = "not_first_action"
        restart_reason = "Restart is not the first local recovery action for this health classification."
    if issue_type == "auth_session":
        primary_action = "Sign in or refresh the Codex session before retrying; only restart the app if it still holds stale auth after reauth."
    elif issue_type == "transport":
        primary_action = "Verify file state, checkpoint current work, and resume from a smaller prompt before retrying the stream."
    elif issue_type == "app_state":
        primary_action = "Checkpoint active work, then restart Codex now if app-state events repeat."
    elif issue_type == "mixed":
        primary_action = "Write a recovery bundle, checkpoint active work, then restart Codex after preserving state."
    elif issue_type == "compaction":
        primary_action = "Stop adding context, write a checkpoint, and resume in a smaller slice."
    elif issue_type == "no_progress":
        primary_action = "Name one next file or behavior, checkpoint it, and stop if the next slice makes no verified progress."
    else:
        primary_action = "No known Codex connection failure pattern was found in the sampled logs."
    restart_decision = restart_decision_record(
        issue_type,
        restart_codex_now,
        restart_recommended,
        restart_timing,
        restart_reason,
    )

    return {
        "status": "ok" if issue_type == "healthy" else "attention",
        "issue_type": issue_type,
        "direct_fix_available": False,
        "direct_fix_boundary": direct_fix_boundary(),
        "connection_fix_scope": "local recovery, diagnosis, checkpointing, and restart guidance only",
        "transport_unreliable": transport_unreliable,
        "app_state_unstable": app_state_unstable,
        "auth_session_unhealthy": auth_session_unhealthy,
        "restart_codex_now": restart_codex_now,
        "restart_recommended": restart_recommended,
        "restart_timing": restart_timing,
        "restart_reason": restart_reason,
        "restart_decision": restart_decision,
        "transport_patterns": transport_patterns,
        "app_state_patterns": app_state_patterns,
        "auth_session_patterns": auth_session_patterns,
        "compaction_patterns": compaction_patterns,
        "no_progress_patterns": no_progress_patterns,
        "primary_action": primary_action,
        "restart_rule": "Restart Codex after checkpointing when app-state events repeat without transport failures.",
    }


def render_health_markdown(report: dict[str, Any]) -> str:
    health = report["health"]
    boundary = health.get("direct_fix_boundary") or direct_fix_boundary()
    lines = [
        "# Codex Guardian Health",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Status: `{health['status']}`",
        f"- Issue type: `{health['issue_type']}`",
        f"- Direct fix available: `{str(health['direct_fix_available']).lower()}`",
        f"- Restart Codex now: `{str(health['restart_codex_now']).lower()}`",
        f"- Restart recommended: `{str(health['restart_recommended']).lower()}`",
        f"- Restart timing: `{health['restart_timing']}`",
        f"- Restart decision: `{health['restart_decision']['decision']}`",
        f"- Restart first action: `{health['restart_decision']['first_action']}`",
        f"- Restart reason: {health['restart_reason']}",
        f"- Primary action: {health['primary_action']}",
        "",
        "## Direct-Fix Boundary",
        "",
        f"- Direct fix ceiling score: `{boundary['direct_fix_ceiling_score']}/10`",
        f"- Recovery tooling ceiling score: `{boundary['recovery_tooling_ceiling_score']}/10`",
        f"- Highest local recovery command: `{boundary['highest_local_recovery_command']}`",
        f"- Full bundle command: `{boundary['full_bundle_command']}`",
        f"- Boundary reason: {boundary['boundary_reason']}",
        "",
        "## Pattern Groups",
        "",
        f"- Transport: {', '.join(health['transport_patterns']) or 'none'}",
        f"- App state: {', '.join(health['app_state_patterns']) or 'none'}",
        f"- Auth/session: {', '.join(health['auth_session_patterns']) or 'none'}",
        f"- Compaction: {', '.join(health['compaction_patterns']) or 'none'}",
        f"- No progress: {', '.join(health['no_progress_patterns']) or 'none'}",
    ]
    lines += reachability_markdown_section(report)
    lines += service_status_markdown_section(report)
    artifacts = []
    if report.get("recovery_report"):
        artifacts.append(f"- Recovery bundle: `{report['recovery_report']}`")
    if report.get("preflight_checkpoint"):
        artifacts.append(f"- Preflight checkpoint: `{report['preflight_checkpoint']}`")
    marker = report.get("restart_marker")
    if marker:
        artifacts.append(f"- Restart marker: `{marker['path']}`")
    if report.get("doctor"):
        artifacts.append("- Doctor files: `doctor.md`, `doctor.json`")
        artifacts.append("- Reachability files: `reachability.md`, `reachability.json`")
        artifacts.append("- Service status files: `service-status.md`, `service-status.json`")
        artifacts.append("- Connection triage files: `connection-triage.md`, `connection-triage.json`")
    if artifacts:
        lines += ["", "## Artifacts", "", *artifacts]
    return "\n".join(lines) + "\n"


def render_health_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_health_markdown(report)


def parse_report_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lookback_hours_covering_since(requested_hours: int, since: datetime) -> int:
    age_seconds = max(0, int((datetime.now(timezone.utc) - utc_datetime(since)).total_seconds()))
    marker_hours = ((age_seconds + 3599) // 3600) + 1
    return max(requested_hours, marker_hours)


def events_since(events: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    since = utc_datetime(since)
    selected = []
    for event in events:
        event_time = parse_report_time(str(event.get("time", "")))
        if event_time:
            event_time = utc_datetime(event_time)
        if event_time and event_time >= since:
            selected.append(event)
    return selected


def post_restart_assessment(events: list[dict[str, Any]], since: datetime, activity_count: int) -> dict[str, Any]:
    recent = events_since(events, since)
    summary = summarize(recent)
    counts = summary["pattern_counts"]
    app_state_patterns = [code for code in APP_STATE_CODES if counts.get(code)]
    transport_patterns = [code for code in TRANSPORT_CODES if counts.get(code)]
    still_unstable = bool(app_state_patterns)
    if still_unstable:
        status = "still_unstable"
        actions = [
            "App-state errors continued after the restart marker.",
            "Create a fresh doctor bundle and restart Codex again only after preserving active state.",
        ]
    elif activity_count == 0:
        status = "no_activity"
        actions = [
            "No Codex log activity was found after the restart marker.",
            "Open or restart Codex, run a small action, then run post-restart again from the same project.",
        ]
    elif transport_patterns:
        status = "transport_unreliable"
        actions = [
            "No app-state errors were found after the restart marker.",
            "Transport errors remain; resume with a smaller prompt and verify file state before retrying broad work.",
        ]
    else:
        status = "clean"
        actions = ["No app-state errors were found after the restart marker."]
    return {
        "status": status,
        "since": since.replace(microsecond=0).isoformat(),
        "marker_path": None,
        "event_count_after_restart": len(recent),
        "activity_count_after_restart": activity_count,
        "app_state_patterns_after_restart": app_state_patterns,
        "transport_patterns_after_restart": transport_patterns,
        "actions": actions,
    }


def render_post_restart_markdown(report: dict[str, Any]) -> str:
    post = report["post_restart"]
    lines = [
        "# Codex Guardian Post-Restart Check",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Since: `{post['since']}`",
        f"- Status: `{post['status']}`",
        f"- Events after restart: {post['event_count_after_restart']}",
        f"- Log activity after restart: {post['activity_count_after_restart']}",
    ]
    marker = report.get("restart_marker")
    if marker:
        lines.extend([
            "",
            "## Restart Marker",
            "",
            f"- Path: `{marker['path']}`",
        ])
        marker_fields = [
            ("source", "Source"),
            ("issue_type", "Issue type"),
            ("restart_timing", "Restart timing"),
            ("restart_recommended", "Restart recommended"),
            ("restart_codex_now", "Restart Codex now"),
            ("reason", "Reason"),
            ("restart_reason", "Restart reason"),
        ]
        for key, label in marker_fields:
            if key in marker and marker[key] is not None:
                lines.append(f"- {label}: `{marker[key]}`")
    lines.extend([
        "",
        "## Actions",
        "",
    ])
    for action in post["actions"]:
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def render_post_restart_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_post_restart_markdown(report)


def latest_recovery_bundle(project: Path) -> Optional[Path]:
    recovery_root = guardian_dir(project) / "recovery"
    if not recovery_root.exists():
        return None
    bundles = [path for path in recovery_root.iterdir() if path.is_dir()]
    if not bundles:
        return None
    return max(bundles, key=lambda path: (path.stat().st_mtime_ns, path.name))


def checkpoint_due_status(checkpoint: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not checkpoint:
        return {"checkpoint_due_at": None, "checkpoint_overdue": False}
    due_at = checkpoint.get("checkpoint_due_at")
    due = parse_report_time(str(due_at or ""))
    overdue = False
    if due and checkpoint.get("status") == "in_progress":
        overdue = utc_datetime(due) < datetime.now(timezone.utc)
    return {"checkpoint_due_at": due_at, "checkpoint_overdue": overdue}


def read_current_checkpoint(project: Path) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    path = guardian_dir(project) / "current.json"
    try:
        checkpoint = load_checkpoint(project)
    except FileNotFoundError:
        return None, {"checkpoint_due_at": None, "checkpoint_overdue": False}
    except (OSError, json.JSONDecodeError) as exc:
        return None, {
            "checkpoint_due_at": None,
            "checkpoint_overdue": False,
            "checkpoint_read_error_path": str(path),
            "checkpoint_read_error": str(exc),
        }
    return checkpoint, checkpoint_due_status(checkpoint)


def status_fresh_bundle_recommended(health: dict[str, Any], status: dict[str, Any]) -> bool:
    post = status.get("post_restart") or {}
    return bool(
        health.get("status") != "ok"
        or post.get("status") == "still_unstable"
        or post.get("status") == "transport_unreliable"
        or status.get("checkpoint_read_error")
        or status.get("checkpoint_overdue")
    )


def status_restart_marker_context(status: dict[str, Any]) -> Optional[dict[str, Any]]:
    post = status.get("post_restart") or {}
    if post.get("status") != "still_unstable":
        return None
    return {
        "issue_type": "post_restart_still_unstable",
        "restart_timing": "after_state_preserved",
        "restart_reason": "Post-restart app-state errors continued; preserve state before restarting Codex again.",
        "restart_recommended": True,
        "restart_codex_now": False,
        "restart_decision": restart_decision_record(
            "post_restart_still_unstable",
            False,
            True,
            "after_state_preserved",
            "Post-restart app-state errors continued; preserve state before restarting Codex again.",
        ),
    }


def full_recovery_command(hours: int, mark_restart: bool = False) -> str:
    command = f"recover-now --project . --hours {hours}"
    if not mark_restart:
        command += " --no-mark-restart"
    return command


def status_next_actions(health: dict[str, Any], status: dict[str, Any], hours: int) -> list[str]:
    recovery_command = full_recovery_command(hours)
    restart_recovery_command = full_recovery_command(hours, mark_restart=True)
    post_restart_command = f"post-restart --project . --hours {hours}"
    post = status.get("post_restart") or {}
    post_status = post.get("status")
    issue_type = health.get("issue_type")

    if status.get("checkpoint_read_error"):
        return [
            "The current checkpoint could not be read; inspect `.codex-guardian/current.json` before continuing.",
            f"Run `{recovery_command}` if current task state is unclear.",
        ]
    if status.get("checkpoint_overdue"):
        return [
            "The active checkpoint is overdue; write a fresh checkpoint before continuing the task.",
            f"Run `{recovery_command}` if current task state is unclear.",
        ]
    if post_status == "still_unstable":
        return [
            f"Run `{restart_recovery_command}` to create a fresh full recovery bundle before restarting Codex again.",
            f"After restart, run `{post_restart_command}` from the same project.",
        ]
    if post_status == "no_activity":
        return [
            "Open or restart Codex, run one small action, then verify the marker again.",
            f"Run `{post_restart_command}` from the same project after that action.",
        ]
    if post_status == "transport_unreliable":
        return [
            f"Run `{recovery_command}` to create a fresh full recovery bundle before retrying.",
            "Resume with a smaller prompt and verify file state before retrying broad work.",
        ]
    if health.get("status") == "ok":
        return ["No immediate recovery action is required. Continue from the active checkpoint if one exists."]
    if issue_type == "mixed" or (issue_type == "app_state" and health.get("restart_recommended")):
        return [
            f"Run `{restart_recovery_command}` to preserve state and seed post-restart verification.",
            "Restart Codex only after the bundle and checkpoint exist.",
            f"After restart, run `{post_restart_command}` from the same project.",
        ]
    if issue_type == "app_state":
        return [
            f"Run `{recovery_command}` to preserve state if current task state is unclear.",
            "Checkpoint active work and retry one small action; restart only if app-state events repeat.",
        ]
    if issue_type == "transport":
        return [
            f"Run `{recovery_command}` to create a fresh full recovery bundle before retrying.",
            "Resume with a smaller prompt and verify file state before retrying broad work.",
        ]
    if issue_type == "auth_session":
        return [
            f"Run `{recovery_command}` if current task state is unclear.",
            "Sign in or refresh the Codex session before retrying the request.",
            "Restart the app only if it still reports stale auth after reauth.",
        ]
    if issue_type == "compaction":
        return [
            f"Run `{recovery_command}` to preserve state, then resume with a smaller context slice.",
        ]
    if issue_type == "no_progress":
        return [
            "Name one next file or behavior and write a checkpoint before continuing.",
            f"Run `{recovery_command}` if the next slice still makes no verified progress.",
        ]
    return [health.get("primary_action") or "Review health output before continuing."]


def build_status_report(codex_home: Path, project: Path, hours: int, limit: int) -> dict[str, Any]:
    report = build_report(codex_home, hours, limit)
    report["health"] = health_assessment(report["summary"])
    status: dict[str, Any] = {
        "project": str(project),
        "checkpoint_present": False,
        "checkpoint": None,
        "latest_recovery_bundle": None,
        "restart_marker_present": False,
        "restart_marker": None,
    }
    checkpoint, checkpoint_attention = read_current_checkpoint(project)
    if checkpoint:
        status["checkpoint_present"] = True
        status["checkpoint"] = checkpoint
    status.update(checkpoint_attention)
    bundle = latest_recovery_bundle(project)
    if bundle:
        status["latest_recovery_bundle"] = str(bundle)
    try:
        marker = load_restart_marker(project)
    except (OSError, json.JSONDecodeError):
        marker = None
    if marker:
        marker_path = restart_marker_path(project)
        status["restart_marker_present"] = True
        status["restart_marker"] = restart_marker_summary(marker_path, marker)
        since = parse_report_time(str(marker.get("created_at", "")))
        if since:
            since = utc_datetime(since)
            effective_hours = lookback_hours_covering_since(hours, since)
            post_report = build_report(codex_home, effective_hours, limit)
            activity_count = count_log_activity_since(codex_home, effective_hours, since)
            post = post_restart_assessment(post_report["events"], since, activity_count)
            post["marker_path"] = str(marker_path)
            status["post_restart"] = post
    status["fresh_recovery_bundle_recommended"] = status_fresh_bundle_recommended(report["health"], status)
    status["next_actions"] = status_next_actions(report["health"], status, hours)
    report["status"] = status
    return report


def render_status_markdown(report: dict[str, Any]) -> str:
    health = report["health"]
    status = report["status"]
    lines = [
        "# Codex Guardian Status",
        "",
        f"- Generated: {report['generated_at']}",
        "",
        "## Current Health",
        "",
        f"- Issue type: `{health['issue_type']}`",
        f"- Restart Codex now: `{str(health['restart_codex_now']).lower()}`",
        f"- Primary action: {health['primary_action']}",
        "",
        "## Recovery State",
        "",
        f"- Checkpoint present: `{str(status['checkpoint_present']).lower()}`",
    ]
    checkpoint = status.get("checkpoint")
    if checkpoint:
        lines.append(f"- Checkpoint task: {checkpoint.get('task', 'unknown')}")
        lines.append(f"- Checkpoint phase: `{checkpoint.get('phase', 'unknown')}`")
        if status.get("checkpoint_due_at"):
            lines.append(f"- Checkpoint due at: `{status['checkpoint_due_at']}`")
        lines.append(f"- Checkpoint overdue: `{str(status['checkpoint_overdue']).lower()}`")
    if status.get("checkpoint_read_error"):
        lines.append(f"- Checkpoint read error: `{status['checkpoint_read_error_path']}`")
    bundle = status.get("latest_recovery_bundle")
    lines.append(f"- Latest recovery bundle: `{bundle or 'none'}`")
    lines.append(f"- Fresh recovery bundle recommended: `{str(status['fresh_recovery_bundle_recommended']).lower()}`")
    marker = status.get("restart_marker")
    if marker:
        lines.append(f"- Restart marker: `{marker['path']}`")
        lines.append(f"- Restart reason: {marker.get('reason') or 'unknown'}")
    else:
        lines.append("- Restart marker: `none`")
    post = status.get("post_restart")
    if post:
        lines.append(f"- Post-restart status: `{post['status']}`")
        for action in post["actions"]:
            lines.append(f"- {action}")
    lines += ["", "## Next Actions", ""]
    for action in status["next_actions"]:
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def render_status_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_status_markdown(report)


def connection_recovery_attention(status: dict[str, Any]) -> str:
    if reachability_needs_attention(status.get("reachability")):
        return "reachability_failed"
    if service_status_needs_attention(status.get("service_status")):
        return "upstream_degraded"
    if status.get("checkpoint_read_error"):
        return "checkpoint_read_error"
    if status.get("checkpoint_overdue"):
        return "checkpoint_overdue"
    post_status = (status.get("post_restart") or {}).get("status")
    if post_status == "still_unstable":
        return "post_restart_still_unstable"
    if post_status == "no_activity":
        return "post_restart_no_activity"
    if post_status == "transport_unreliable":
        return "post_restart_transport_unreliable"
    return "none"


def connection_escalation_packet(report: dict[str, Any], recovery_attention: str) -> dict[str, Any]:
    health = report["health"]
    evidence = [
        "connection-triage.md",
        "connection-triage.json",
        "status.md",
        "status.json",
        "diagnosis.md",
        "diagnosis.json",
        "events.json",
        "resume-prompt.txt",
    ]
    if report.get("reachability"):
        evidence.extend(["reachability.md", "reachability.json"])
    if report.get("service_status"):
        evidence.extend(["service-status.md", "service-status.json"])
    return {
        "local_direct_fix_available": False,
        "issue_type": health["issue_type"],
        "recovery_attention": recovery_attention,
        "restart_decision": health.get("restart_decision"),
        "external_owner": "Codex/OpenAI product layer, local network, or auth provider depending on the preserved evidence.",
        "when_to_escalate": "Escalate only after preserving state and collecting a fresh recovery bundle, or when the same transport, auth, backend, or app-state failure persists after the recommended local action.",
        "evidence_to_preserve": evidence,
        "do_not_share": [
            "auth tokens or session files",
            "raw unredacted logs",
            "private transcripts",
            "unredacted home paths or project secrets",
        ],
    }


def direct_fix_boundary() -> dict[str, Any]:
    return {
        "direct_fix_available": False,
        "direct_fix_ceiling_score": 3,
        "recovery_tooling_ceiling_score": 9,
        "highest_local_recovery_command": "doctor --project . --hours 1",
        "full_bundle_command": "recover-now --project . --hours 1",
        "boundary_reason": (
            "Guardian can preserve state, classify health, check reachability and upstream status, "
            "guide restart or reauth, and create resume material. It cannot patch Codex app internals, "
            "OpenAI backend availability, auth/session bugs, WebSocket transport, or local network failures."
        ),
    }


def attach_connection_triage(report: dict[str, Any], local_actions: list[str]) -> dict[str, Any]:
    health = report["health"]
    status = report.get("status") or {}
    actions = list(local_actions)
    if reachability_needs_attention(report.get("reachability")):
        actions.append("Open the reachability section and fix the local DNS, TLS, proxy, or timeout issue before retrying Codex.")
    if service_status_needs_attention(report.get("service_status")):
        actions.append("Upstream service status is degraded; preserve state and wait or retry later instead of changing local files repeatedly.")
    recovery_attention = connection_recovery_attention({**status, "reachability": report.get("reachability"), "service_status": report.get("service_status")})
    report["connection_triage"] = {
        "direct_fix_available": False,
        "local_fix_ceiling": "recovery and local restart guidance",
        "local_fix_scope": health["connection_fix_scope"],
        "issue_type": health["issue_type"],
        "recovery_attention": recovery_attention,
        "direct_fix_boundary": direct_fix_boundary(),
        "local_actions": actions,
        "escalation_packet": connection_escalation_packet(report, recovery_attention),
        "external_boundaries": [
            "Cannot patch Codex app internals, OpenAI backend availability, auth/session bugs, WebSocket transport, or local network failures.",
            "Escalate persistent transport, auth, backend, or app bugs to the Codex/OpenAI product layer after preserving local state.",
        ],
    }
    return report


def build_connection_triage_report(codex_home: Path, project: Path, hours: int, limit: int) -> dict[str, Any]:
    report = build_status_report(codex_home, project, hours, limit)
    return attach_connection_triage(report, report["status"]["next_actions"])


def render_connection_triage_markdown(report: dict[str, Any]) -> str:
    health = report["health"]
    triage = report["connection_triage"]
    lines = [
        "# Codex Guardian Connection Triage",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Issue type: `{health['issue_type']}`",
        f"- Direct fix available: `{str(triage['direct_fix_available']).lower()}`",
        f"- Local fix ceiling: {triage['local_fix_ceiling']}",
        f"- Local fix scope: {triage['local_fix_scope']}",
        f"- Recovery attention: `{triage['recovery_attention']}`",
        "",
        "## Direct-Fix Ceiling",
        "",
        f"- Direct fix ceiling score: `{triage['direct_fix_boundary']['direct_fix_ceiling_score']}/10`",
        f"- Recovery tooling ceiling score: `{triage['direct_fix_boundary']['recovery_tooling_ceiling_score']}/10`",
        f"- Highest local recovery command: `{triage['direct_fix_boundary']['highest_local_recovery_command']}`",
        f"- Full bundle command: `{triage['direct_fix_boundary']['full_bundle_command']}`",
        f"- Boundary reason: {triage['direct_fix_boundary']['boundary_reason']}",
        "",
        "## Local Actions",
        "",
    ]
    for action in triage["local_actions"]:
        lines.append(f"- {action}")
    packet = triage["escalation_packet"]
    lines += [
        "",
        "## Escalation Packet",
        "",
        f"- Local direct fix available: `{str(packet['local_direct_fix_available']).lower()}`",
        f"- External owner: {packet['external_owner']}",
        f"- When to escalate: {packet['when_to_escalate']}",
        "",
        "Evidence to preserve:",
        "",
    ]
    for item in packet["evidence_to_preserve"]:
        lines.append(f"- `{item}`")
    lines += ["", "Do not share:", ""]
    for item in packet["do_not_share"]:
        lines.append(f"- {item}")
    lines += reachability_markdown_section(report)
    lines += service_status_markdown_section(report)
    lines += ["", "## External Boundaries", ""]
    for boundary in triage["external_boundaries"]:
        lines.append(f"- {boundary}")
    return "\n".join(lines) + "\n"


def render_connection_triage_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_connection_triage_markdown(report)


def doctor_restart_recommended(health: dict[str, Any]) -> bool:
    return bool(health.get("restart_recommended") or health.get("restart_codex_now") or health.get("issue_type") == "mixed")


def reachability_needs_attention(reachability_report: Optional[dict[str, Any]]) -> bool:
    return bool((reachability_report or {}).get("reachability", {}).get("local_network_issue"))


def service_status_needs_attention(service_status_report: Optional[dict[str, Any]]) -> bool:
    return bool((service_status_report or {}).get("service_status", {}).get("upstream_issue"))


def restart_marker_summary(marker_path: Path, marker_payload: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "path": str(marker_path),
        "created_at": marker_payload.get("created_at"),
        "reason": marker_payload.get("reason"),
    }
    for key in (
        "source",
        "issue_type",
        "restart_timing",
        "restart_reason",
        "restart_recommended",
        "restart_codex_now",
        "restart_decision",
    ):
        if key in marker_payload:
            summary[key] = marker_payload[key]
    if "restart_decision" not in summary and marker_payload.get("issue_type"):
        summary["restart_decision"] = restart_decision_record(
            marker_payload["issue_type"],
            bool(marker_payload.get("restart_codex_now")),
            bool(marker_payload.get("restart_recommended")),
            marker_payload.get("restart_timing") or "not_first_action",
            marker_payload.get("restart_reason") or marker_payload.get("reason") or "Restart marker did not include a restart reason.",
        )
    return summary


def attach_restart_marker(project: Path, report: dict[str, Any], source: str, marker_context: Optional[dict[str, Any]] = None) -> Path:
    health = report["health"]
    context = marker_context or {
        "issue_type": health["issue_type"],
        "restart_timing": health.get("restart_timing"),
        "restart_reason": health.get("restart_reason"),
        "restart_recommended": health.get("restart_recommended"),
        "restart_codex_now": health.get("restart_codex_now"),
        "restart_decision": health.get("restart_decision"),
    }
    marker_path = write_restart_marker(
        project,
        f"{source} recommended restart for {context['issue_type']}",
        {
            "source": source,
            **context,
        },
    )
    marker_payload = load_restart_marker(project)
    report["restart_marker"] = restart_marker_summary(marker_path, marker_payload)
    return marker_path


def doctor_actions(
    health: dict[str, Any],
    bundle_path: Optional[Path],
    checkpoint_path: Optional[Path] = None,
    marker_path: Optional[Path] = None,
    checkpoint_attention: Optional[dict[str, Any]] = None,
    status: Optional[dict[str, Any]] = None,
    reachability_report: Optional[dict[str, Any]] = None,
    service_status_report: Optional[dict[str, Any]] = None,
) -> list[str]:
    issue_type = health["issue_type"]
    attention = checkpoint_attention or {}
    status = status or {}
    checkpoint_overdue = bool(attention.get("checkpoint_overdue"))
    checkpoint_read_error = bool(attention.get("checkpoint_read_error"))
    status_needs_attention = bool(status.get("fresh_recovery_bundle_recommended"))
    reachability_attention = reachability_needs_attention(reachability_report)
    service_status_attention = service_status_needs_attention(service_status_report)
    if (
        issue_type == "healthy"
        and not checkpoint_overdue
        and not checkpoint_read_error
        and not status_needs_attention
        and not reachability_attention
        and not service_status_attention
    ):
        if checkpoint_path:
            return [
                f"Preflight checkpoint written: {checkpoint_path}",
                "No known Codex connection failure was found in the sampled logs.",
            ]
        return ["No known Codex connection failure was found in the sampled logs."]

    if checkpoint_path:
        actions = [f"Preflight checkpoint written: {checkpoint_path}"]
        if checkpoint_read_error:
            actions.append("The previous current checkpoint could not be read before this preflight; inspect `.codex-guardian/checkpoints/` if older task state matters.")
        elif checkpoint_overdue:
            actions.append("The previous active checkpoint was overdue; this preflight checkpoint now records the current task state.")
    elif checkpoint_read_error:
        actions = ["The current checkpoint could not be read; inspect `.codex-guardian/current.json` before continuing."]
    elif checkpoint_overdue:
        actions = ["The active checkpoint is overdue; write a fresh checkpoint before continuing."]
    else:
        actions = ["Checkpoint active work before changing app or task state."]
    if bundle_path:
        actions.append(f"Use recovery bundle: {bundle_path}")
        actions.append("Open `status.md` in that bundle for the current recovery state and next actions.")
    if marker_path:
        actions.append(f"Restart marker written: {marker_path}")
        actions.append("After restarting Codex, run `post-restart --project .` from the same project.")

    if reachability_attention:
        status_code = reachability_report["reachability"]["status"]
        actions.append(f"Reachability check failed with `{status_code}`; open `reachability.md` before retrying Codex.")
    if service_status_attention:
        actions.append("Upstream service status is degraded; preserve state and wait or retry later instead of changing local files repeatedly.")

    if issue_type == "app_state":
        if health.get("restart_recommended"):
            actions.append("Restart Codex after the checkpoint and bundle are preserved.")
            actions.append("Resume with the generated prompt instead of retrying the broad prior turn.")
        else:
            actions.append("Checkpoint active work and retry one small action.")
            actions.append("Restart only if app-state events repeat after state is preserved.")
    elif issue_type == "auth_session":
        actions.append("Sign in or refresh the Codex session before retrying the request.")
        actions.append("Only restart the app if it still reports stale auth after reauth.")
    elif issue_type == "transport":
        actions.append("Do not restart first; verify file state and resume from a smaller prompt.")
        actions.append("If transport failures repeat after a small retry, preserve state and restart Codex.")
    elif issue_type == "mixed":
        actions.append("Restart Codex after preserving state because app state and transport failures both appear.")
        actions.append("Resume only from the generated prompt and touched files.")
    elif issue_type == "compaction":
        actions.append("Stop adding context and continue in a smaller thread or slice.")
        actions.append("Use the generated prompt to name only the next files to read.")
    elif issue_type == "no_progress":
        actions.append("Name one next file or behavior, then stop if the next slice makes no verified progress.")
    elif checkpoint_read_error:
        actions.append("Use the recovery bundle to preserve logs and diagnose before repairing or replacing the checkpoint.")
    elif checkpoint_overdue:
        actions.append("Verify current file state against the bundled checkpoint before resuming.")
    elif status_needs_attention:
        actions.extend(status.get("next_actions") or [health["primary_action"]])
    elif not service_status_attention:
        actions.append(health["primary_action"])
    return actions


def render_doctor_markdown(report: dict[str, Any]) -> str:
    health = report["health"]
    doctor = report["doctor"]
    lines = [
        "# Codex Guardian Doctor",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Issue type: `{health['issue_type']}`",
        f"- Created recovery bundle: `{str(doctor['created_recovery_bundle']).lower()}`",
        f"- Direct fix available: `{str(health['direct_fix_available']).lower()}`",
        "",
        "## Actions",
        "",
    ]
    for action in doctor["actions"]:
        lines.append(f"- {action}")
    if report.get("recovery_report"):
        lines += ["", f"Recovery bundle: `{report['recovery_report']}`"]
    lines += reachability_markdown_section(report)
    lines += service_status_markdown_section(report)
    return "\n".join(lines) + "\n"


def render_doctor_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_doctor_markdown(report)


def write_connection_triage_files(bundle: Path, report: dict[str, Any], hours: int) -> None:
    status = report.get("status") or report.get("checkpoint_attention") or {}
    local_actions = status.get("next_actions") or (report.get("doctor") or {}).get("actions") or status_next_actions(report["health"], status, hours)
    triage_report = attach_connection_triage(json.loads(json.dumps(report, ensure_ascii=False)), local_actions)
    (bundle / "connection-triage.json").write_text(json.dumps(triage_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "connection-triage.md").write_text(render_connection_triage_markdown(triage_report), encoding="utf-8")


def write_status_files(bundle: Path, status_report: dict[str, Any]) -> None:
    redacted_status = json.loads(json.dumps(status_report, ensure_ascii=False))
    (bundle / "status.json").write_text(json.dumps(redacted_status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "status.md").write_text(render_status_markdown(redacted_status), encoding="utf-8")


def write_reachability_files(
    bundle: Path,
    endpoint: str = DEFAULT_REACHABILITY_ENDPOINT,
    timeout: float = 5.0,
    dns_only: bool = False,
    report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    report = report or build_reachability_report(endpoint, timeout, dns_only)
    (bundle / "reachability.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "reachability.md").write_text(render_reachability_markdown(report), encoding="utf-8")
    return report


def write_service_status_files(
    bundle: Path,
    endpoint: str = DEFAULT_SERVICE_STATUS_ENDPOINT,
    timeout: float = 5.0,
    report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    report = report or build_service_status_report(endpoint, timeout)
    (bundle / "service-status.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "service-status.md").write_text(render_service_status_markdown(report), encoding="utf-8")
    return report


def codex_cli_snapshot() -> dict[str, Any]:
    path = shutil.which("codex")
    if not path:
        return {"path": None, "version": None, "version_status": "not_found"}
    info: dict[str, Any] = {
        "path": redact(path),
        "version": None,
        "version_status": "unknown",
    }
    try:
        result = subprocess.run([path, "--version"], text=True, capture_output=True, timeout=2, check=False)
    except subprocess.TimeoutExpired:
        info["version_status"] = "timeout"
        return info
    except OSError as exc:
        info["version_status"] = "error"
        info["error"] = redact(str(exc))
        return info
    output = (result.stdout or result.stderr).strip()
    if output:
        info["version"] = redact(output.splitlines()[0][:200])
    info["version_status"] = "ok" if result.returncode == 0 else "error"
    info["returncode"] = result.returncode
    return info


def path_snapshot(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": redact(str(path)),
        "exists": path.exists(),
    }
    if not path.exists():
        return snapshot
    try:
        stat = path.stat()
    except OSError as exc:
        snapshot["stat_error"] = redact(str(exc))
        return snapshot
    snapshot["size_bytes"] = stat.st_size
    snapshot["modified_at"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat()
    return snapshot


def log_source_snapshot(codex_home_text: Optional[str], hours: int = 48) -> dict[str, Any]:
    codex_home = Path(codex_home_text or str(default_codex_home())).expanduser()
    desktop_dirs = desktop_log_dirs(hours)
    desktop_files: list[Path] = []
    for directory in desktop_dirs:
        try:
            desktop_files.extend(path for path in directory.glob("*.log") if path.is_file())
        except OSError:
            continue
    latest_desktop_log = max(desktop_files, key=lambda path: path.stat().st_mtime) if desktop_files else None
    return {
        "lookback_hours": hours,
        "sqlite_log": path_snapshot(codex_home / "logs_2.sqlite"),
        "desktop_log_base": path_snapshot(Path.home() / "Library" / "Logs" / "com.openai.codex"),
        "desktop_log_dirs_found": len(desktop_dirs),
        "desktop_log_files_found": len(desktop_files),
        "latest_desktop_log": path_snapshot(latest_desktop_log) if latest_desktop_log else None,
    }


def build_environment_report(report: dict[str, Any], project: Path) -> dict[str, Any]:
    return {
        "schema": "codex-guardian.environment.v1",
        "generated_at": now_utc(),
        "project": redact(str(project)),
        "codex_home": report.get("codex_home"),
        "reachability_endpoint": (report.get("reachability") or {}).get("endpoint", DEFAULT_REACHABILITY_ENDPOINT),
        "python": {
            "version": platform.python_version(),
            "executable": redact(sys.executable),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "codex_cli": codex_cli_snapshot(),
        "log_sources": log_source_snapshot(report.get("codex_home")),
    }


def render_environment_markdown(report: dict[str, Any]) -> str:
    codex_cli = report["codex_cli"]
    lines = [
        "# Codex Guardian Environment",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Project: `{report['project']}`",
        f"- Codex home: `{report.get('codex_home') or 'unknown'}`",
        f"- Reachability endpoint: `{report['reachability_endpoint']}`",
        "",
        "## Runtime",
        "",
        f"- Python: `{report['python']['version']}`",
        f"- Python executable: `{report['python']['executable']}`",
        f"- Platform: `{report['platform']['system']} {report['platform']['release']} {report['platform']['machine']}`",
        "",
        "## Codex CLI",
        "",
        f"- Path: `{codex_cli.get('path') or 'not found'}`",
        f"- Version status: `{codex_cli.get('version_status')}`",
    ]
    if codex_cli.get("version"):
        lines.append(f"- Version: `{codex_cli['version']}`")
    if codex_cli.get("error"):
        lines.append(f"- Error: `{codex_cli['error']}`")
    log_sources = report.get("log_sources") or {}
    sqlite_log = log_sources.get("sqlite_log") or {}
    lines += [
        "",
        "## Log Sources",
        "",
        f"- SQLite log: `{sqlite_log.get('path') or 'unknown'}`",
        f"- SQLite log exists: `{str(sqlite_log.get('exists', False)).lower()}`",
        f"- Desktop log base exists: `{str((log_sources.get('desktop_log_base') or {}).get('exists', False)).lower()}`",
        f"- Desktop log dirs found: `{log_sources.get('desktop_log_dirs_found', 0)}`",
        f"- Desktop log files found: `{log_sources.get('desktop_log_files_found', 0)}`",
    ]
    latest = log_sources.get("latest_desktop_log")
    if latest:
        lines.append(f"- Latest desktop log: `{latest.get('path')}`")
    return "\n".join(lines) + "\n"


def write_environment_files(bundle: Path, report: dict[str, Any]) -> dict[str, Any]:
    project = bundle.parents[2]
    environment = build_environment_report(report, project)
    (bundle / "environment.json").write_text(json.dumps(environment, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "environment.md").write_text(render_environment_markdown(environment), encoding="utf-8")
    return environment


def add_status_guidance_to_resume_prompt(bundle: Path, next_artifact: str = "doctor.md") -> None:
    path = bundle / "resume-prompt.txt"
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8")
    if "Open `status.md` first" in existing:
        return
    guidance = (
        "Open `status.md` first for the current recovery state and next actions. "
        f"Then use `{next_artifact}` for the ordered local actions before continuing.\n\n"
    )
    path.write_text(guidance + existing, encoding="utf-8")


def write_doctor_files(bundle: Path, report: dict[str, Any], hours: int, status_report: Optional[dict[str, Any]] = None) -> None:
    redacted_report = json.loads(json.dumps(report, ensure_ascii=False))
    if status_report:
        write_status_files(bundle, status_report)
        redacted_report["status"] = status_report["status"]
    if not redacted_report.get("service_status"):
        redacted_report["service_status"] = build_service_status_report()
    (bundle / "doctor.json").write_text(json.dumps(redacted_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "doctor.md").write_text(render_doctor_markdown(redacted_report), encoding="utf-8")
    write_reachability_files(bundle, report=redacted_report.get("reachability"))
    write_service_status_files(bundle, report=redacted_report.get("service_status"))
    redacted_report["environment"] = write_environment_files(bundle, redacted_report)
    write_connection_triage_files(bundle, redacted_report, hours)
    if status_report:
        add_status_guidance_to_resume_prompt(bundle)
    write_recovery_bundle_index(bundle, redacted_report)


def recovery_bundle_open_first(bundle: Path) -> list[str]:
    order = ["README.md"]
    for filename in ("doctor.md", "status.md", "reachability.md", "service-status.md", "environment.md", "connection-triage.md", "resume-prompt.txt", "diagnosis.md", "checkpoint.json", "events.json"):
        if (bundle / filename).exists():
            order.append(filename)
    return order


def render_recovery_bundle_readme(manifest: dict[str, Any]) -> str:
    lines = [
        "# Codex Guardian Recovery Bundle",
        "",
        f"- Created: {manifest['created_at']}",
        f"- Checkpoint present: `{str(manifest['checkpoint_present']).lower()}`",
    ]
    health = manifest.get("health")
    if health:
        lines.append(f"- Issue type: `{health.get('issue_type', 'unknown')}`")
        lines.append(f"- Restart Codex now: `{str(health.get('restart_codex_now', False)).lower()}`")
    checkpoint_attention = manifest.get("checkpoint_attention") or {}
    if checkpoint_attention.get("checkpoint_read_error"):
        lines.append("- Checkpoint attention: `checkpoint could not be read`")
        lines.append(f"- Checkpoint path: `{checkpoint_attention.get('checkpoint_read_error_path', '.codex-guardian/current.json')}`")
    elif checkpoint_attention.get("checkpoint_overdue"):
        lines.append("- Checkpoint attention: `checkpoint overdue`")
        if checkpoint_attention.get("checkpoint_due_at"):
            lines.append(f"- Checkpoint due at: `{checkpoint_attention['checkpoint_due_at']}`")
    lines += [
        "",
        "## Open First",
        "",
    ]
    for filename in manifest["open_first"]:
        lines.append(f"- `{filename}`")
    lines += [
        "",
        "## Files",
        "",
    ]
    for filename in manifest["files"]:
        lines.append(f"- `{filename}`")
    return "\n".join(lines) + "\n"


def write_recovery_bundle_index(bundle: Path, report: dict[str, Any]) -> None:
    files = sorted(path.name for path in bundle.iterdir() if path.is_file())
    for filename in ("manifest.json", "README.md"):
        if filename not in files:
            files.append(filename)
    files = sorted(files)
    manifest = {
        "schema": "codex-guardian.recovery-bundle.v1",
        "created_at": now_utc(),
        "bundle": redact(str(bundle)),
        "open_first": recovery_bundle_open_first(bundle),
        "files": files,
        "checkpoint_present": (bundle / "checkpoint.json").exists(),
        "summary": report.get("summary", {}),
        "health": report.get("health"),
        "checkpoint_attention": report.get("checkpoint_attention"),
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "README.md").write_text(render_recovery_bundle_readme(manifest), encoding="utf-8")


def write_recovery_report(project: Path, report: dict[str, Any]) -> Path:
    recovery_root = guardian_dir(project) / "recovery"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = recovery_root / stamp
    suffix = 1
    while bundle.exists():
        bundle = recovery_root / f"{stamp}-{suffix}"
        suffix += 1
    bundle.mkdir(parents=True, exist_ok=False)

    redacted_report = json.loads(json.dumps(report, ensure_ascii=False))
    (bundle / "diagnosis.json").write_text(json.dumps(redacted_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (bundle / "diagnosis.md").write_text(render_markdown(redacted_report), encoding="utf-8")
    (bundle / "events.json").write_text(json.dumps(redacted_report.get("events", [])[:20], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    checkpoint_error = None
    try:
        checkpoint = load_checkpoint(project)
    except FileNotFoundError:
        checkpoint = None
    except (OSError, json.JSONDecodeError):
        checkpoint_error = True
        checkpoint = None
    if checkpoint:
        (bundle / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        resume_prompt = build_resume_prompt(checkpoint)
    elif checkpoint_error or (redacted_report.get("checkpoint_attention") or {}).get("checkpoint_read_error"):
        error_path = (redacted_report.get("checkpoint_attention") or {}).get("checkpoint_read_error_path") or str(guardian_dir(project) / "current.json")
        resume_prompt = (
            f"The current checkpoint could not be read: {error_path}\n\n"
            "Do not treat this as no checkpoint. First inspect `.codex-guardian/current.json`, "
            "verify current file state with the smallest safe command, and use `doctor.md` or "
            "`diagnosis.md` from this bundle before continuing.\n"
        )
    else:
        resume_prompt = (
            "No Codex Guardian checkpoint was found.\n\n"
            "First verify current file state with the smallest safe command. "
            "Do not re-read broad context. Create a preflight checkpoint before continuing.\n"
        )
    (bundle / "resume-prompt.txt").write_text(redact(resume_prompt), encoding="utf-8")
    write_recovery_bundle_index(bundle, redacted_report)
    return bundle


def cmd_diagnose(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    report = build_report(codex_home, args.hours, args.limit)
    write_output(render_report(report, args.format), args.output)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    project = Path(args.project).expanduser().resolve()
    write_recovery = bool(args.recovery_report or args.doctor)
    checkpoint_path = None
    if args.task:
        checkpoint_path = write_preflight_checkpoint(
            project,
            args.task,
            args.touched or [],
            args.slice_minutes,
            "Use watch result and recovery bundle before continuing",
        )
    while True:
        report = build_report(codex_home, args.hours, args.limit)
        report["health"] = health_assessment(report["summary"])
        if args.check_reachability:
            try:
                report["reachability"] = build_reachability_report(args.reachability_endpoint, args.reachability_timeout, args.reachability_dns_only)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        if args.check_service_status:
            report["service_status"] = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
        if checkpoint_path:
            report["preflight_checkpoint"] = str(checkpoint_path)
        _, checkpoint_attention = read_current_checkpoint(project)
        if checkpoint_attention.get("checkpoint_overdue") or checkpoint_attention.get("checkpoint_read_error"):
            report["checkpoint_attention"] = checkpoint_attention
        status_report = build_status_report(codex_home, project, args.hours, args.limit)
        if status_report["status"].get("fresh_recovery_bundle_recommended"):
            report["status"] = status_report["status"]
        actionable = has_actionable_failure(report)
        marker_path = None
        if actionable and args.mark_restart and doctor_restart_recommended(report["health"]):
            marker_path = attach_restart_marker(project, report, "watch")
        if actionable and write_recovery:
            if args.doctor:
                status_report = build_status_report(codex_home, project, args.hours, args.limit)
                report["status"] = status_report["status"]
                if not report.get("reachability"):
                    try:
                        report["reachability"] = build_reachability_report(args.reachability_endpoint, args.reachability_timeout, args.reachability_dns_only)
                    except ValueError as exc:
                        print(str(exc), file=sys.stderr)
                        return 2
                report["service_status"] = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
            bundle = write_recovery_report(project, report)
            report["recovery_report"] = redact(str(bundle))
            if args.doctor:
                report["doctor"] = {
                    "created_recovery_bundle": True,
                    "created_preflight_checkpoint": checkpoint_path is not None,
                    "created_restart_marker": marker_path is not None,
                    "actions": doctor_actions(
                        report["health"],
                        bundle,
                        checkpoint_path,
                        marker_path,
                        checkpoint_attention,
                        status_report["status"],
                        report["reachability"],
                        report.get("service_status"),
                    ),
                }
                write_doctor_files(bundle, report, args.hours, status_report)
            else:
                if report.get("reachability"):
                    write_reachability_files(bundle, report=report["reachability"])
                    write_recovery_bundle_index(bundle, report)
                if report.get("service_status"):
                    write_service_status_files(bundle, report=report["service_status"])
                    write_connection_triage_files(bundle, report, args.hours)
                    write_recovery_bundle_index(bundle, report)
                if report.get("status"):
                    status_report = build_status_report(codex_home, project, args.hours, args.limit)
                    report["status"] = status_report["status"]
                    write_status_files(bundle, status_report)
                    write_connection_triage_files(bundle, report, args.hours)
                    add_status_guidance_to_resume_prompt(bundle, "connection-triage.md")
                    write_recovery_bundle_index(bundle, report)
        write_output(render_report(report, args.format), args.output)
        if actionable:
            return 1
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_health(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    report = build_report(codex_home, args.hours, args.limit)
    report["health"] = health_assessment(report["summary"])
    if args.check_reachability:
        try:
            report["reachability"] = build_reachability_report(args.reachability_endpoint, args.reachability_timeout, args.reachability_dns_only)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.check_service_status:
        report["service_status"] = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
    write_output(render_health_report(report, args.format), args.output)
    reachability_attention = reachability_needs_attention(report.get("reachability"))
    service_status_attention = service_status_needs_attention(report.get("service_status"))
    return 0 if report["health"]["status"] == "ok" and not reachability_attention and not service_status_attention else 1


def cmd_reachability(args: argparse.Namespace) -> int:
    try:
        report = build_reachability_report(args.endpoint, args.timeout, args.dns_only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    write_output(render_reachability_report(report, args.format), args.output)
    return 0 if not report["reachability"]["local_network_issue"] else 1


def cmd_service_status(args: argparse.Namespace) -> int:
    report = build_service_status_report(args.endpoint, args.timeout)
    write_output(render_service_status_report(report, args.format), args.output)
    status = report["service_status"]
    return 0 if not status["upstream_issue"] and not status["check_failed"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    project = Path(args.project).expanduser().resolve()
    report = build_status_report(codex_home, project, args.hours, args.limit)
    write_output(render_status_report(report, args.format), args.output)
    post = report["status"].get("post_restart")
    needs_attention = (
        report["health"]["status"] != "ok"
        or bool(report["status"].get("checkpoint_read_error"))
        or bool(report["status"].get("checkpoint_overdue"))
        or (post and post.get("status") != "clean")
    )
    return 1 if needs_attention else 0


def cmd_connection_triage(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    project = Path(args.project).expanduser().resolve()
    report = build_connection_triage_report(codex_home, project, args.hours, args.limit)
    if args.check_reachability:
        try:
            report["reachability"] = build_reachability_report(args.reachability_endpoint, args.reachability_timeout, args.reachability_dns_only)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.check_service_status:
        report["service_status"] = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
    if args.check_reachability or args.check_service_status:
        attach_connection_triage(report, report["status"]["next_actions"])
    write_output(render_connection_triage_report(report, args.format), args.output)
    post = report["status"].get("post_restart")
    needs_attention = (
        report["health"]["status"] != "ok"
        or bool(report["status"].get("checkpoint_read_error"))
        or bool(report["status"].get("checkpoint_overdue"))
        or (post and post.get("status") != "clean")
        or reachability_needs_attention(report.get("reachability"))
        or service_status_needs_attention(report.get("service_status"))
    )
    return 1 if needs_attention else 0


def cmd_post_restart(args: argparse.Namespace) -> int:
    marker_path = None
    marker_payload = None
    if args.since:
        since = parse_report_time(args.since)
    else:
        project = Path(args.project).expanduser().resolve()
        try:
            marker_payload = load_restart_marker(project)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read restart marker. Use --since or run mark-restart first: {exc}", file=sys.stderr)
            return 2
        marker_path = restart_marker_path(project)
        since = parse_report_time(str(marker_payload.get("created_at", "")))
    if not since:
        print(f"Invalid restart timestamp: {args.since or marker_path}", file=sys.stderr)
        return 2
    since = utc_datetime(since)
    hours = lookback_hours_covering_since(args.hours, since)
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    report = build_report(codex_home, hours, args.limit)
    activity_count = count_log_activity_since(codex_home, hours, since)
    report["post_restart"] = post_restart_assessment(report["events"], since, activity_count)
    if marker_path:
        report["restart_marker"] = restart_marker_summary(marker_path, marker_payload or {})
        report["post_restart"]["marker_path"] = str(marker_path)
    write_output(render_post_restart_report(report, args.format), args.output)
    return 0 if report["post_restart"]["status"] == "clean" else 1


def cmd_bundle(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    project = Path(args.project).expanduser().resolve()
    source_command = getattr(args, "source_command", "bundle")
    report = build_report(codex_home, args.hours, args.limit)
    report["health"] = health_assessment(report["summary"])
    checkpoint_path = None
    if args.task:
        checkpoint_path = write_preflight_checkpoint(
            project,
            args.task,
            args.touched or [],
            args.slice_minutes,
            "Use recovery bundle and resume safely",
        )
        report["preflight_checkpoint"] = str(checkpoint_path)
    _, checkpoint_attention = read_current_checkpoint(project)
    if checkpoint_attention.get("checkpoint_overdue") or checkpoint_attention.get("checkpoint_read_error"):
        report["checkpoint_attention"] = checkpoint_attention
    status_report = build_status_report(codex_home, project, args.hours, args.limit)
    marker_context = status_restart_marker_context(status_report["status"])
    marker_path = None
    if args.mark_restart and (doctor_restart_recommended(report["health"]) or marker_context):
        marker_path = attach_restart_marker(project, report, source_command, marker_context)
    bundle = write_recovery_report(project, report)
    report["recovery_report"] = str(bundle)
    if args.doctor:
        status_report = build_status_report(codex_home, project, args.hours, args.limit)
        report["status"] = status_report["status"]
        report["reachability"] = build_reachability_report(args.reachability_endpoint, args.reachability_timeout, args.reachability_dns_only)
        report["service_status"] = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
        report["doctor"] = {
            "created_recovery_bundle": True,
            "created_preflight_checkpoint": checkpoint_path is not None,
            "created_restart_marker": marker_path is not None,
            "actions": doctor_actions(
                report["health"],
                bundle,
                checkpoint_path,
                marker_path,
                checkpoint_attention,
                status_report["status"],
                report["reachability"],
                report["service_status"],
            ),
        }
        write_doctor_files(bundle, report, args.hours, status_report)
    write_output(render_health_report(report, args.format), args.output)
    return 0


def cmd_recover_now(args: argparse.Namespace) -> int:
    bundle_args = argparse.Namespace(**vars(args))
    bundle_args.doctor = True
    bundle_args.source_command = "recover-now"
    return cmd_bundle(bundle_args)


def cmd_doctor(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    project = Path(args.project).expanduser().resolve()
    report = build_report(codex_home, args.hours, args.limit)
    report["health"] = health_assessment(report["summary"])
    status_report = build_status_report(codex_home, project, args.hours, args.limit)
    reachability_report = None
    if args.check_reachability:
        reachability_report = build_reachability_report(args.reachability_endpoint, args.reachability_timeout, args.reachability_dns_only)
        report["reachability"] = reachability_report
    service_status_report = None
    if args.check_service_status:
        service_status_report = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
        report["service_status"] = service_status_report
    _, checkpoint_attention = read_current_checkpoint(project)
    if checkpoint_attention.get("checkpoint_overdue") or checkpoint_attention.get("checkpoint_read_error"):
        report["checkpoint_attention"] = checkpoint_attention
    checkpoint_path = None
    if args.task:
        checkpoint_path = write_preflight_checkpoint(
            project,
            args.task,
            args.touched or [],
            args.slice_minutes,
            "Use doctor recovery bundle and resume safely",
        )
        report["preflight_checkpoint"] = str(checkpoint_path)
        status_report = build_status_report(codex_home, project, args.hours, args.limit)
    needs_attention = (
        report["health"]["status"] != "ok"
        or bool(checkpoint_attention.get("checkpoint_overdue"))
        or bool(checkpoint_attention.get("checkpoint_read_error"))
        or bool(status_report["status"].get("fresh_recovery_bundle_recommended"))
        or reachability_needs_attention(reachability_report)
        or service_status_needs_attention(service_status_report)
    )
    marker_context = status_restart_marker_context(status_report["status"])
    marker_path = None
    if args.mark_restart and (doctor_restart_recommended(report["health"]) or marker_context):
        marker_path = attach_restart_marker(project, report, "doctor", marker_context)
    bundle_path = None
    if needs_attention:
        bundle_path = write_recovery_report(project, report)
        report["recovery_report"] = str(bundle_path)
    report["doctor"] = {
        "created_recovery_bundle": bundle_path is not None,
        "created_preflight_checkpoint": checkpoint_path is not None,
        "created_restart_marker": marker_path is not None,
        "actions": doctor_actions(
            report["health"],
            bundle_path,
            checkpoint_path,
            marker_path,
            checkpoint_attention,
            status_report["status"],
            reachability_report,
            service_status_report,
        ),
    }
    if bundle_path:
        status_report = build_status_report(codex_home, project, args.hours, args.limit)
        report["status"] = status_report["status"]
        if not service_status_report:
            service_status_report = build_service_status_report(args.service_status_endpoint, args.service_status_timeout)
            report["service_status"] = service_status_report
        write_doctor_files(bundle_path, report, args.hours, status_report)
    write_output(render_doctor_report(report, args.format), args.output)
    return 0 if not needs_attention else 1


def guardian_dir(project: Path) -> Path:
    return project / ".codex-guardian"


def restart_marker_path(project: Path) -> Path:
    return guardian_dir(project) / "restart-marker.json"


def write_restart_marker(project: Path, reason: str, metadata: Optional[dict[str, Any]] = None) -> Path:
    path = restart_marker_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "codex-guardian.restart-marker.v1",
        "created_at": now_utc(),
        "reason": reason,
        "project": str(project),
    }
    if metadata:
        payload.update({key: value for key, value in metadata.items() if value is not None})
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_restart_marker(project: Path) -> dict[str, Any]:
    return json.loads(restart_marker_path(project).read_text(encoding="utf-8"))


def render_restart_marker_markdown(report: dict[str, Any]) -> str:
    marker = report["restart_marker"]
    lines = [
        "# Codex Guardian Restart Marker",
        "",
        f"- Created at: `{marker['created_at']}`",
        f"- Path: `{marker['path']}`",
        f"- Reason: {marker['reason']}",
    ]
    if marker.get("restart_decision"):
        lines.append(f"- Restart decision: `{marker['restart_decision']['decision']}`")
        lines.append(f"- Restart first action: `{marker['restart_decision']['first_action']}`")
    lines += [
        "",
        "Restart Codex now, then run `post-restart --project .` from the same project.",
    ]
    return "\n".join(lines) + "\n"


def render_restart_marker_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_restart_marker_markdown(report)


def cmd_mark_restart(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    restart_decision = restart_decision_record(
        "manual_restart",
        False,
        True,
        "after_state_preserved",
        args.reason,
    )
    path = write_restart_marker(
        project,
        args.reason,
        {
            "source": "mark-restart",
            "issue_type": "manual_restart",
            "restart_timing": "after_state_preserved",
            "restart_reason": args.reason,
            "restart_recommended": True,
            "restart_codex_now": False,
            "restart_decision": restart_decision,
        },
    )
    payload = load_restart_marker(project)
    report = {
        "restart_marker": restart_marker_summary(path, payload)
    }
    write_output(render_restart_marker_report(report, args.format), args.output)
    return 0


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "checkpoint"


def checkpoint_payload(args: argparse.Namespace, status: str | None = None) -> dict[str, Any]:
    payload = {
        "schema": "codex-guardian.checkpoint.v1",
        "created_at": now_utc(),
        "task": args.task,
        "phase": args.phase,
        "status": status or args.status,
        "next_action": args.next_action,
        "touched": args.touched or [],
        "verified": args.verified or [],
        "notes": args.notes or [],
        "cwd": str(Path(args.project).expanduser().resolve()),
    }
    slice_minutes = getattr(args, "slice_minutes", None)
    if slice_minutes:
        payload["slice_minutes"] = slice_minutes
        payload["checkpoint_due_at"] = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=slice_minutes)
        ).isoformat()
    return payload


def path_inside_project(project: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(project)
        return True
    except ValueError:
        return False


def touched_file_facts(project: Path, touched: list[str]) -> tuple[list[str], list[str]]:
    verified: list[str] = []
    notes: list[str] = []
    for raw in touched:
        resolved = (project / raw).resolve()
        if not path_inside_project(project, resolved):
            notes.append(f"touched file outside project: {raw}")
        elif resolved.exists():
            verified.append(f"touched file exists: {raw}")
        else:
            verified.append(f"touched file missing: {raw}")
    return verified, notes


def project_fingerprint(project: Path, touched: list[str]) -> dict[str, Any]:
    fingerprint: dict[str, Any] = {
        "schema": "codex-guardian.fingerprint.v1",
        "git_status": None,
        "git_available": False,
        "touched": [],
    }
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(project),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fingerprint["git_error"] = str(exc)
    else:
        if result.returncode == 0:
            fingerprint["git_available"] = True
            fingerprint["git_status"] = result.stdout
        else:
            fingerprint["git_error"] = (result.stderr or result.stdout).strip()

    for raw in touched:
        resolved = (project / raw).resolve()
        entry: dict[str, Any] = {"path": raw}
        if not path_inside_project(project, resolved):
            entry["state"] = "outside_project"
        elif not resolved.exists():
            entry["state"] = "missing"
        elif resolved.is_file():
            stat = resolved.stat()
            entry.update({"state": "file", "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
        else:
            entry["state"] = "non_file"
        fingerprint["touched"].append(entry)
    stable = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
    fingerprint["digest"] = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return fingerprint


def save_checkpoint(project: Path, payload: dict[str, Any]) -> Path:
    root = guardian_dir(project)
    checkpoint_root = root / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = checkpoint_root / f"{stamp}-{slugify(payload['phase'])}.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    (root / "current.json").write_text(text, encoding="utf-8")
    return path


def cmd_checkpoint(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    payload = checkpoint_payload(args)
    if args.fingerprint:
        payload["fingerprint"] = project_fingerprint(project, args.touched or [])
    if args.compare_fingerprint:
        try:
            previous = load_checkpoint(project)
        except (OSError, json.JSONDecodeError):
            previous = {}
        previous_fingerprint = previous.get("fingerprint")
        current_fingerprint = payload.get("fingerprint")
        if previous_fingerprint and current_fingerprint and previous_fingerprint.get("digest") == current_fingerprint.get("digest"):
            payload["status"] = "no_progress"
            payload["verified"].append("no_progress: fingerprint unchanged")
        elif current_fingerprint:
            payload["verified"].append("progress: fingerprint changed")
    path = save_checkpoint(project, payload)
    print(f"Wrote checkpoint: {path}")
    return 0


def git_status_facts(project: Path) -> tuple[list[str], list[str]]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(project),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"git status: unavailable ({exc})"], []
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f" ({detail[0]})" if detail else ""
        return [f"git status: unavailable{suffix}"], []
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    state = "dirty" if lines else "clean"
    notes = [f"git status entry: {line}" for line in lines[:10]]
    if len(lines) > 10:
        notes.append(f"git status omitted entries: {len(lines) - 10}")
    return [f"git status: {state}"], notes


def write_preflight_checkpoint(
    project: Path,
    task: str,
    touched: list[str],
    slice_minutes: int,
    next_action: str,
) -> Path:
    verified, status_notes = git_status_facts(project)
    file_verified, file_notes = touched_file_facts(project, touched)
    verified.extend(file_verified)
    checkpoint_args = argparse.Namespace(
        project=str(project),
        task=task,
        phase="preflight_done",
        status="in_progress",
        next_action=next_action,
        touched=touched,
        verified=verified,
        notes=status_notes + file_notes,
        slice_minutes=slice_minutes,
    )
    return save_checkpoint(project, checkpoint_payload(checkpoint_args))


def cmd_preflight(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    verified, status_notes = git_status_facts(project)
    file_verified, file_notes = touched_file_facts(project, args.touched or [])
    verified.extend(file_verified)
    verified.extend(args.verified or [])
    notes = status_notes + file_notes + (args.notes or [])
    payload_args = argparse.Namespace(
        project=str(project),
        task=args.task,
        phase="preflight_done",
        status="in_progress",
        next_action=args.next_action,
        touched=args.touched or [],
        verified=verified,
        notes=notes,
        slice_minutes=args.slice_minutes,
    )
    path = save_checkpoint(project, checkpoint_payload(payload_args))
    print(f"Wrote preflight checkpoint: {path}")
    return 0


def build_auto_preflight_report(
    project: Path,
    task: str,
    next_action: str,
    touched: list[str],
    estimated_minutes: int,
    threshold_minutes: int,
    slice_minutes: int,
    force: bool,
) -> dict[str, Any]:
    should_checkpoint = force or estimated_minutes >= threshold_minutes
    reason = "forced" if force else "estimated_minutes_meets_threshold"
    checkpoint_path = None
    if should_checkpoint:
        checkpoint_path = write_preflight_checkpoint(project, task, touched, slice_minutes, next_action)
    else:
        reason = "estimated_minutes_below_threshold"
    return {
        "schema": "codex-guardian.auto-preflight.v1",
        "generated_at": now_utc(),
        "project": str(project),
        "task": task,
        "next_action": next_action,
        "estimated_minutes": estimated_minutes,
        "threshold_minutes": threshold_minutes,
        "slice_minutes": slice_minutes,
        "created_preflight_checkpoint": should_checkpoint,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
        "reason": reason,
    }


def render_auto_preflight_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Codex Guardian Auto-Preflight",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Project: `{report['project']}`",
        f"- Task: {report['task']}",
        f"- Estimated minutes: `{report['estimated_minutes']}`",
        f"- Threshold minutes: `{report['threshold_minutes']}`",
        f"- Created preflight checkpoint: `{str(report['created_preflight_checkpoint']).lower()}`",
        f"- Reason: `{report['reason']}`",
    ]
    if report.get("checkpoint_path"):
        lines.append(f"- Checkpoint: `{report['checkpoint_path']}`")
    else:
        lines.append("- Checkpoint: `none`")
    lines.append(f"- Next action: {report['next_action']}")
    return "\n".join(lines) + "\n"


def render_auto_preflight_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return render_auto_preflight_markdown(report)


def cmd_auto_preflight(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    report = build_auto_preflight_report(
        project=project,
        task=args.task,
        next_action=args.next_action,
        touched=args.touched or [],
        estimated_minutes=args.estimated_minutes,
        threshold_minutes=args.threshold_minutes,
        slice_minutes=args.slice_minutes,
        force=args.force,
    )
    write_output(render_auto_preflight_report(report, args.format), args.output)
    return 0


def skill_source_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def missing_required_skill_files(skill_dir: Path) -> list[str]:
    return [relative for relative in REQUIRED_SKILL_FILES if not (skill_dir / relative).exists()]


def required_skill_files_message(prefix: str, missing: list[str]) -> str:
    return f"{prefix}: {', '.join(missing)}"


def cmd_install_check(args: argparse.Namespace) -> int:
    source = skill_source_dir()
    source_missing = missing_required_skill_files(source)
    if source_missing:
        print(required_skill_files_message(f"Source skill is incomplete at {source}", source_missing), file=sys.stderr)
        return 1
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    target = codex_home / "skills" / "codex-guardian"

    if target.exists():
        if args.install and not args.force:
            print(f"Target skill already exists: {target}. Use --force to overwrite.", file=sys.stderr)
            return 1
        if args.install and args.force:
            shutil.rmtree(target)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            print(f"Reinstalled Codex Guardian at {target}")
            return 0
        target_missing = missing_required_skill_files(target)
        if target_missing:
            print(required_skill_files_message(f"Installed Codex Guardian is incomplete at {target}", target_missing), file=sys.stderr)
            print("Run again with --install --force to replace the incomplete installed copy.", file=sys.stderr)
            return 1
        print(f"Codex Guardian is installed at {target}")
        return 0

    if not args.install:
        print(f"Codex Guardian is not installed at {target}. Run again with --install to copy it.", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    print(f"Installed Codex Guardian at {target}")
    return 0


def parse_simple_frontmatter(skill_md: Path) -> tuple[bool, str]:
    content = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            return False, f"Invalid frontmatter line: {line}"
        key, value = line.split(":", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        data[key.strip()] = value
    allowed = {"name", "description", "license", "allowed-tools", "metadata"}
    unexpected = set(data) - allowed
    if unexpected:
        return False, f"Unexpected frontmatter keys: {', '.join(sorted(unexpected))}"
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    if not name:
        return False, "Missing name"
    if not re.match(r"^[a-z0-9-]+$", name) or name.startswith("-") or name.endswith("-") or "--" in name or len(name) > 64:
        return False, f"Invalid name: {name}"
    if not description:
        return False, "Missing description"
    if "<" in description or ">" in description or len(description) > 1024:
        return False, "Invalid description"
    return True, "Skill is valid"


def cmd_validate_skill(args: argparse.Namespace) -> int:
    skill_dir = Path(args.skill_dir).expanduser().resolve() if args.skill_dir else skill_source_dir()
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(f"SKILL.md not found: {skill_md}", file=sys.stderr)
        return 1
    missing = missing_required_skill_files(skill_dir)
    if missing:
        print(required_skill_files_message(f"Skill is incomplete at {skill_dir}", missing), file=sys.stderr)
        return 1
    valid, message = parse_simple_frontmatter(skill_md)
    if valid:
        print(f"{message}: {skill_dir}")
        return 0
    print(message, file=sys.stderr)
    return 1


def fixture_corpus_path() -> Path:
    return skill_source_dir() / "fixtures" / "redacted-real-log-corpus.json"


def load_fixture_corpus() -> dict[str, Any]:
    return json.loads(fixture_corpus_path().read_text(encoding="utf-8"))


def fixture_case_match_codes(case: dict[str, Any]) -> set[str]:
    haystack = f"{case.get('target', '')}\n{case.get('message', '')}"
    if is_quoted_agent_history(haystack):
        return set()
    return {match["code"] for match in match_events(haystack)}


REQUIRED_FIXTURE_CODES = set(
    TRANSPORT_CODES
    + APP_STATE_CODES
    + COMPACTION_CODES
    + NO_PROGRESS_CODES
    + AUTH_SESSION_CODES
)

REQUIRED_FIXTURE_TARGETS = {
    "desktop-log",
    "codex_api::endpoint::responses_websocket",
    "codex_core::responses_retry",
    "codex_core::session_startup_prewarm",
    "codex_otel.log_only",
}

REQUIRED_FALSE_POSITIVE_MARKERS = {
    "response.output_text.delta",
    "response.create",
    "thread/goal/set",
}


def fixture_corpus_coverage_checks(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    covered_codes = {
        code
        for case in cases
        for code in case.get("expect_matches", [])
    }
    targets = {str(case.get("target", "")) for case in cases}
    false_positive_text = "\n".join(
        str(case.get("message", ""))
        for case in cases
        if not case.get("expect_matches") and case.get("reject_matches")
    )
    missing_codes = sorted(REQUIRED_FIXTURE_CODES - covered_codes)
    missing_targets = sorted(REQUIRED_FIXTURE_TARGETS - targets)
    missing_false_positive_markers = sorted(
        marker for marker in REQUIRED_FALSE_POSITIVE_MARKERS if marker not in false_positive_text
    )
    return [
        {
            "name": "fixture corpus covers classifier families",
            "passed": not missing_codes,
            "missing": missing_codes,
        },
        {
            "name": "fixture corpus keeps real-log-shaped targets",
            "passed": not missing_targets,
            "missing": missing_targets,
        },
        {
            "name": "fixture corpus protects quoted payload false positives",
            "passed": not missing_false_positive_markers,
            "missing": missing_false_positive_markers,
        },
    ]


def fixture_corpus_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        corpus = load_fixture_corpus()
    except (OSError, json.JSONDecodeError) as exc:
        return [{"name": "fixture corpus loaded", "passed": False, "error": str(exc)}]

    cases = corpus.get("cases", [])
    loaded = corpus.get("schema") == "codex-guardian.fixture-corpus.v1" and isinstance(cases, list) and bool(cases)
    checks.append({"name": "fixture corpus loaded", "passed": loaded, "case_count": len(cases) if isinstance(cases, list) else 0})
    if not loaded:
        return checks

    checks.extend(fixture_corpus_coverage_checks(cases))

    for case in cases:
        expected = set(case.get("expect_matches", []))
        rejected = set(case.get("reject_matches", []))
        actual = fixture_case_match_codes(case)
        checks.append({
            "name": f"fixture corpus {case.get('name', 'unnamed')}",
            "passed": expected.issubset(actual) and not rejected.intersection(actual),
            "expected": sorted(expected),
            "rejected": sorted(rejected),
            "actual": sorted(actual),
        })
    return checks


def create_fixture_logs(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    db_path = codex_home / "logs_2.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("create table logs (ts integer, level text, target text, feedback_log_body text)")
    now = int(datetime.now(timezone.utc).timestamp())
    rows = [
        ("ERROR", "soak-fixture", "stream disconnected before completion"),
        ("ERROR", "soak-fixture", "responses_websocket error closed for conversationId=019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba user kent@example.com"),
        ("ERROR", "soak-fixture", "Received turn/completed for unknown conversation conversationId=019ebaef-5249-7222-bf11-5a77e8c990e8"),
        ("ERROR", "soak-fixture", "Error running remote compact task"),
        ("ERROR", "soak-fixture", "turn/start timeout while waiting"),
        ("ERROR", "soak-fixture", "No progress loop: reread the same files repeatedly without edits"),
        (
            "TRACE",
            "codex_api::endpoint::responses_websocket",
            'websocket event: {"type":"response.output_text.done","text":"that affects confidence in the failure count"}',
        ),
    ]
    for offset, (level, target, message) in enumerate(rows):
        con.execute("insert into logs values (?, ?, ?, ?)", (now - offset, level, target, message))
    con.commit()
    con.close()


def self_test_report() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        codex_home = root / "codex-home"
        project = root / "project"
        project.mkdir()
        create_fixture_logs(codex_home)

        report = build_report(codex_home, 24, DEFAULT_LIMIT, include_desktop=False)
        counts = report["summary"]["pattern_counts"]
        expected = [
            "stream_disconnect",
            "responses_websocket_failure",
            "unknown_conversation",
            "remote_compaction_failed",
            "turn_start_timeout",
            "no_progress_loop",
        ]
        for code in expected:
            checks.append({"name": f"detect {code}", "passed": counts.get(code, 0) >= 1})
        checks.append({"name": "ignore normal response payload failure text", "passed": counts.get("responses_websocket_failure") == 1})
        checks.extend(fixture_corpus_checks())
        health = health_assessment(report["summary"])
        checks.append({"name": "classify mixed health", "passed": health["issue_type"] == "mixed"})
        single_app_state_health = health_assessment({"pattern_counts": {"unknown_conversation": 1}})
        single_app_state_actions = status_next_actions(single_app_state_health, {}, 1)
        checks.append({
            "name": "single app-state event does not restart first",
            "passed": (
                single_app_state_health["restart_decision"]["decision"] == "watch_for_repeat_before_restart"
                and single_app_state_health["restart_recommended"] is False
                and any("--no-mark-restart" in action for action in single_app_state_actions)
                and not any("post-restart" in action for action in single_app_state_actions)
            ),
        })
        auto_project = root / "auto-project"
        auto_project.mkdir()
        (auto_project / "target.txt").write_text("ok\n", encoding="utf-8")
        auto_long = build_auto_preflight_report(
            auto_project,
            "Self-test long task",
            "Edit target.txt",
            ["target.txt"],
            estimated_minutes=20,
            threshold_minutes=10,
            slice_minutes=15,
            force=False,
        )
        checks.append({
            "name": "auto-preflight writes checkpoint for long task",
            "passed": (
                auto_long["created_preflight_checkpoint"] is True
                and auto_long["reason"] == "estimated_minutes_meets_threshold"
                and auto_long["checkpoint_path"] is not None
                and Path(auto_long["checkpoint_path"]).exists()
                and (auto_project / ".codex-guardian" / "current.json").exists()
            ),
        })
        auto_short_project = root / "auto-short-project"
        auto_short_project.mkdir()
        auto_short = build_auto_preflight_report(
            auto_short_project,
            "Self-test short task",
            "Answer directly",
            [],
            estimated_minutes=4,
            threshold_minutes=10,
            slice_minutes=15,
            force=False,
        )
        checks.append({
            "name": "auto-preflight skips short task",
            "passed": (
                auto_short["created_preflight_checkpoint"] is False
                and auto_short["reason"] == "estimated_minutes_below_threshold"
                and auto_short["checkpoint_path"] is None
                and not (auto_short_project / ".codex-guardian" / "current.json").exists()
            ),
        })
        reachability_cases = [
            (socket.gaierror("failed to lookup address information"), "dns_resolution_failed"),
            (RuntimeError("TLS handshake failed: invalid peer certificate"), "tls_handshake_failed"),
            (RuntimeError("received fatal alert: BadRecordMac"), "tls_handshake_failed"),
            (ConnectionResetError("connection reset by peer"), "connection_reset"),
            (TimeoutError("operation timed out"), "request_timeout"),
        ]
        checks.append({
            "name": "reachability classifier transport families",
            "passed": all(classify_reachability_error(exc)["code"] == expected for exc, expected in reachability_cases),
        })
        failed_reachability = {
            "reachability": {
                "status": "dns_resolution_failed",
                "local_network_issue": True,
            }
        }
        reachability_actions = doctor_actions(
            health_assessment({"pattern_counts": {}, "severity_counts": {}}),
            root / "reachability-bundle",
            reachability_report=failed_reachability,
        )
        checks.append({
            "name": "doctor reachability attention action",
            "passed": reachability_needs_attention(failed_reachability)
            and any("Reachability check failed" in action for action in reachability_actions),
        })
        health_reachability = build_reachability_report(
            DEFAULT_REACHABILITY_ENDPOINT,
            1.0,
            dns_probe=lambda _host, _port: [{"family": "AF_INET", "address": "127.0.0.1"}],
            http_probe=lambda _endpoint, _timeout: {"status": "reachable", "status_code": 204, "method": "HEAD"},
        )
        health_report = json.loads(json.dumps(report, ensure_ascii=False))
        health_report["health"] = health
        health_report["reachability"] = health_reachability
        health_markdown = render_health_markdown(health_report)
        checks.append({
            "name": "health reachability report",
            "passed": (
                health_reachability["reachability"]["status"] == "reachable"
                and "## Reachability" in health_markdown
                and "Reachability status: `reachable`" in health_markdown
            ),
        })
        health_boundary = health.get("direct_fix_boundary") or {}
        checks.append({
            "name": "health direct-fix boundary",
            "passed": (
                health_boundary.get("direct_fix_available") is False
                and health_boundary.get("direct_fix_ceiling_score") == 3
                and health_boundary.get("recovery_tooling_ceiling_score") == 9
                and "## Direct-Fix Boundary" in health_markdown
            ),
        })
        triage_reachability = json.loads(json.dumps(health_reachability, ensure_ascii=False))
        triage_reachability["reachability"]["status"] = "connection_refused"
        triage_reachability["reachability"]["local_network_issue"] = True
        triage_report = build_connection_triage_report(codex_home, project, 24, DEFAULT_LIMIT)
        triage_report["reachability"] = triage_reachability
        attach_connection_triage(triage_report, triage_report["status"]["next_actions"])
        triage_markdown = render_connection_triage_markdown(triage_report)
        checks.append({
            "name": "connection triage reachability boundary",
            "passed": (
                triage_report["connection_triage"]["recovery_attention"] == "reachability_failed"
                and "## Reachability" in triage_markdown
                and any("reachability" in action.lower() for action in triage_report["connection_triage"]["local_actions"])
            ),
        })
        restart_context = status_restart_marker_context({"post_restart": {"status": "still_unstable"}})
        checks.append({
            "name": "post-restart restart marker context",
            "passed": bool(restart_context)
            and restart_context["issue_type"] == "post_restart_still_unstable"
            and restart_context["restart_recommended"] is True
            and restart_context["restart_codex_now"] is False,
        })
        service_ok = build_service_status_report(
            DEFAULT_SERVICE_STATUS_ENDPOINT,
            1.0,
            status_probe=lambda _endpoint, _timeout: {"status": {"indicator": "none", "description": "All Systems Operational"}},
        )
        service_degraded = build_service_status_report(
            DEFAULT_SERVICE_STATUS_ENDPOINT,
            1.0,
            status_probe=lambda _endpoint, _timeout: {"status": {"indicator": "major", "description": "Major Service Outage"}},
        )
        checks.append({
            "name": "service status parser boundary",
            "passed": (
                service_ok["service_status"]["status"] == "operational"
                and not service_ok["service_status"]["upstream_issue"]
                and service_degraded["service_status"]["status"] == "degraded"
                and service_degraded["service_status"]["upstream_issue"]
            ),
        })

        rendered = render_report(report, "json")
        checks.append({"name": "redact email", "passed": "kent@example.com" not in rendered})
        checks.append({"name": "redact conversation id", "passed": "019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba" not in rendered})

        checkpoint_args = argparse.Namespace(
            project=str(project),
            task="Codex Guardian self-test",
            phase="write_started",
            status="in_progress",
            next_action="Verify recovery bundle",
            touched=["README.md"],
            verified=["fixture checkpoint"],
            notes=[],
        )
        save_checkpoint(project, checkpoint_payload(checkpoint_args))
        bundle = write_recovery_report(project, report)
        expected_files = {
            "diagnosis.json",
            "diagnosis.md",
            "checkpoint.json",
            "resume-prompt.txt",
            "events.json",
            "manifest.json",
            "README.md",
        }
        actual_files = {path.name for path in bundle.iterdir() if path.is_file()}
        checks.append({"name": "write recovery bundle", "passed": expected_files.issubset(actual_files)})

        doctor_project = root / "doctor-project"
        doctor_project.mkdir()
        doctor_checkpoint_args = argparse.Namespace(
            project=str(doctor_project),
            task="Codex Guardian doctor self-test",
            phase="preflight_done",
            status="in_progress",
            next_action="Verify doctor recovery bundle",
            touched=["README.md"],
            verified=["doctor fixture checkpoint"],
            notes=[],
        )
        doctor_checkpoint = save_checkpoint(doctor_project, checkpoint_payload(doctor_checkpoint_args))
        doctor_report = json.loads(json.dumps(report, ensure_ascii=False))
        doctor_report["health"] = health
        doctor_bundle = write_recovery_report(doctor_project, doctor_report)
        doctor_report["recovery_report"] = str(doctor_bundle)
        doctor_report["doctor"] = {
            "created_recovery_bundle": True,
            "created_preflight_checkpoint": True,
            "created_restart_marker": False,
            "actions": doctor_actions(health, doctor_bundle, doctor_checkpoint),
        }
        doctor_report["service_status"] = build_service_status_report(
            DEFAULT_SERVICE_STATUS_ENDPOINT,
            1.0,
            status_probe=lambda _endpoint, _timeout: {"status": {"indicator": "none", "description": "All Systems Operational"}},
        )
        doctor_status_report = build_status_report(codex_home, doctor_project, 24, DEFAULT_LIMIT)
        write_doctor_files(doctor_bundle, doctor_report, 24, doctor_status_report)
        doctor_expected_files = {
            "doctor.json",
            "doctor.md",
            "status.json",
            "status.md",
            "reachability.json",
            "reachability.md",
            "service-status.json",
            "service-status.md",
            "environment.json",
            "environment.md",
            "connection-triage.json",
            "connection-triage.md",
            "checkpoint.json",
            "manifest.json",
            "README.md",
        }
        doctor_actual_files = {path.name for path in doctor_bundle.iterdir() if path.is_file()}
        doctor_manifest = json.loads((doctor_bundle / "manifest.json").read_text(encoding="utf-8"))
        bundled_status = json.loads((doctor_bundle / "status.json").read_text(encoding="utf-8"))
        checks.append({
            "name": "write doctor recovery bundle",
            "passed": (
                doctor_expected_files.issubset(doctor_actual_files)
                and "doctor.md" in doctor_manifest["open_first"]
                and "status.md" in doctor_manifest["open_first"]
                and "reachability.md" in doctor_manifest["open_first"]
                and "service-status.md" in doctor_manifest["open_first"]
                and "environment.md" in doctor_manifest["open_first"]
                and "connection-triage.md" in doctor_manifest["open_first"]
            ),
        })
        bundled_reachability = json.loads((doctor_bundle / "reachability.json").read_text(encoding="utf-8"))
        checks.append({
            "name": "doctor recovery bundle reachability snapshot",
            "passed": (
                "reachability" in bundled_reachability
                and (doctor_bundle / "reachability.md").exists()
                and bool(bundled_reachability["reachability"]["status"])
            ),
        })
        bundled_environment = json.loads((doctor_bundle / "environment.json").read_text(encoding="utf-8"))
        checks.append({
            "name": "doctor recovery bundle environment snapshot",
            "passed": (
                bundled_environment.get("schema") == "codex-guardian.environment.v1"
                and bundled_environment.get("codex_home") == report.get("codex_home")
                and "codex_cli" in bundled_environment
                and bundled_environment.get("log_sources", {}).get("sqlite_log", {}).get("exists") is True
                and (doctor_bundle / "environment.md").exists()
            ),
        })
        bundled_service_status = json.loads((doctor_bundle / "service-status.json").read_text(encoding="utf-8"))
        checks.append({
            "name": "doctor recovery bundle service status snapshot",
            "passed": (
                bundled_service_status.get("schema") == "codex-guardian.service-status.v1"
                and bundled_service_status.get("service_status", {}).get("status") == "operational"
                and (doctor_bundle / "service-status.md").exists()
            ),
        })
        checks.append({
            "name": "write doctor recovery bundle status snapshot",
            "passed": Path(bundled_status["status"]["latest_recovery_bundle"]).resolve() == doctor_bundle.resolve()
            and bool(bundled_status["status"].get("next_actions")),
        })
        doctor_resume_prompt = (doctor_bundle / "resume-prompt.txt").read_text(encoding="utf-8")
        checks.append({
            "name": "doctor recovery bundle resume prompt status guidance",
            "passed": "Open `status.md` first" in doctor_resume_prompt and "Then use `doctor.md`" in doctor_resume_prompt,
        })

        triage_project = root / "triage-project"
        triage_marker_dir = triage_project / ".codex-guardian"
        triage_marker_dir.mkdir(parents=True)
        triage_marker_created = int(datetime.now(timezone.utc).timestamp()) - 7200
        (triage_marker_dir / "restart-marker.json").write_text(
            json.dumps({
                "schema": "codex-guardian.restart-marker.v1",
                "created_at": datetime.fromtimestamp(triage_marker_created, timezone.utc).replace(microsecond=0).isoformat(),
                "reason": "self-test restart fixture",
            }) + "\n",
            encoding="utf-8",
        )
        triage_home = root / "triage-codex-home"
        triage_home.mkdir()
        triage_db = triage_home / "logs_2.sqlite"
        con = sqlite3.connect(triage_db)
        con.execute("create table logs (ts integer, level text, target text, feedback_log_body text)")
        con.execute(
            "insert into logs values (?, ?, ?, ?)",
            (triage_marker_created + 60, "ERROR", "self-test", "Received turn/started for unknown conversation"),
        )
        con.commit()
        con.close()
        triage_report = build_connection_triage_report(triage_home, triage_project, 1, DEFAULT_LIMIT)
        triage_markdown = render_connection_triage_markdown(triage_report)
        checks.append({
            "name": "connection triage recovery attention",
            "passed": (
                triage_report["status"]["post_restart"]["status"] == "still_unstable"
                and triage_report["connection_triage"]["recovery_attention"] == "post_restart_still_unstable"
                and "Recovery attention: `post_restart_still_unstable`" in triage_markdown
            ),
        })
        triage_packet = triage_report["connection_triage"].get("escalation_packet") or {}
        checks.append({
            "name": "connection triage escalation packet",
            "passed": (
                triage_packet.get("local_direct_fix_available") is False
                and triage_packet.get("recovery_attention") == "post_restart_still_unstable"
                and "connection-triage.json" in triage_packet.get("evidence_to_preserve", [])
                and "auth tokens or session files" in triage_packet.get("do_not_share", [])
                and "## Escalation Packet" in triage_markdown
            ),
        })
        triage_boundary = triage_report["connection_triage"].get("direct_fix_boundary") or {}
        checks.append({
            "name": "connection triage direct-fix ceiling",
            "passed": (
                triage_boundary.get("direct_fix_available") is False
                and triage_boundary.get("direct_fix_ceiling_score") == 3
                and triage_boundary.get("recovery_tooling_ceiling_score") == 9
                and triage_boundary.get("highest_local_recovery_command") == "doctor --project . --hours 1"
                and "## Direct-Fix Ceiling" in triage_markdown
            ),
        })

        transport_home = root / "transport-codex-home"
        transport_home.mkdir()
        transport_db = transport_home / "logs_2.sqlite"
        transport_since = datetime.now(timezone.utc) - timedelta(seconds=50)
        con = sqlite3.connect(transport_db)
        con.execute("create table logs (ts integer, level text, target text, feedback_log_body text)")
        transport_rows = [
            (int(datetime.now(timezone.utc).timestamp()) - 10, "ERROR", "self-test", "failed to send websocket request"),
            (int(datetime.now(timezone.utc).timestamp()) - 11, "ERROR", "self-test", "dns error: failed to lookup address information"),
            (int(datetime.now(timezone.utc).timestamp()) - 12, "ERROR", "self-test", "TLS handshake failed: invalid peer certificate"),
            (int(datetime.now(timezone.utc).timestamp()) - 13, "ERROR", "self-test", "connection reset by peer"),
            (
                int(datetime.now(timezone.utc).timestamp()) - 14,
                "ERROR",
                "self-test",
                "error sending request for url https://chatgpt.com/backend-api/codex/responses: operation timed out",
            ),
        ]
        for row in transport_rows:
            con.execute("insert into logs values (?, ?, ?, ?)", row)
        con.commit()
        con.close()
        transport_report = build_report(transport_home, 1, DEFAULT_LIMIT, include_desktop=False)
        transport_post = post_restart_assessment(
            transport_report["events"],
            transport_since,
            count_sqlite_activity_since(transport_home, transport_since),
        )
        transport_post_patterns = set(transport_post["transport_patterns_after_restart"])
        checks.append({
            "name": "post-restart transport unreliable",
            "passed": (
                transport_post["status"] == "transport_unreliable"
                and transport_post["app_state_patterns_after_restart"] == []
                and {
                    "websocket_send_failed",
                    "dns_resolution_failed",
                    "tls_handshake_failed",
                    "connection_reset",
                    "request_timeout",
                }.issubset(transport_post_patterns)
            ),
        })

        source_dir = skill_source_dir()
        source_missing = missing_required_skill_files(source_dir)
        valid, _message = parse_simple_frontmatter(source_dir / "SKILL.md")
        checks.append({
            "name": "validate skill required files and frontmatter",
            "passed": valid and not source_missing,
            "missing": source_missing,
        })

        incomplete_skill = root / "incomplete-skill"
        incomplete_skill.mkdir()
        (incomplete_skill / "SKILL.md").write_text(
            "---\n"
            "name: codex-guardian\n"
            "description: Local recovery workflow for Codex connection failures.\n"
            "---\n",
            encoding="utf-8",
        )
        incomplete_missing = missing_required_skill_files(incomplete_skill)
        checks.append({
            "name": "validate skill rejects missing required files",
            "passed": "scripts/codex_guardian.py" in incomplete_missing,
            "missing": incomplete_missing,
        })

    failed = [check for check in checks if not check["passed"]]
    return {
        "status": "passed" if not failed else "failed",
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "checks": checks,
        "pattern_counts": counts,
    }


def render_self_test_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Codex Guardian Self-Test",
        "",
        f"- Status: `{report['status']}`",
        f"- Checks passed: {report['checks_passed']}",
        f"- Checks failed: {report['checks_failed']}",
        "",
        "## Checks",
        "",
    ]
    for check in report["checks"]:
        marker = "pass" if check["passed"] else "fail"
        lines.append(f"- `{marker}` {check['name']}")
    lines += ["", "## Pattern Counts", ""]
    for key, value in sorted(report["pattern_counts"].items()):
        lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def cmd_self_test(args: argparse.Namespace) -> int:
    report = self_test_report()
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_self_test_markdown(report), end="")
    return 0 if report["status"] == "passed" else 1


def should_package_file(path: Path) -> bool:
    if path.name in PACKAGE_EXCLUDES:
        return False
    if path.suffix == ".pyc":
        return False
    if "__pycache__" in path.parts:
        return False
    return path.is_file()


def package_files(source: Path) -> list[Path]:
    return sorted(path for path in source.rglob("*") if should_package_file(path))


def add_tar_file(archive: tarfile.TarFile, source: Path, arcname: str) -> None:
    info = archive.gettarinfo(str(source), arcname=arcname)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    with source.open("rb") as handle:
        archive.addfile(info, handle)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_package(args: argparse.Namespace) -> int:
    source = skill_source_dir()
    source_missing = missing_required_skill_files(source)
    if source_missing:
        print(required_skill_files_message(f"Source skill is incomplete at {source}", source_missing), file=sys.stderr)
        return 1
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "codex-guardian.tar.gz"
    manifest_path = output_dir / "codex-guardian-package.json"
    files = package_files(source)
    if not files:
        print(f"No packageable files found in {source}", file=sys.stderr)
        return 1

    with tarfile.open(archive_path, "w:gz") as archive:
        for file_path in files:
            rel = file_path.relative_to(source)
            add_tar_file(archive, file_path, f"codex-guardian/{rel.as_posix()}")

    manifest = {
        "skill": "codex-guardian",
        "created_at": now_utc(),
        "archive": archive_path.name,
        "archive_sha256": sha256_file(archive_path),
        "file_count": len(files),
        "required_files": [f"codex-guardian/{relative}" for relative in REQUIRED_SKILL_FILES],
        "missing_required_files": [],
        "files": [f"codex-guardian/{path.relative_to(source).as_posix()}" for path in files],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote package: {archive_path}")
    print(f"Wrote manifest: {manifest_path}")
    return 0


def load_checkpoint(project: Path, checkpoint: Optional[str] = None) -> dict[str, Any]:
    if checkpoint:
        path = Path(checkpoint).expanduser()
    else:
        path = guardian_dir(project) / "current.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_resume_prompt(payload: dict[str, Any], diagnosis: Optional[str] = None) -> str:
    touched = ", ".join(payload.get("touched") or []) or "none recorded"
    verified = "; ".join(payload.get("verified") or []) or "none recorded"
    notes = "; ".join(payload.get("notes") or []) or "none recorded"
    diagnosis_line = diagnosis or "No diagnosis file provided."
    phase = payload.get("phase")
    next_action = payload.get("next_action")
    if payload.get("touched"):
        read_scope = f"Read only these files first: {touched}."
    else:
        read_scope = "Read only the files required for the next action."
    return f"""Use the latest Codex Guardian checkpoint as the source of continuity.

Task: {payload.get("task")}
Phase: {phase}
Status: {payload.get("status")}
Touched files: {touched}
Verified facts: {verified}
Notes: {notes}
Next action: {next_action}
Diagnosis: {diagnosis_line}

Continue from phase {phase}. First verify whether the recorded edits already happened. Before editing, verify the current file state with the smallest safe command. Do not re-run broad preflight unless the checkpoint conflicts with current files. {read_scope} If current files contradict this checkpoint, stop and report the conflict. Otherwise do the next action and finish with a concise status report.
"""


def cmd_resume_prompt(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    payload = load_checkpoint(project, args.checkpoint)
    diagnosis = None
    if args.diagnosis:
        diagnosis = redact(Path(args.diagnosis).expanduser().read_text(encoding="utf-8")[:2000])
    write_output(build_resume_prompt(payload, diagnosis), args.output)
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    if not args.command:
        print("No command provided after --", file=sys.stderr)
        return 2
    project = Path(args.project).expanduser().resolve()
    verified, status_notes = git_status_facts(project)
    file_verified, file_notes = touched_file_facts(project, args.touched or [])
    verified.extend(file_verified)
    verified.append(f"Command: {' '.join(args.command)}")
    start_args = argparse.Namespace(
        project=str(project),
        task=args.task,
        phase="preflight_done",
        status="in_progress",
        next_action="Run guarded command",
        touched=args.touched or [],
        verified=verified,
        notes=status_notes + file_notes,
        slice_minutes=args.slice_minutes,
    )
    start_path = save_checkpoint(project, checkpoint_payload(start_args))
    print(f"Guardian start checkpoint: {start_path}")

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    result = subprocess.run(command, cwd=str(project), text=True)

    finish_args = argparse.Namespace(
        project=str(project),
        task=args.task,
        phase="completed" if result.returncode == 0 else "blocked",
        status="completed" if result.returncode == 0 else "failed",
        next_action="Review command output" if result.returncode == 0 else "Use resume-prompt before retrying",
        touched=args.touched or [],
        verified=[f"Command exit code: {result.returncode}"],
        notes=[],
    )
    finish_path = save_checkpoint(project, checkpoint_payload(finish_args))
    print(f"Guardian finish checkpoint: {finish_path}")
    if result.returncode != 0:
        print()
        print(build_resume_prompt(load_checkpoint(project)))
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Guardian recovery CLI")
    sub = parser.add_subparsers(dest="command_name", required=True)

    diagnose = sub.add_parser("diagnose", help="Summarize Codex stream and reconnect failures")
    diagnose.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    diagnose.add_argument("--hours", type=int, default=12, help="Lookback window in hours")
    diagnose.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    diagnose.add_argument("--format", choices=["markdown", "json"], default="markdown")
    diagnose.add_argument("--output", default=None, help="Write report to this file")
    diagnose.set_defaults(func=cmd_diagnose)

    watch = sub.add_parser("watch", help="Watch for Codex stream failures and exit when actionable patterns appear")
    watch.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    watch.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    watch.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    watch.add_argument("--interval", type=float, default=30.0, help="Seconds between checks")
    watch.add_argument("--once", action="store_true", help="Run one check and exit")
    watch.add_argument("--format", choices=["markdown", "json"], default="markdown")
    watch.add_argument("--output", default=None, help="Write report to this file")
    watch.add_argument("--project", default=".", help="Project directory for optional recovery reports")
    watch.add_argument("--recovery-report", action="store_true", help="Write a recovery bundle when actionable failures appear")
    watch.add_argument("--doctor", action="store_true", help="Write a doctor-grade recovery bundle when actionable failures appear")
    watch.add_argument("--mark-restart", action="store_true", help="Write a restart marker when watch recommends restarting Codex")
    watch.add_argument("--task", default=None, help="Optional active task name for automatic preflight checkpointing")
    watch.add_argument("--touched", action="append", help="Touched or relevant file path for optional preflight checkpointing")
    watch.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint when --task is used")
    watch.add_argument("--check-reachability", action="store_true", help="Probe Codex endpoint reachability and treat local network failure as actionable")
    watch.add_argument(
        "--reachability-endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe when --check-reachability is set, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    watch.add_argument("--reachability-timeout", type=float, default=5.0, help="Reachability probe timeout in seconds")
    watch.add_argument("--reachability-dns-only", action="store_true", help="When checking reachability, skip HTTP/TLS and only probe DNS")
    watch.add_argument("--check-service-status", action="store_true", help="Probe upstream service status and treat degraded upstream status as actionable")
    watch.add_argument(
        "--service-status-endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to probe when --check-service-status is set or record when --doctor creates a bundle, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    watch.add_argument("--service-status-timeout", type=float, default=5.0, help="Service status probe timeout in seconds")
    watch.set_defaults(func=cmd_watch)

    health = sub.add_parser("health", help="Classify Codex connection health by issue type")
    health.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    health.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    health.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    health.add_argument("--format", choices=["markdown", "json"], default="markdown")
    health.add_argument("--output", default=None, help="Write health report to this file")
    health.add_argument("--check-reachability", action="store_true", help="Probe Codex endpoint reachability alongside log-derived health")
    health.add_argument(
        "--reachability-endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe when --check-reachability is set, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    health.add_argument("--reachability-timeout", type=float, default=5.0, help="Reachability probe timeout in seconds")
    health.add_argument("--reachability-dns-only", action="store_true", help="When checking reachability, skip HTTP/TLS and only probe DNS")
    health.add_argument("--check-service-status", action="store_true", help="Probe upstream service status alongside log-derived health")
    health.add_argument(
        "--service-status-endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to probe when --check-service-status is set, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    health.add_argument("--service-status-timeout", type=float, default=5.0, help="Service status probe timeout in seconds")
    health.set_defaults(func=cmd_health)

    reachability = sub.add_parser("reachability", help="Check DNS and HTTP/TLS reachability for the Codex endpoint")
    reachability.add_argument(
        "--endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    reachability.add_argument("--timeout", type=float, default=5.0, help="Network timeout in seconds")
    reachability.add_argument("--dns-only", action="store_true", help="Check DNS only and skip the HTTP/TLS probe")
    reachability.add_argument("--format", choices=["markdown", "json"], default="markdown")
    reachability.add_argument("--output", default=None, help="Write reachability report to this file")
    reachability.set_defaults(func=cmd_reachability)

    service_status = sub.add_parser("service-status", help="Check the configured upstream service status endpoint")
    service_status.add_argument(
        "--endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to probe, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    service_status.add_argument("--timeout", type=float, default=5.0, help="Network timeout in seconds")
    service_status.add_argument("--format", choices=["markdown", "json"], default="markdown")
    service_status.add_argument("--output", default=None, help="Write service status report to this file")
    service_status.set_defaults(func=cmd_service_status)

    status = sub.add_parser("status", help="Summarize current Guardian health and recovery state")
    status.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    status.add_argument("--project", default=".", help="Project directory for checkpoint, bundle, and marker lookup")
    status.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    status.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    status.add_argument("--format", choices=["markdown", "json"], default="markdown")
    status.add_argument("--output", default=None, help="Write status report to this file")
    status.set_defaults(func=cmd_status)

    connection_triage = sub.add_parser("connection-triage", help="Explain local connection recovery actions and direct-fix boundaries")
    connection_triage.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    connection_triage.add_argument("--project", default=".", help="Project directory for checkpoint, bundle, and marker lookup")
    connection_triage.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    connection_triage.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    connection_triage.add_argument("--format", choices=["markdown", "json"], default="markdown")
    connection_triage.add_argument("--output", default=None, help="Write connection triage report to this file")
    connection_triage.add_argument("--check-reachability", action="store_true", help="Probe Codex endpoint reachability alongside the local fix boundary")
    connection_triage.add_argument(
        "--reachability-endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe when --check-reachability is set, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    connection_triage.add_argument("--reachability-timeout", type=float, default=5.0, help="Reachability probe timeout in seconds")
    connection_triage.add_argument("--reachability-dns-only", action="store_true", help="When checking reachability, skip HTTP/TLS and only probe DNS")
    connection_triage.add_argument("--check-service-status", action="store_true", help="Probe upstream service status alongside the local fix boundary")
    connection_triage.add_argument(
        "--service-status-endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to probe when --check-service-status is set, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    connection_triage.add_argument("--service-status-timeout", type=float, default=5.0, help="Service status probe timeout in seconds")
    connection_triage.set_defaults(func=cmd_connection_triage)

    mark_restart = sub.add_parser("mark-restart", help="Write a restart marker before restarting Codex")
    mark_restart.add_argument("--project", default=".", help="Project directory for the restart marker")
    mark_restart.add_argument("--reason", default="Restart Codex after Guardian recommendation", help="Reason stored in the marker")
    mark_restart.add_argument("--format", choices=["markdown", "json"], default="markdown")
    mark_restart.add_argument("--output", default=None, help="Write marker report to this file")
    mark_restart.set_defaults(func=cmd_mark_restart)

    post_restart = sub.add_parser("post-restart", help="Check whether app-state errors continued after a restart marker")
    post_restart.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    post_restart.add_argument("--project", default=".", help="Project directory for marker lookup when --since is omitted")
    post_restart.add_argument("--since", default=None, help="Restart marker timestamp, for example 2026-06-12T09:30:00+00:00")
    post_restart.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    post_restart.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    post_restart.add_argument("--format", choices=["markdown", "json"], default="markdown")
    post_restart.add_argument("--output", default=None, help="Write post-restart report to this file")
    post_restart.set_defaults(func=cmd_post_restart)

    bundle = sub.add_parser("bundle", help="Write a recovery bundle on demand")
    bundle.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    bundle.add_argument("--project", default=".", help="Project directory for the recovery bundle")
    bundle.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    bundle.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    bundle.add_argument("--format", choices=["markdown", "json"], default="markdown")
    bundle.add_argument("--output", default=None, help="Write bundle report to this file")
    bundle.add_argument("--task", default=None, help="Optional active task name for automatic preflight checkpointing")
    bundle.add_argument("--touched", action="append", help="Touched or relevant file path for optional preflight checkpointing")
    bundle.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint when --task is used")
    bundle.add_argument("--doctor", action="store_true", help="Also write doctor.json and doctor.md action plan files into the bundle")
    bundle.add_argument("--mark-restart", action="store_true", help="Write a restart marker when bundle health recommends restarting Codex")
    bundle.add_argument(
        "--reachability-endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe when --doctor is set, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    bundle.add_argument("--reachability-timeout", type=float, default=5.0, help="Reachability probe timeout in seconds")
    bundle.add_argument("--reachability-dns-only", action="store_true", help="When writing doctor files, skip HTTP/TLS and only probe DNS")
    bundle.add_argument(
        "--service-status-endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to record when --doctor is set, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    bundle.add_argument("--service-status-timeout", type=float, default=5.0, help="Service status probe timeout in seconds")
    bundle.set_defaults(func=cmd_bundle)

    recover_now = sub.add_parser("recover-now", help="Write a full doctor-grade recovery bundle now")
    recover_now.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    recover_now.add_argument("--project", default=".", help="Project directory for the recovery bundle")
    recover_now.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    recover_now.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    recover_now.add_argument("--format", choices=["markdown", "json"], default="markdown")
    recover_now.add_argument("--output", default=None, help="Write recovery report to this file")
    recover_now.add_argument("--task", default=None, help="Optional active task name for automatic preflight checkpointing")
    recover_now.add_argument("--touched", action="append", help="Touched or relevant file path for optional preflight checkpointing")
    recover_now.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint when --task is used")
    recover_now.add_argument("--no-mark-restart", dest="mark_restart", action="store_false", help="Do not write a restart marker")
    recover_now.add_argument(
        "--reachability-endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    recover_now.add_argument("--reachability-timeout", type=float, default=5.0, help="Reachability probe timeout in seconds")
    recover_now.add_argument("--reachability-dns-only", action="store_true", help="Skip HTTP/TLS and only probe DNS")
    recover_now.add_argument(
        "--service-status-endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to record, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    recover_now.add_argument("--service-status-timeout", type=float, default=5.0, help="Service status probe timeout in seconds")
    recover_now.set_defaults(func=cmd_recover_now, mark_restart=True)

    doctor = sub.add_parser("doctor", help="Classify health and write a recovery plan")
    doctor.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    doctor.add_argument("--project", default=".", help="Project directory for optional recovery bundle")
    doctor.add_argument("--hours", type=int, default=1, help="Lookback window in hours")
    doctor.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum matched events")
    doctor.add_argument("--format", choices=["markdown", "json"], default="markdown")
    doctor.add_argument("--output", default=None, help="Write doctor report to this file")
    doctor.add_argument("--task", default=None, help="Optional active task name for automatic preflight checkpointing")
    doctor.add_argument("--touched", action="append", help="Touched or relevant file path for optional preflight checkpointing")
    doctor.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint when --task is used")
    doctor.add_argument("--mark-restart", action="store_true", help="Write a restart marker when doctor recommends restarting Codex")
    doctor.add_argument("--check-reachability", action="store_true", help="Probe Codex endpoint reachability and treat local network failure as attention")
    doctor.add_argument(
        "--reachability-endpoint",
        default=DEFAULT_REACHABILITY_ENDPOINT,
        help=f"HTTP(S) endpoint to probe when --check-reachability is set, default: {DEFAULT_REACHABILITY_ENDPOINT}",
    )
    doctor.add_argument("--reachability-timeout", type=float, default=5.0, help="Reachability probe timeout in seconds")
    doctor.add_argument("--reachability-dns-only", action="store_true", help="When checking reachability, skip HTTP/TLS and only probe DNS")
    doctor.add_argument("--check-service-status", action="store_true", help="Probe upstream service status and treat degraded upstream status as attention")
    doctor.add_argument(
        "--service-status-endpoint",
        default=DEFAULT_SERVICE_STATUS_ENDPOINT,
        help=f"Statuspage JSON endpoint to probe when --check-service-status is set or record when a recovery bundle is created, default: {DEFAULT_SERVICE_STATUS_ENDPOINT}",
    )
    doctor.add_argument("--service-status-timeout", type=float, default=5.0, help="Service status probe timeout in seconds")
    doctor.set_defaults(func=cmd_doctor)

    install = sub.add_parser("install-check", help="Check or install this skill into a Codex home")
    install.add_argument("--codex-home", default=None, help="Codex home directory, default: CODEX_HOME or ~/.codex")
    install.add_argument("--install", action="store_true", help="Copy this skill into the Codex skills directory when missing")
    install.add_argument("--force", action="store_true", help="Overwrite an existing target skill")
    install.set_defaults(func=cmd_install_check)

    validate = sub.add_parser("validate-skill", help="Validate required skill files and frontmatter without external Python packages")
    validate.add_argument("--skill-dir", default=None, help="Skill directory, default: this codex-guardian skill")
    validate.set_defaults(func=cmd_validate_skill)

    self_test = sub.add_parser("self-test", help="Run a local fixture soak test without reading private logs")
    self_test.add_argument("--format", choices=["markdown", "json"], default="markdown")
    self_test.set_defaults(func=cmd_self_test)

    package = sub.add_parser("package", help="Create a clean distributable skill archive and manifest")
    package.add_argument("--output-dir", default="dist", help="Directory for codex-guardian.tar.gz and manifest")
    package.set_defaults(func=cmd_package)

    preflight = sub.add_parser("preflight", help="Write a preflight_done checkpoint with git state")
    preflight.add_argument("--project", default=".", help="Project directory")
    preflight.add_argument("--task", required=True, help="Task name")
    preflight.add_argument("--next-action", required=True, help="Next intended action")
    preflight.add_argument("--touched", action="append", help="Touched or relevant file path")
    preflight.add_argument("--verified", action="append", help="Additional verified fact")
    preflight.add_argument("--notes", action="append", help="Additional note")
    preflight.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint")
    preflight.set_defaults(func=cmd_preflight)

    auto_preflight = sub.add_parser("auto-preflight", help="Write preflight only when a task estimate crosses the long-task threshold")
    auto_preflight.add_argument("--project", default=".", help="Project directory")
    auto_preflight.add_argument("--task", required=True, help="Task name")
    auto_preflight.add_argument("--next-action", required=True, help="Next intended action")
    auto_preflight.add_argument("--estimated-minutes", type=int, required=True, help="Estimated task duration in minutes")
    auto_preflight.add_argument("--threshold-minutes", type=int, default=10, help="Minimum estimate that should trigger preflight")
    auto_preflight.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint")
    auto_preflight.add_argument("--touched", action="append", help="Touched or relevant file path")
    auto_preflight.add_argument("--force", action="store_true", help="Write preflight even below the estimate threshold")
    auto_preflight.add_argument("--format", choices=["markdown", "json"], default="markdown")
    auto_preflight.add_argument("--output", default=None, help="Write auto-preflight report to this file")
    auto_preflight.set_defaults(func=cmd_auto_preflight)

    checkpoint = sub.add_parser("checkpoint", help="Write a durable project checkpoint")
    add_checkpoint_args(checkpoint)
    checkpoint.set_defaults(func=cmd_checkpoint)

    resume = sub.add_parser("resume-prompt", help="Generate a safe resume prompt from the latest checkpoint")
    resume.add_argument("--project", default=".", help="Project directory")
    resume.add_argument("--checkpoint", default=None, help="Specific checkpoint JSON")
    resume.add_argument("--diagnosis", default=None, help="Optional diagnosis report to quote briefly")
    resume.add_argument("--output", default=None, help="Write prompt to this file")
    resume.set_defaults(func=cmd_resume_prompt)

    wrap = sub.add_parser("wrap", help="Run a non-interactive command with Guardian checkpoints")
    wrap.add_argument("--project", default=".", help="Project directory")
    wrap.add_argument("--task", required=True, help="Task being guarded")
    wrap.add_argument("--phase", default="preflight_done", help=argparse.SUPPRESS)
    wrap.add_argument("--touched", action="append", help="Touched or relevant file path")
    wrap.add_argument("--slice-minutes", type=int, default=15, help="Minutes until next explicit checkpoint")
    wrap.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    wrap.set_defaults(func=cmd_wrap)
    return parser


def add_checkpoint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=".", help="Project directory")
    parser.add_argument("--task", required=True, help="Task name")
    parser.add_argument("--phase", required=True, help="Durable phase name")
    parser.add_argument("--status", default="in_progress", help="Status label")
    parser.add_argument("--next-action", required=True, help="Next intended action")
    parser.add_argument("--touched", action="append", help="Touched or relevant file path")
    parser.add_argument("--verified", action="append", help="Verified fact")
    parser.add_argument("--notes", action="append", help="Additional note")
    parser.add_argument("--fingerprint", action="store_true", help="Record a lightweight project fingerprint")
    parser.add_argument("--compare-fingerprint", action="store_true", help="Compare with the previous checkpoint fingerprint and mark no_progress when unchanged")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
