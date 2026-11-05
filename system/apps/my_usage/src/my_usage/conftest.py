from pathlib import Path

import pytest

from my_usage.testing import gateway_request_count
from my_usage.testing import git_commit
from my_usage.testing import header
from my_usage.testing import index_run
from my_usage.testing import message
from my_usage.testing import step
from my_usage.testing import window_opened
from my_usage.testing import workspace_state
from my_usage.testing import write_chat_name
from my_usage.testing import write_run


@pytest.fixture
def copy_dir(tmp_path: Path) -> Path:
    return tmp_path / "local-analytics-copy"


@pytest.fixture
def host_dir(tmp_path: Path) -> Path:
    path = tmp_path / "mngr-host"
    path.mkdir()
    return path


@pytest.fixture
def standard_copy(copy_dir: Path, host_dir: Path) -> Path:
    """Two finished collections that overlap, plus an interrupted and a failed one that must be ignored.

    Expected, counted in UTC up to 2026-10-04: messages 1, 0, 1, 0; agent steps by model
    claude-opus-5-5 1, 0, 0, 0 / claude-sonnet-5 0, 1, 0, 0 / unknown 0, 0, 1, 0; 20 records.
    """
    write_run(
        copy_dir,
        "run-a",
        [
            header("chat-c"),
            step("s1", "chat-a", "2026-10-01T10:00:00Z", "claude-opus-5-5"),
            step("s2", "chat-a", "2026-10-02T10:00:00.123456789Z", "claude-sonnet-5"),
            step("s3", "chat-a", "2026-10-02T10:01:00Z", model_name=None, source="user"),
            message("m1", "chat-a", "2026-10-01T09:00:00Z"),
            message("m2", "chat-b", "2026-10-03T09:00:00Z"),
            "{this line is not json",
            workspace_state("ws-a", "2026-10-01T00:00:00+00:00", ["chat", "my-usage"]),
            git_commit("g1", "2026-09-16T14:08:08-07:00"),
        ],
        finished_at="2026-10-02T00:00:00Z",
    )
    write_run(
        copy_dir,
        "run-b",
        [
            # Already collected by run-a: counted once.
            step("s2", "chat-a", "2026-10-02T10:00:00.123456789Z", "claude-sonnet-5"),
            message("m1", "chat-a", "2026-10-01T09:00:00Z"),
            step("s4", "chat-c", "2026-10-03T12:00:00Z", model_name=None),
            window_opened("w1", "files", "2026-10-03T08:00:00Z"),
            window_opened("w2", "files", "2026-10-03T08:05:00Z"),
            window_opened("w3", "chat", "2026-10-03T08:10:00Z"),
            gateway_request_count("g-files-owner", "files", "owner", "allowed", 3),
            gateway_request_count("g-files-user", "files", "user", "allowed", 2),
            gateway_request_count("g-files-agent", "files", "agent", "allowed", 1),
            gateway_request_count("g-files-denied", "files", "owner", "denied_not_shared", 4),
            gateway_request_count("g-chat-owner", "chat", "owner", "allowed", 5),
            workspace_state("ws-b", "2026-10-03T00:00:00Z", ["chat", "files", "my-usage"]),
        ],
        finished_at="2026-10-04T12:00:00Z",
        error_by_source={"servers": "timed out"},
    )
    # A run killed part-way, and one that failed: neither is a finished collection.
    write_run(copy_dir, "run-c", [message("m9", "chat-a", "2026-10-01T11:00:00Z")], suffix=".jsonl.part", is_indexed=False)
    write_run(copy_dir, "run-d", [message("m10", "chat-a", "2026-10-01T12:00:00Z")], suffix=".failed", is_indexed=False)
    index_run(copy_dir, "run-d", "2026-10-03T00:00:00Z", exit_code=1)

    write_chat_name(host_dir, "chat-a", "Planning chat")
    (host_dir / "preserved" / "Old chat--chat-b").mkdir(parents=True)
    return copy_dir
