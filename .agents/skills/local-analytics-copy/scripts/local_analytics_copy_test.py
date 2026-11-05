"""Tests for local_analytics_copy.

The collector is a stand-in script that takes the real one's arguments, writes the same audit files
and prints a fixture stream; the GitHub fetch is an injected function. Nothing here reaches GitHub,
downloads a dependency or runs a secret scanner.
"""

import fcntl
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import local_analytics_copy as lac
import pytest
from local_analytics_copy import Layout

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
HOURLY_PAYLOAD = FIXTURES / "payload_hourly.jsonl"
TRANSCRIPTS_FAILED_PAYLOAD = FIXTURES / "payload_transcripts_failed.jsonl"
LISTING = FIXTURES / "collector_listing.json"
PYTHON_LAUNCHER = (sys.executable,)
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40

FAKE_COLLECTOR = """\
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
for option in ("--run-id", "--script-version", "--workspace-root", "--host-dir", "--cursors-file"):
    parser.add_argument(option, required=True)
parser.add_argument("--budget-bytes", required=True, type=int)
args = parser.parse_args()
host_agents = Path(args.host_dir) / "agents"
seen = {
    **vars(args),
    "cursors": json.loads(Path(args.cursors_file).read_text()),
    "agents": sorted(path.name for path in host_agents.iterdir()),
}
Path(os.environ["FAKE_COLLECTOR_ARGS_OUT"]).write_text(json.dumps(seen))
analytics = Path(args.workspace_root) / "data" / ".imbue" / "analytics"
(analytics / "README.md").write_text("what the collector reads\\n")
with (analytics / "collections.jsonl").open("a") as audit:
    audit.write(json.dumps({"run_id": args.run_id}) + "\\n")
sys.stderr.write("fake collector diagnostics\\n")
sys.stdout.buffer.write(Path(os.environ["FAKE_COLLECTOR_STDOUT"]).read_bytes())
sys.exit(int(os.environ.get("FAKE_COLLECTOR_EXIT", "0")))
"""


def _summary_of(payload: Path) -> dict[str, object]:
    return json.loads(payload.read_bytes().splitlines()[-1])


def _all_files(root: Path, excluded: Path | None = None) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not (excluded is not None and path.is_relative_to(excluded))
    }


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    repo_root = tmp_path / "workspace"
    (repo_root / "data" / ".imbue").mkdir(parents=True)
    (repo_root / "data" / ".imbue" / "keep.txt").write_text("not the skill's")
    (repo_root / "README.md").write_text("workspace readme")
    activity = (
        repo_root
        / "data"
        / ".state"
        / "system_interface"
        / "events"
        / "client_activity"
    )
    activity.mkdir(parents=True)
    (activity / "events.jsonl").write_text('{"type": "window_opened"}\n')

    host_dir = tmp_path / "host"
    shell_agent = host_dir / "agents" / f"agent-{uuid4().hex}"
    (shell_agent / "events" / "services").mkdir(parents=True)
    (shell_agent / "data.json").write_text(json.dumps({"name": lac.SHELL_AGENT_NAME}))
    (shell_agent / "events" / "services" / "events.jsonl").write_text(
        '{"service": "chat"}\n'
    )
    chat_agent = host_dir / "agents" / f"agent-{uuid4().hex}"
    (chat_agent / "events").mkdir(parents=True)
    (chat_agent / "data.json").write_text(json.dumps({"name": "some-chat"}))
    (host_dir / "agents" / "stray-file").write_text("not an agent dir")
    return lac.Layout(repo_root=repo_root, host_dir=host_dir)


