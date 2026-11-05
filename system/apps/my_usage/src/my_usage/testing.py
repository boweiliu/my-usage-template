"""Small synthetic analytics copies for the tests, laid out the way the local-analytics-copy skill
writes them: ``runs/<run id>.jsonl`` (one ``{"source", "record"}`` line per record, then a
``run_summary`` line) and the ``runs.jsonl`` index."""

import json
from pathlib import Path

from pydantic import JsonValue


def step(
    event_id: str,
    agent_id: str,
    timestamp: str,
    model_name: str | None = "claude-opus-5-5",
    source: str = "agent",
) -> tuple[str, dict[str, JsonValue]]:
    """A transcript step, shaped like the collector's (``agent`` steps carry the model)."""
    record: dict[str, JsonValue] = {
        "agent_id": agent_id,
        "emitter": "claude/common_transcript",
        "event_id": event_id,
        "message": f"step {event_id}",
        "source": source,
        "timestamp": timestamp,
        "type": "step",
    }
    if model_name is not None:
        record["model_name"] = model_name
    return "transcripts", record


def header(agent_id: str) -> tuple[str, dict[str, JsonValue]]:
    """A transcript header: names the harness, counts as nothing."""
    return "transcripts", {
        "agent_id": agent_id,
        "emitter": "codex/common_transcript",
        "event_id": f"header-{agent_id}",
        "schema_version": "ATIF-v1.7",
        "type": "header",
    }


def message(event_id: str, key: str, timestamp: str) -> tuple[str, dict[str, JsonValue]]:
    """A message sent from one of the workspace's pages (``key`` is the chat)."""
    return "client_activity", {
        "app": "chat",
        "event_id": event_id,
        "key": key,
        "message_text_length": 12,
        "source": "client_activity",
        "timestamp": timestamp,
        "type": "message",
    }


def window_opened(event_id: str, app: str, timestamp: str) -> tuple[str, dict[str, JsonValue]]:
    """A window of ``app`` opened on the desktop."""
    return "client_activity", {
        "app": app,
        "event_id": event_id,
        "source": "client_activity",
        "timestamp": timestamp,
        "type": "window_opened",
    }


def gateway_request_count(
    event_id: str, service: str, requester_kind: str, outcome: str, request_count: int
) -> tuple[str, dict[str, JsonValue]]:
    """One minute's request count at the sharing gateway, shaped like the template's."""
    return "gateway_requests", {
        "event_id": event_id,
        "outcome": outcome,
        "request_count": request_count,
        "requester_kind": requester_kind,
        "service": service,
        "source": "gateway_requests",
        "timestamp": "2026-10-01T12:00:00Z",
        "type": "gateway_request_count",
    }


def workspace_state(event_id: str, timestamp: str, apps: list[str]) -> tuple[str, dict[str, JsonValue]]:
    """A workspace snapshot naming the installed apps."""
    return "workspace_state", {
        "event_id": event_id,
        "installed_app_names": list(apps),
        "source": "workspace_state",
        "timestamp": timestamp,
        "type": "workspace_state",
    }


def git_commit(event_id: str, timestamp: str) -> tuple[str, dict[str, JsonValue]]:
    """A commit record: collected, but not counted on the page."""
    return "git_numstat", {
        "deletions": 2,
        "event_id": event_id,
        "insertions": 5,
        "source": "git_numstat",
        "timestamp": timestamp,
        "type": "git_commit",
    }


def write_run(
    copy_dir: Path,
    run_id: str,
    lines: list[tuple[str, dict[str, JsonValue]] | str],
    finished_at: str = "2026-10-04T12:00:00Z",
    error_by_source: dict[str, str] | None = None,
    suffix: str = ".jsonl",
    is_indexed: bool = True,
) -> Path:
    """Write one collection (records, then its summary) and index it; a ``str`` line is written as is."""
    runs_dir = copy_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run_id}{suffix}"
    with path.open("w") as handle:
        for line in lines:
            if isinstance(line, str):
                handle.write(line + "\n")
            else:
                source, record = line
                handle.write(json.dumps({"source": source, "record": record}) + "\n")
        handle.write(json.dumps({"source": "run_summary", "record_count": len(lines)}) + "\n")
    if is_indexed:
        index_run(copy_dir, run_id, finished_at, exit_code=0, error_by_source=error_by_source)
    return path


def index_run(
    copy_dir: Path,
    run_id: str,
    finished_at: str,
    exit_code: int | None,
    error_by_source: dict[str, str] | None = None,
) -> None:
    """Append one run to ``runs.jsonl`` the way the copy tool does: a run that finished with its
    summary names its payload file; any other (a collector that exited non-zero, one that could not
    start, so ``exit_code`` None) names its ``.failed`` file and the error instead."""
    entry: dict[str, JsonValue] = {
        "run_id": run_id,
        "started_at": finished_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "collector_commit": "0" * 40,
    }
    if exit_code == 0:
        entry.update(
            payload_file=f"runs/{run_id}.jsonl",
            record_count_by_source={},
            error_by_source=dict(error_by_source or {}),
        )
    else:
        entry.update(
            payload_file=None,
            failed_file=f"runs/{run_id}.failed",
            error=f"the collector exited with {exit_code}",
        )
    _append_index_line(copy_dir, entry)


def index_interrupted_run(copy_dir: Path, run_id: str, recovered_at: str) -> None:
    """Append the line the copy tool writes when it finds a run killed part-way (its ``.part`` kept as ``.failed``)."""
    _append_index_line(
        copy_dir,
        {
            "run_id": run_id,
            "started_at": None,
            "finished_at": None,
            "recovered_at": recovered_at,
            "exit_code": None,
            "payload_file": None,
            "failed_file": f"runs/{run_id}.failed",
            "error": "the run was interrupted before it finished",
        },
    )


def _append_index_line(copy_dir: Path, entry: dict[str, JsonValue]) -> None:
    copy_dir.mkdir(parents=True, exist_ok=True)
    with (copy_dir / "runs.jsonl").open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def write_chat_name(host_dir: Path, agent_id: str, name: str) -> None:
    """Name a chat the way mngr does, in ``agents/<id>/data.json``."""
    agent_dir = host_dir / "agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(json.dumps({"id": agent_id, "name": name}))