@pytest.fixture
def collector(layout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the stand-in collector; returns the file it records the arguments it was given in."""
    layout.analytics_dir.mkdir(parents=True)
    (layout.analytics_dir / lac.ENTRYPOINT_NAME).write_text(FAKE_COLLECTOR)
    layout.source_path.write_text(
        json.dumps(
            {"repo": lac.COLLECTOR_REPO, "commit_sha": COMMIT_A, "fetched_at": "t"}
        )
    )
    args_out = tmp_path / f"collector-args-{uuid4().hex}.json"
    monkeypatch.setenv("FAKE_COLLECTOR_ARGS_OUT", str(args_out))
    monkeypatch.setenv("FAKE_COLLECTOR_STDOUT", str(HOURLY_PAYLOAD))
    monkeypatch.delenv("FAKE_COLLECTOR_EXIT", raising=False)
    return args_out


def _index(layout: Layout) -> list[dict[str, object]]:
    return [json.loads(line) for line in layout.index_path.read_text().splitlines()]


@pytest.fixture
def held_lock(layout: Layout) -> Iterator[None]:
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    with (layout.state_dir / "run.lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# ------------------------------------------------------------------------------------------ run


def test_run_saves_the_collector_stdout_byte_for_byte_and_indexes_it(
    layout: Layout, collector: Path
) -> None:
    assert lac.command_run(layout, budget_mib=3, launcher=PYTHON_LAUNCHER) == 0

    payloads = sorted(layout.runs_dir.glob("*.jsonl"))
    assert len(payloads) == 1
    assert payloads[0].read_bytes() == HOURLY_PAYLOAD.read_bytes()
    [record] = _index(layout)
    summary = _summary_of(HOURLY_PAYLOAD)
    assert record["exit_code"] == 0
    assert record["payload_file"] == f"runs/{payloads[0].name}"
    assert (
        record["payload_sha256"]
        == hashlib.sha256(HOURLY_PAYLOAD.read_bytes()).hexdigest()
    )
    assert record["payload_bytes"] == HOURLY_PAYLOAD.stat().st_size
    assert record["record_count_by_source"] == summary["record_count_by_source"]
    assert record["collector_commit"] == COMMIT_A
    assert record["run_id"] == payloads[0].stem
    assert (
        layout.runs_dir / f"{payloads[0].stem}.stderr.log"
    ).read_text() == "fake collector diagnostics\n"
    assert not list(layout.runs_dir.glob("*.part"))

    seen = json.loads(collector.read_text())
    assert seen["budget_bytes"] == 3 * 1024 * 1024
    assert seen["script_version"] == f"local-{COMMIT_A[:12]}"
    assert seen["run_id"] == payloads[0].stem
    assert seen["cursors"] == {}


def test_run_advances_the_cursors_the_summary_names(
    layout: Layout, collector: Path
) -> None:
    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)

    cursor_by_source = _summary_of(HOURLY_PAYLOAD)["cursor_by_source"]
    assert isinstance(cursor_by_source, dict)
    assert json.loads(layout.cursors_path.read_text()) == {
        feed: json.loads(encoded) for feed, encoded in cursor_by_source.items()
    }


def test_second_run_hands_the_collector_the_saved_cursors(
    layout: Layout, collector: Path
) -> None:
    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)
    saved = json.loads(layout.cursors_path.read_text())

    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)

    assert json.loads(collector.read_text())["cursors"] == saved
    assert len(list(layout.runs_dir.glob("*.jsonl"))) == 2


def test_a_failed_feed_keeps_its_cursor_and_the_others_advance(
    layout: Layout, collector: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.cursors_path.write_text(
        json.dumps({"transcripts": {"old.jsonl": 17}, "retired_feed": {"x": 1}})
    )
    monkeypatch.setenv("FAKE_COLLECTOR_STDOUT", str(TRANSCRIPTS_FAILED_PAYLOAD))

    assert lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER) == 0

    cursors = json.loads(layout.cursors_path.read_text())
    assert cursors["transcripts"] == {"old.jsonl": 17}
    assert cursors["retired_feed"] == {"x": 1}
    assert cursors["git_numstat"] == {
        "last_sha": "1111111111111111111111111111111111111111"
    }
    [record] = _index(layout)
    assert (
        record["error_by_source"]
        == _summary_of(TRANSCRIPTS_FAILED_PAYLOAD)["error_by_source"]
    )


def test_a_non_zero_exit_keeps_the_output_as_failed_and_leaves_the_cursors_alone(
    layout: Layout, collector: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.cursors_path.write_text('{"transcripts": {"a.jsonl": 5}}\n')
    before = layout.cursors_path.read_bytes()
    monkeypatch.setenv("FAKE_COLLECTOR_EXIT", "37")

    assert lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER) == 1

    assert layout.cursors_path.read_bytes() == before
    assert not list(layout.runs_dir.glob("*.jsonl"))
    [failed] = list(layout.runs_dir.glob("*.failed"))
    assert failed.read_bytes() == HOURLY_PAYLOAD.read_bytes()
    [record] = _index(layout)
    assert record["exit_code"] == 37
    assert record["payload_file"] is None
    assert record["failed_file"] == f"runs/{failed.name}"
    assert record["error"] == "the collector exited with 37"


def test_output_without_a_run_summary_is_a_failed_run(
    layout: Layout, collector: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    truncated = tmp_path / "truncated.jsonl"
    truncated.write_bytes(HOURLY_PAYLOAD.read_bytes().splitlines(keepends=True)[0])
    monkeypatch.setenv("FAKE_COLLECTOR_STDOUT", str(truncated))

    assert lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER) == 1

    assert json.loads(layout.cursors_path.read_text()) == {}
    [record] = _index(layout)
    assert record["exit_code"] == 0
    assert (
        record["error"] == "the collector's output did not end with a run_summary line"
    )


def test_a_collector_that_cannot_start_is_a_recorded_failure(
    layout: Layout, collector: Path, tmp_path: Path
) -> None:
    missing = tmp_path / f"no-such-uv-{uuid4().hex}"

    assert lac.command_run(layout, budget_mib=1, launcher=(str(missing),)) == 1

    [record] = _index(layout)
    assert record["exit_code"] is None
    assert str(record["error"]).startswith("could not start the collector:")
    assert len(list(layout.runs_dir.glob("*.failed"))) == 1


def test_run_is_skipped_while_another_holds_the_lock(
    layout: Layout, collector: Path, held_lock: None
) -> None:
    assert lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER) == 0

    assert not layout.runs_dir.exists()
    assert not layout.index_path.exists()
    assert not collector.exists()


def test_run_without_a_fetched_collector_says_to_fetch(layout: Layout) -> None:
    with pytest.raises(
        lac.LocalAnalyticsCopyError, match="run the `fetch` command first"
    ):
        lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)


def test_run_refuses_an_unreadable_cursors_file_without_running(
    layout: Layout, collector: Path
) -> None:
    layout.cursors_path.write_text("{not json")

    with pytest.raises(lac.LocalAnalyticsCopyError, match="cursors.json is unreadable"):
        lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)

    assert not collector.exists()
    assert layout.cursors_path.read_text() == "{not json"


def test_collector_audit_files_land_in_the_skill_never_in_the_real_imbue_dir(
    layout: Layout, collector: Path
) -> None:
    real_imbue = layout.repo_root / "data" / ".imbue"
    before = _all_files(real_imbue)

    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)

    assert _all_files(real_imbue) == before
    assert (
        layout.analytics_dir / "README.md"
    ).read_text() == "what the collector reads\n"
    assert (
        len((layout.analytics_dir / "collections.jsonl").read_text().splitlines()) == 1
    )


def test_an_interrupted_run_is_kept_as_failed_and_indexed(
    layout: Layout, collector: Path
) -> None:
    layout.runs_dir.mkdir(parents=True)
    interrupted_id = uuid4().hex
    (layout.runs_dir / f"{interrupted_id}.jsonl.part").write_bytes(
        b'{"source": "transcripts"}\n'
    )

    assert lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER) == 0

    assert not list(layout.runs_dir.glob("*.part"))
    assert (
        layout.runs_dir / f"{interrupted_id}.failed"
    ).read_bytes() == b'{"source": "transcripts"}\n'
    interrupted, finished = _index(layout)
    assert interrupted["run_id"] == interrupted_id
    assert interrupted["error"] == "the run was interrupted before it finished"
    assert interrupted["payload_file"] is None
    assert finished["exit_code"] == 0


def test_diagnostics_are_pruned_to_the_newest_and_payloads_are_all_kept(
    layout: Layout, collector: Path
) -> None:
    layout.runs_dir.mkdir(parents=True)
    old_logs = [
        layout.runs_dir / f"old{number:03d}.stderr.log"
        for number in range(lac.KEPT_STDERR_LOGS + 5)
    ]
    old_failed = [
        layout.runs_dir / f"old{number:03d}.failed"
        for number in range(lac.KEPT_FAILED_PAYLOADS + 3)
    ]
    old_payloads = [
        layout.runs_dir / f"old{number:03d}.jsonl"
        for number in range(lac.KEPT_STDERR_LOGS + 20)
    ]
    for age, path in enumerate([*old_logs, *old_failed, *old_payloads]):
        path.write_text("x")
        os.utime(path, ns=(1_000_000_000 + age, 1_000_000_000 + age))

    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)

    logs = {path.name for path in layout.runs_dir.glob("*.stderr.log")}
    assert len(logs) == lac.KEPT_STDERR_LOGS
    # The run's own log is the newest; the six oldest planted logs are gone.
    assert {path.name for path in old_logs[:6]}.isdisjoint(logs)
    assert {path.name for path in old_logs[6:]} <= logs
    failed = {path.name for path in layout.runs_dir.glob("*.failed")}
    assert failed == {path.name for path in old_failed[3:]}
    assert len(list(layout.runs_dir.glob("*.jsonl"))) == len(old_payloads) + 1


def test_the_real_uv_launcher_runs_the_collector_without_touching_the_workspace_project(
    layout: Layout, collector: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    monkeypatch.setenv("UV_OFFLINE", "1")

    assert lac.command_run(layout, budget_mib=1) == 0

    [payload] = list(layout.runs_dir.glob("*.jsonl"))
    assert payload.read_bytes() == HOURLY_PAYLOAD.read_bytes()


# ------------------------------------------------------------------------------------- summary


def test_parse_summary_reads_a_summary_line_longer_than_the_read_block(
    tmp_path: Path,
) -> None:
    long_cursor = {
        f"agents/agent-{number}/events.jsonl": number for number in range(60_000)
    }
    summary = {
        "source": "run_summary",
        "cursor_by_source": {"transcripts": json.dumps(long_cursor)},
    }
    payload = tmp_path / "long.jsonl"
    payload.write_text(
        '{"source": "transcripts", "record": {}}\n' + json.dumps(summary) + "\n\n"
    )
    assert payload.stat().st_size > 1 << 20

    parsed = lac.parse_summary(payload)

    assert parsed is not None
    assert json.loads(parsed["cursor_by_source"]["transcripts"]) == long_cursor


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b'{"source": "transcripts", "record": {}}\n',
        b'{"source": "transcripts", "record": {}}\n{"source": "run_summ',
        b'["run_summary"]\n',
    ],
)
def test_parse_summary_returns_none_without_a_closing_summary(
    tmp_path: Path, content: bytes
) -> None:
    payload = tmp_path / "payload.jsonl"
    payload.write_bytes(content)

    assert lac.parse_summary(payload) is None


def test_parse_summary_reads_the_fixture_payloads() -> None:
    hourly = lac.parse_summary(HOURLY_PAYLOAD)
    failed_feed = lac.parse_summary(TRANSCRIPTS_FAILED_PAYLOAD)

    assert hourly is not None and failed_feed is not None
    assert hourly["record_count_by_source"] == {
        "client_activity": 0,
        "gateway_requests": 0,
        "git_numstat": 0,
        "servers": 0,
        "services": 0,
        "transcripts": 0,
        "workspace_state": 1,
    }
    assert sorted(failed_feed["error_by_source"]) == ["transcripts"]


def test_merged_cursors_takes_text_or_objects_and_skips_what_it_cannot_read() -> None:
    summary = {
        "cursor_by_source": {
            "transcripts": '{"a.jsonl": 3}',
            "services": {"b.jsonl": 4},
            "servers": "not json",
            "client_activity": "[1, 2]",
            "git_numstat": 7,
        }
    }

    merged, unreadable = lac.merged_cursors(
        {"servers": {"s.jsonl": 1}, "kept": {"k": 2}}, summary
    )

    assert merged == {
        "transcripts": {"a.jsonl": 3},
        "services": {"b.jsonl": 4},
        "servers": {"s.jsonl": 1},
        "kept": {"k": 2},
    }
    assert unreadable == ["client_activity", "git_numstat", "servers"]


# ---------------------------------------------------------------------------------------- view


def test_rebuilding_the_view_never_deletes_anything_outside_it(
    layout: Layout, tmp_path: Path
) -> None:
    real_before = {
        **_all_files(layout.repo_root, layout.state_dir),
        **_all_files(layout.host_dir),
    }

    lac.build_view(layout)
    workspace_view, host_view = lac.build_view(layout)
    shutil.rmtree(layout.view_dir)
    lac.build_view(layout)

    assert {
        **_all_files(layout.repo_root, layout.state_dir),
        **_all_files(layout.host_dir),
    } == real_before
    assert (workspace_view / "README.md").resolve() == (
        layout.repo_root / "README.md"
    ).resolve()
    assert (
        workspace_view / "data" / ".imbue" / "keep.txt"
    ).read_text() == "not the skill's"
    assert (
        workspace_view / "data" / ".imbue" / "analytics"
    ).resolve() == layout.analytics_dir.resolve()
    assert not (layout.repo_root / "data" / ".imbue" / "analytics").exists()
    assert host_view == layout.view_dir / "host"


def test_a_view_that_is_a_link_elsewhere_is_unlinked_not_emptied(
    layout: Layout, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep me")
    layout.state_dir.mkdir(parents=True)
    layout.view_dir.symlink_to(elsewhere)

    lac.build_view(layout)

    assert (elsewhere / "precious.txt").read_text() == "keep me"
    assert not layout.view_dir.is_symlink()
    assert (layout.view_dir / "workspace").is_dir()


def test_the_view_bridges_the_shell_client_activity_log(layout: Layout) -> None:
    _, host_view = lac.build_view(layout)

    agents = {
        path.name: path
        for path in (layout.host_dir / "agents").iterdir()
        if path.is_dir()
    }
    shell_id = next(
        name
        for name, path in agents.items()
        if lac.SHELL_AGENT_NAME in (path / "data.json").read_text()
    )
    chat_id = next(name for name in agents if name != shell_id)
    shell_view = host_view / "agents" / shell_id
    events = (
        shell_view / "workspace_layout" / "events" / "client_activity" / "events.jsonl"
    )
    assert events.read_text() == '{"type": "window_opened"}\n'
    assert not shell_view.is_symlink()
    assert (
        shell_view / "events" / "services" / "events.jsonl"
    ).read_text() == '{"service": "chat"}\n'
    assert (host_view / "agents" / chat_id).is_symlink()
    assert sorted(path.name for path in (host_view / "agents").iterdir()) == sorted(
        agents
    )


def test_the_bridge_is_skipped_when_the_agent_has_its_own_workspace_layout(
    layout: Layout,
) -> None:
    shell_dir = next(
        path
        for path in (layout.host_dir / "agents").iterdir()
        if path.is_dir() and lac.SHELL_AGENT_NAME in (path / "data.json").read_text()
    )
    own = shell_dir / "workspace_layout" / "events" / "client_activity"
    own.mkdir(parents=True)
    (own / "events.jsonl").write_text('{"type": "the agent dir own"}\n')

    _, host_view = lac.build_view(layout)

    shell_view = host_view / "agents" / shell_dir.name
    assert shell_view.is_symlink()
    assert (
        shell_view / "workspace_layout" / "events" / "client_activity" / "events.jsonl"
    ).read_text() == ('{"type": "the agent dir own"}\n')


def test_a_host_dir_without_agents_gives_an_empty_agents_view(layout: Layout) -> None:
    shutil.rmtree(layout.host_dir / "agents")

    _, host_view = lac.build_view(layout)

    assert list((host_view / "agents").iterdir()) == []


def test_the_view_is_kept_out_of_the_backup_and_comes_back_after_deletion(
    layout: Layout,
) -> None:
    lac.build_view(layout)
    assert (layout.view_dir / ".nobackup").is_file()

    shutil.rmtree(layout.view_dir)
    lac.build_view(layout)

    assert (layout.view_dir / ".nobackup").is_file()
    assert (
        layout.view_dir / "workspace" / "README.md"
    ).read_text() == "workspace readme"


# --------------------------------------------------------------------------------------- fetch


class FakeGitHub:
    """Serves a collector directory at one or more commits; records every request."""

    def __init__(
        self,
        files_by_commit: dict[str, dict[str, bytes]],
        ref_target: str,
        failing_name: str | None = None,
    ):
        self.files_by_commit = files_by_commit
        self.ref_target = ref_target
        self.failing_name = failing_name
        self.requests: list[str] = []

    def __call__(self, url_path: str, accept: str) -> bytes:
        self.requests.append(url_path)
        prefix = f"repos/{lac.COLLECTOR_REPO}/"
        if url_path.startswith(prefix + "commits/"):
            assert accept == "application/vnd.github.sha"
            return (self.ref_target + "\n").encode()
        path, _, query = url_path.removeprefix(prefix + "contents/").partition("?ref=")
        ref = self.ref_target if query == lac.UPSTREAM_BRANCH else query
        files = self.files_by_commit[ref]
        if path == lac.COLLECTOR_DIR_IN_REPO:
            listing = [
                {"name": name, "type": "file", "sha": _git_blob_sha(content)}
                for name, content in files.items()
            ]
            return json.dumps(
                [*listing, {"name": "fixtures", "type": "dir", "sha": "0" * 40}]
            ).encode()
        name = path.removeprefix(lac.COLLECTOR_DIR_IN_REPO + "/")
        assert accept == "application/vnd.github.raw+json"
        if name == self.failing_name:
            raise lac.LocalAnalyticsCopyError(
                f"GitHub request for {url_path} failed (exit 22)"
            )
        return files[name]


COLLECTOR_FILES_A = {
    "__init__.py": b"",
    "collect.py": b"print('collector A')\n",
    "collect_test.py": b"never downloaded\n",
    "workspace_feeds.py": b"FEEDS = 'A'\n",
    "workspace_redaction.py": b"REDACTION = 'A'\n",
}
COLLECTOR_FILES_B = {
    **COLLECTOR_FILES_A,
    "collect.py": b"print('collector B')\n",
    "workspace_new.py": b"NEW = 1\n",
}


def test_fetch_lays_the_collector_out_as_imbue_does_and_records_its_source(
    layout: Layout,
) -> None:
    github = FakeGitHub({COMMIT_A: COLLECTOR_FILES_A}, ref_target=COMMIT_A)

    lac.command_fetch(layout, "main", github)

    injected = layout.analytics_dir / lac.INJECTED_PACKAGE_RELPATH
    assert (layout.analytics_dir / "collect.py").read_bytes() == COLLECTOR_FILES_A[
        "collect.py"
    ]
    assert sorted(path.name for path in injected.iterdir()) == [
        "__init__.py",
        "workspace_feeds.py",
        "workspace_redaction.py",
    ]
    assert not any("collect_test.py" in request for request in github.requests)
    source = json.loads(layout.source_path.read_text())
    assert source["commit_sha"] == COMMIT_A
    assert source["requested_ref"] == "main"
    assert source["file_sha256"] == {
        name: hashlib.sha256(content).hexdigest()
        for name, content in COLLECTOR_FILES_A.items()
        if name != "collect_test.py"
    }
    [history] = (layout.state_dir / "sources.jsonl").read_text().splitlines()
    assert json.loads(history) == source


def test_a_failed_download_leaves_the_previous_collector_whole(layout: Layout) -> None:
    lac.command_fetch(
        layout, COMMIT_A, FakeGitHub({COMMIT_A: COLLECTOR_FILES_A}, ref_target=COMMIT_A)
    )
    before = _all_files(layout.analytics_dir)
    failing = FakeGitHub(
        {COMMIT_B: COLLECTOR_FILES_B},
        ref_target=COMMIT_B,
        failing_name="workspace_redaction.py",
    )

    with pytest.raises(lac.LocalAnalyticsCopyError, match="exit 22"):
        lac.command_fetch(layout, "main", failing)

    assert _all_files(layout.analytics_dir) == before
    assert len((layout.state_dir / "sources.jsonl").read_text().splitlines()) == 1


def test_fetch_refuses_a_directory_without_the_entrypoint(layout: Layout) -> None:
    files = {
        name: content
        for name, content in COLLECTOR_FILES_A.items()
        if name != "collect.py"
    }

    with pytest.raises(lac.LocalAnalyticsCopyError, match="collect.py not found"):
        lac.command_fetch(
            layout, "main", FakeGitHub({COMMIT_A: files}, ref_target=COMMIT_A)
        )

    assert not layout.source_path.exists()


def test_fetch_refuses_to_swap_the_collector_during_a_run(
    layout: Layout, held_lock: None
) -> None:
    github = FakeGitHub({COMMIT_A: COLLECTOR_FILES_A}, ref_target=COMMIT_A)

    with pytest.raises(
        lac.LocalAnalyticsCopyError, match="collection run is in progress"
    ):
        lac.command_fetch(layout, "main", github)

    assert github.requests == []


def test_parse_collector_listing_reads_the_real_listing_shape() -> None:
    assert lac.parse_collector_listing(LISTING.read_bytes()) == {
        "__init__.py": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "collect.py": "91257204f4526111a6c84e19744bb5c8c58c2302",
        "workspace_feeds.py": "71265a2e74f0d4f0f486ff68a5d5616399e9a0f0",
        "workspace_redaction.py": "057b141d8f210f2587e7532df5a12eecc59d807c",
    }


@pytest.mark.parametrize("body", [b"not json", b'{"message": "Not Found"}'])
def test_parse_collector_listing_rejects_what_is_not_a_listing(body: bytes) -> None:
    with pytest.raises(lac.LocalAnalyticsCopyError):
        lac.parse_collector_listing(body)


# -------------------------------------------------------------------------------------- status


def test_status_says_main_has_the_same_collector(layout: Layout) -> None:
    github = FakeGitHub({COMMIT_A: COLLECTOR_FILES_A}, ref_target=COMMIT_A)
    lac.command_fetch(layout, "main", github)

    lines = lac.status_lines(layout, github)

    assert lines[0].startswith(f"Collector: {lac.COLLECTOR_REPO}@{COMMIT_A[:10]} ")
    assert lines[1] == f"Upstream: {lac.COLLECTOR_REPO} main has the same collector"
    assert lines[2] == "Runs: 0 (0 ok, 0 failed)"
    assert lines[3] == "Runs folder: 0 bytes (0 bytes)"


def test_status_names_the_files_main_has_changed_with_one_github_call(
    layout: Layout,
) -> None:
    lac.command_fetch(
        layout, "main", FakeGitHub({COMMIT_A: COLLECTOR_FILES_A}, ref_target=COMMIT_A)
    )
    moved = FakeGitHub({COMMIT_B: COLLECTOR_FILES_B}, ref_target=COMMIT_B)

    lines = lac.status_lines(layout, moved)

    assert lines[1] == (
        f"Upstream: {lac.COLLECTOR_REPO} main has moved past the fetched collector "
        "(collect.py, workspace_new.py changed); run `fetch` to take it"
    )
    assert len(moved.requests) == 1


def test_status_says_quietly_when_it_could_not_check(layout: Layout) -> None:
    lac.command_fetch(
        layout, "main", FakeGitHub({COMMIT_A: COLLECTOR_FILES_A}, ref_target=COMMIT_A)
    )

    def unreachable(url_path: str, accept: str) -> bytes:
        raise lac.LocalAnalyticsCopyError(
            "GitHub request failed (exit 6): Could not resolve host"
        )

    lines = lac.status_lines(layout, unreachable)

    assert lines[1] == f"Upstream: could not check {lac.COLLECTOR_REPO} main"


def test_status_lists_recent_runs_and_their_failures(
    layout: Layout, collector: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)
    monkeypatch.setenv("FAKE_COLLECTOR_EXIT", "41")
    lac.command_run(layout, budget_mib=1, launcher=PYTHON_LAUNCHER)
    github = FakeGitHub(
        {COMMIT_A: {"collect.py": FAKE_COLLECTOR.encode()}}, ref_target=COMMIT_A
    )

    lines = lac.status_lines(layout, github)

    assert lines[2] == "Runs: 2 (1 ok, 1 failed)"
    assert "'workspace_state': 1" in lines[3]
    assert lines[4].endswith("exit=41 failed: the collector exited with 41")
    expected_bytes = sum(path.stat().st_size for path in layout.runs_dir.iterdir())
    assert lines[5].endswith(f"({expected_bytes} bytes)")


def test_status_warns_once_the_runs_folder_passes_two_gigabytes(
    layout: Layout, collector: Path
) -> None:
    layout.runs_dir.mkdir(parents=True)
    big = layout.runs_dir / f"{uuid4().hex}.jsonl"
    with big.open("wb") as handle:
        handle.truncate(lac.RUNS_SIZE_WARNING_BYTES)
    github = FakeGitHub(
        {COMMIT_A: {"collect.py": FAKE_COLLECTOR.encode()}}, ref_target=COMMIT_A
    )

    at_limit = lac.status_lines(layout, github)
    (layout.runs_dir / f"{uuid4().hex}.stderr.log").write_bytes(b"x")
    over_limit = lac.status_lines(layout, github)

    assert not any(line.startswith("Warning:") for line in at_limit)
    assert at_limit[-1] == f"Runs folder: 2.0 GB ({lac.RUNS_SIZE_WARNING_BYTES} bytes)"
    assert over_limit[-1] == (
        "Warning: the runs folder is over 2.0 GB. Nothing is deleted automatically; decide whether to keep all of it."
    )
    assert big.stat().st_size == lac.RUNS_SIZE_WARNING_BYTES


def test_status_before_any_fetch(layout: Layout) -> None:
    def never_called(url_path: str, accept: str) -> bytes:
        raise AssertionError("status made a GitHub call with nothing fetched")

    assert lac.status_lines(layout, never_called) == ["No collector fetched yet"]
