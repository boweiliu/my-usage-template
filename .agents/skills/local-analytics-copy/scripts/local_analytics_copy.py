"""Keep a local copy of the exact analytics payload Imbue's collection would take from this workspace.

Imbue's analytics service (for explorer-plan workspaces) writes its collection script into
``data/.imbue/analytics/`` and runs it; the script's stdout is the whole payload. This runs that same
script, fetched from the mngr-internal repo, against this workspace and keeps each run's stdout as a
file, so the usage data can be analyzed here.

Everything lives under ``data/.skills/local-analytics-copy/``:

- ``analytics/``: the collector as Imbue lays it out (``collect.py``, ``imbue/analytics/injected/``),
  plus its own ``README.md`` and ``collections.jsonl`` audit file, and ``SOURCE.json`` saying which
  mngr-internal commit it came from.
- ``runs/<run_id>.jsonl``: one run's payload, byte for byte (``{"source", "record"}`` lines, then a
  ``run_summary`` line). ``runs/<run_id>.stderr.log`` is the collector's diagnostics, and
  ``runs/<run_id>.failed`` the output of a run that failed. Payloads are never deleted; the newest
  ``KEPT_STDERR_LOGS`` logs and ``KEPT_FAILED_PAYLOADS`` failed outputs are kept.
- ``runs.jsonl``: an index of runs. ``cursors.json``: where each feed stopped, so a run only takes
  what is new.
- ``view/``: rebuilt on every run, and left out of the backup. The collector reads a workspace root
  and a host dir; these are symlink mirrors of the real ones, so its audit files land in
  ``analytics/`` rather than in the real ``data/.imbue/``. One bridge: the collector looks for the
  shell's client-activity log at ``agents/<id>/workspace_layout/events/client_activity/``, while this
  workspace keeps it at ``data/.state/system_interface/events/client_activity/``, so the view links
  one to the other when the agent dir has no ``workspace_layout`` of its own.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

SKILL_NAME: Final[str] = "local-analytics-copy"
COLLECTOR_REPO: Final[str] = "imbue-ai/mngr-internal"
COLLECTOR_DIR_IN_REPO: Final[str] = "apps/analytics/imbue/analytics/injected"
UPSTREAM_BRANCH: Final[str] = "main"
# The service-side layout puts the entrypoint at the top and the library modules under this path.
INJECTED_PACKAGE_RELPATH: Final[str] = "imbue/analytics/injected"
ENTRYPOINT_NAME: Final[str] = "collect.py"
RUN_SUMMARY_SOURCE: Final[str] = "run_summary"
SHELL_AGENT_NAME: Final[str] = "system-services"
SHELL_STATE_RELPATH: Final[str] = "data/.state/system_interface"
DEFAULT_BUDGET_MIB: Final[int] = 64
# The collector brings its own dependencies through its PEP 723 header; --no-project keeps uv away
# from the workspace's own project and lock.
COLLECTOR_LAUNCHER: Final[tuple[str, ...]] = ("uv", "run", "--no-project", "--script")
FETCH_CONCURRENCY: Final[int] = 4
GITHUB_TIMEOUT_SECONDS: Final[int] = 30
KEPT_STDERR_LOGS: Final[int] = 100
KEPT_FAILED_PAYLOADS: Final[int] = 10
RUNS_SIZE_WARNING_BYTES: Final[int] = 2 * 1024**3

# (API path under https://api.github.com/, Accept header) -> response body.
GitHubGet = Callable[[str, str], bytes]


class LocalAnalyticsCopyError(Exception):
    """A step failed in a way the operator should read about."""


@dataclass(frozen=True)
class Layout:
    """The real directories a run reads, and where this skill keeps its state."""

    repo_root: Path
    host_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.repo_root / "data" / ".skills" / SKILL_NAME

    @property
    def analytics_dir(self) -> Path:
        return self.state_dir / "analytics"

    @property
    def source_path(self) -> Path:
        return self.analytics_dir / "SOURCE.json"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def view_dir(self) -> Path:
        return self.state_dir / "view"

    @property
    def cursors_path(self) -> Path:
        return self.state_dir / "cursors.json"

    @property
    def index_path(self) -> Path:
        return self.state_dir / "runs.jsonl"


def default_layout() -> Layout:
    # .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py
    repo_root = Path(__file__).resolve().parents[4]
    configured_host_dir = os.environ.get("MNGR_HOST_DIR")
    return Layout(
        repo_root=repo_root,
        host_dir=Path(configured_host_dir)
        if configured_host_dir
        else Path.home() / ".mngr",
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _append_json_line(path: Path, record: Mapping[str, object]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


@contextmanager
def _exclusive_lock(layout: Layout) -> Generator[bool, None, None]:
    """Hold the skill's lock for the block; yields False (holding nothing) when another process has it."""
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    with (layout.state_dir / "run.lock").open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def github_get_through_latchkey(url_path: str, accept: str) -> bytes:
    """GET one GitHub API path through the user's connected account (the repo is private)."""
    command = [
        "latchkey", "curl", "-sS", "-f", "--max-time", str(GITHUB_TIMEOUT_SECONDS),
        "-H", f"Accept: {accept}", f"https://api.github.com/{url_path}",
    ]  # fmt: skip
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as error:
        raise LocalAnalyticsCopyError(
            f"Could not run latchkey for {url_path}: {error}"
        ) from error
    if result.returncode != 0:
        raise LocalAnalyticsCopyError(
            f"GitHub request for {url_path} failed (exit {result.returncode}): {result.stderr.decode(errors='replace')[:300]}"
        )
    return result.stdout


# --------------------------------------------------------------------------------------- fetch


def _collector_destination(analytics_dir: Path, name: str) -> Path:
    return (
        analytics_dir / name
        if name == ENTRYPOINT_NAME
        else analytics_dir / INJECTED_PACKAGE_RELPATH / name
    )


def parse_collector_listing(body: bytes) -> dict[str, str]:
    """The collector's files in a GitHub contents listing, as name -> git blob sha (tests left out)."""
    try:
        listing = json.loads(body)
    except ValueError as error:
        raise LocalAnalyticsCopyError(
            f"GitHub returned an unreadable listing of {COLLECTOR_DIR_IN_REPO}"
        ) from error
    if not isinstance(listing, list):
        raise LocalAnalyticsCopyError(
            f"GitHub did not return a directory listing for {COLLECTOR_DIR_IN_REPO}"
        )
    files: dict[str, str] = {}
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if (
            entry.get("type") == "file"
            and isinstance(name, str)
            and name.endswith(".py")
            and not name.endswith("_test.py")
        ):
            files[name] = str(entry.get("sha", ""))
    return dict(sorted(files.items()))


def _resolve_commit(ref: str, github_get: GitHubGet) -> str:
    commit_sha = (
        github_get(
            f"repos/{COLLECTOR_REPO}/commits/{ref}", "application/vnd.github.sha"
        )
        .decode()
        .strip()
    )
    if len(commit_sha) != 40:
        raise LocalAnalyticsCopyError(f"GitHub did not return a commit for {ref!r}")
    return commit_sha


def command_fetch(
    layout: Layout, ref: str, github_get: GitHubGet = github_get_through_latchkey
) -> None:
    """Download the collector at ``ref`` (a branch, tag or sha) into ``analytics/``."""
    with _exclusive_lock(layout) as is_locked:
        if not is_locked:
            raise LocalAnalyticsCopyError(
                "A collection run is in progress; fetch again once it finishes"
            )
        _fetch_locked(layout, ref, github_get)


def _fetch_locked(layout: Layout, ref: str, github_get: GitHubGet) -> None:
    commit_sha = _resolve_commit(ref, github_get)
    names = list(
        parse_collector_listing(
            github_get(
                f"repos/{COLLECTOR_REPO}/contents/{COLLECTOR_DIR_IN_REPO}?ref={commit_sha}",
                "application/json",
            )
        )
    )
    if ENTRYPOINT_NAME not in names:
        raise LocalAnalyticsCopyError(
            f"{ENTRYPOINT_NAME} not found in {COLLECTOR_DIR_IN_REPO} at {commit_sha}"
        )

    def download(name: str) -> bytes:
        return github_get(
            f"repos/{COLLECTOR_REPO}/contents/{COLLECTOR_DIR_IN_REPO}/{name}?ref={commit_sha}",
            "application/vnd.github.raw+json",
        )

    # Every file is downloaded before any is written, so a failed download never leaves two versions mixed.
    with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
        contents = list(pool.map(download, names))

    file_hashes: dict[str, str] = {}
    for name, content in zip(names, contents, strict=True):
        _write_atomically(_collector_destination(layout.analytics_dir, name), content)
        file_hashes[name] = hashlib.sha256(content).hexdigest()
    source = {
        "repo": COLLECTOR_REPO,
        "path": COLLECTOR_DIR_IN_REPO,
        "requested_ref": ref,
        "commit_sha": commit_sha,
        "fetched_at": _now(),
        "file_sha256": file_hashes,
    }
    _write_atomically(
        layout.source_path,
        (json.dumps(source, indent=2, sort_keys=True) + "\n").encode(),
    )
    _append_json_line(layout.state_dir / "sources.jsonl", source)
    print(
        f"Fetched {len(names)} files from {COLLECTOR_REPO}@{commit_sha[:10]} into {layout.analytics_dir}"
    )


def _read_source(layout: Layout) -> dict[str, Any]:
    try:
        source = json.loads(layout.source_path.read_text())
    except (OSError, ValueError) as error:
        raise LocalAnalyticsCopyError(
            f"{layout.source_path} is unreadable; run the `fetch` command again"
        ) from error
    if not isinstance(source, dict) or not isinstance(source.get("commit_sha"), str):
        raise LocalAnalyticsCopyError(
            f"{layout.source_path} names no commit; run the `fetch` command again"
        )
    return source


# ------------------------------------------------------------------------------------- the view


def _mirror_children(real_dir: Path, view_dir: Path, skip: frozenset[str]) -> None:
    view_dir.mkdir(parents=True, exist_ok=True)
    for child in sorted(real_dir.iterdir()):
        if child.name not in skip:
            (view_dir / child.name).symlink_to(child)


def _agent_name(agent_dir: Path) -> str:
    try:
        return str(json.loads((agent_dir / "data.json").read_text()).get("name", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def _remove_view(view: Path) -> None:
    """Remove the old view without ever following a link out of it."""
    if view.is_symlink() or view.is_file():
        view.unlink()
    elif view.is_dir():
        # rmtree removes the symlinks inside the view, never what they point at.
        shutil.rmtree(view)


def build_view(layout: Layout) -> tuple[Path, Path]:
    """Rebuild ``view/`` and return its (workspace_root, host_dir)."""
    view = layout.view_dir
    _remove_view(view)
    view.mkdir(parents=True)
    # The view is only links, rebuilt every run: keep it out of the hourly backup.
    (view / ".nobackup").touch()

    repo_root = layout.repo_root
    workspace_view = view / "workspace"
    _mirror_children(repo_root, workspace_view, frozenset({"data"}))
    data_view = workspace_view / "data"
    _mirror_children(repo_root / "data", data_view, frozenset({".imbue"}))
    imbue_view = data_view / ".imbue"
    real_imbue = repo_root / "data" / ".imbue"
    if real_imbue.is_dir():
        _mirror_children(real_imbue, imbue_view, frozenset({"analytics"}))
    imbue_view.mkdir(parents=True, exist_ok=True)
    layout.analytics_dir.mkdir(parents=True, exist_ok=True)
    (imbue_view / "analytics").symlink_to(layout.analytics_dir)

    host_view = view / "host"
    agents_view = host_view / "agents"
    agents_view.mkdir(parents=True)
    real_agents = layout.host_dir / "agents"
    shell_state = repo_root / SHELL_STATE_RELPATH
    agent_dirs = sorted(real_agents.iterdir()) if real_agents.is_dir() else []
    for agent_dir in agent_dirs:
        if not agent_dir.is_dir():
            continue
        is_bridged = (
            _agent_name(agent_dir) == SHELL_AGENT_NAME
            and shell_state.is_dir()
            and not (agent_dir / "workspace_layout").exists()
        )
        if is_bridged:
            _mirror_children(
                agent_dir, agents_view / agent_dir.name, frozenset({"workspace_layout"})
            )
            (agents_view / agent_dir.name / "workspace_layout").symlink_to(shell_state)
        else:
            (agents_view / agent_dir.name).symlink_to(agent_dir)
    return workspace_view, host_view


# ----------------------------------------------------------------------------------------- run


def _load_cursors(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise LocalAnalyticsCopyError(
            f"{path} is unreadable; fix it, or delete it to collect everything again"
        ) from error
    if not isinstance(parsed, dict):
        raise LocalAnalyticsCopyError(
            f"{path} does not hold a JSON object; fix it, or delete it to collect everything again"
        )
    return {
        str(source): cursor
        for source, cursor in parsed.items()
        if isinstance(cursor, dict)
    }


def _decoded_cursor(encoded: object) -> dict[str, object] | None:
    """A summary cursor: the collector writes each one as JSON text; an object is taken as is."""
    if isinstance(encoded, dict):
        return encoded
    if isinstance(encoded, str):
        try:
            decoded = json.loads(encoded)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def merged_cursors(
    cursors: dict[str, dict[str, object]], summary: dict[str, Any]
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Advance only the feeds the summary names; returns the new cursors and any feeds left unreadable."""
    merged = dict(cursors)
    unreadable: list[str] = []
    cursor_by_source = summary.get("cursor_by_source")
    for feed, encoded in (
        cursor_by_source if isinstance(cursor_by_source, dict) else {}
    ).items():
        decoded = _decoded_cursor(encoded)
        if decoded is None:
            unreadable.append(str(feed))
        else:
            merged[str(feed)] = decoded
    return merged, sorted(unreadable)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _last_line(path: Path) -> bytes:
    """The file's last non-empty line, however long it is."""
    block_size = 1 << 16
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        tail = b""
        while position > 0:
            step = min(block_size, position)
            position -= step
            handle.seek(position)
            tail = handle.read(step) + tail
            content = tail.rstrip(b"\r\n")
            if b"\n" in content:
                return content.rsplit(b"\n", 1)[1]
        return tail.rstrip(b"\r\n")


def parse_summary(payload_path: Path) -> dict[str, Any] | None:
    """The payload's closing ``run_summary`` line, or None when it does not end with one."""
    try:
        last = json.loads(_last_line(payload_path))
    except ValueError:
        return None
    return (
        last
        if isinstance(last, dict) and last.get("source") == RUN_SUMMARY_SOURCE
        else None
    )


def _keep_newest(paths: Iterable[Path], count: int) -> None:
    ordered = sorted(
        paths, key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True
    )
    for stale in ordered[count:]:
        stale.unlink(missing_ok=True)


def _prune_diagnostics(runs_dir: Path) -> None:
    """Bound the diagnostics; payloads (``*.jsonl``) are the user's data and are never deleted."""
    _keep_newest(runs_dir.glob("*.stderr.log"), KEPT_STDERR_LOGS)
    _keep_newest(runs_dir.glob("*.failed"), KEPT_FAILED_PAYLOADS)


def _recover_interrupted_runs(layout: Layout) -> None:
    """A ``.part`` left while nobody holds the lock is a run that was killed: keep it as ``.failed``."""
    for partial_path in sorted(layout.runs_dir.glob("*.jsonl.part")):
        run_id = partial_path.name.removesuffix(".jsonl.part")
        failed_path = layout.runs_dir / f"{run_id}.failed"
        partial_path.replace(failed_path)
        _append_json_line(
            layout.index_path,
            {
                "run_id": run_id,
                "started_at": None,
                "finished_at": None,
                "recovered_at": _now(),
                "exit_code": None,
                "payload_file": None,
                "failed_file": str(failed_path.relative_to(layout.state_dir)),
                "error": "the run was interrupted before it finished",
            },
        )
        print(
            f"Run {run_id} was interrupted earlier; kept its partial output as {failed_path}"
        )


def command_run(
    layout: Layout, budget_mib: int, launcher: Sequence[str] = COLLECTOR_LAUNCHER
) -> int:
    entrypoint = layout.analytics_dir / ENTRYPOINT_NAME
    if not entrypoint.is_file() or not layout.source_path.is_file():
        raise LocalAnalyticsCopyError("No collector yet; run the `fetch` command first")
    with _exclusive_lock(layout) as is_locked:
        if not is_locked:
            print("Another run is in progress; skipping this one")
            return 0
        commit_sha = _read_source(layout)["commit_sha"]
        return _run_locked(layout, entrypoint, commit_sha, budget_mib, launcher)


def _run_locked(
    layout: Layout,
    entrypoint: Path,
    commit_sha: str,
    budget_mib: int,
    launcher: Sequence[str],
) -> int:
    runs_dir = layout.runs_dir
    runs_dir.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_runs(layout)
    cursors = _load_cursors(layout.cursors_path)
    if not layout.cursors_path.is_file():
        _write_atomically(layout.cursors_path, b"{}\n")
    workspace_root, host_dir = build_view(layout)

    run_id = uuid.uuid4().hex
    payload_path = runs_dir / f"{run_id}.jsonl"
    partial_path = runs_dir / f"{run_id}.jsonl.part"
    stderr_path = runs_dir / f"{run_id}.stderr.log"
    command = [
        *launcher, str(entrypoint),
        "--run-id", run_id,
        "--script-version", f"local-{commit_sha[:12]}",
        "--workspace-root", str(workspace_root),
        "--host-dir", str(host_dir),
        "--cursors-file", str(layout.cursors_path),
        "--budget-bytes", str(budget_mib * 1024 * 1024),
    ]  # fmt: skip
    started_at = _now()
    exit_code: int | None = None
    launch_error: str | None = None
    # The collector's stdout goes straight to the file: the payload is never read back and rewritten.
    with partial_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        try:
            exit_code = subprocess.run(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                cwd=layout.state_dir,
            ).returncode
        except OSError as error:
            launch_error = f"could not start the collector: {error}"

    summary = parse_summary(partial_path) if exit_code == 0 else None
    record: dict[str, object] = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _now(),
        "exit_code": exit_code,
        "collector_commit": commit_sha,
    }
    if summary is None:
        failed_path = runs_dir / f"{run_id}.failed"
        partial_path.replace(failed_path)
        if launch_error is not None:
            error = launch_error
        elif exit_code == 0:
            error = "the collector's output did not end with a run_summary line"
        else:
            error = f"the collector exited with {exit_code}"
        record.update(
            payload_file=None,
            failed_file=str(failed_path.relative_to(layout.state_dir)),
            error=error,
        )
        _append_json_line(layout.index_path, record)
        _prune_diagnostics(runs_dir)
        print(f"Run {run_id} failed: {error}; see {stderr_path}")
        return 1

    # Keep the payload before advancing the cursors: a crash in between re-collects, it never loses records.
    partial_path.replace(payload_path)
    new_cursors, unreadable_cursors = merged_cursors(cursors, summary)
    _write_atomically(
        layout.cursors_path,
        (json.dumps(new_cursors, indent=2, sort_keys=True) + "\n").encode(),
    )
    record_counts = summary.get("record_count_by_source", {})
    feed_errors = summary.get("error_by_source", {})
    record.update(
        payload_file=str(payload_path.relative_to(layout.state_dir)),
        payload_bytes=payload_path.stat().st_size,
        payload_sha256=_file_sha256(payload_path),
        record_count_by_source=record_counts,
        error_by_source=feed_errors,
        read_bytes=summary.get("read_bytes", 0),
        is_budget_exhausted=summary.get("is_budget_exhausted", False),
    )
    if unreadable_cursors:
        record["unreadable_cursor_sources"] = unreadable_cursors
    _append_json_line(layout.index_path, record)
    _prune_diagnostics(runs_dir)

    counts = (
        ", ".join(f"{feed}={count}" for feed, count in sorted(record_counts.items()))
        if isinstance(record_counts, dict)
        else ""
    )
    print(f"Run {run_id}: {record['payload_bytes']} bytes; records: {counts or 'none'}")
    if feed_errors:
        print(f"Feed errors: {feed_errors}")
    if unreadable_cursors:
        print(
            f"Unreadable cursors left where they were (those feeds repeat next run): {', '.join(unreadable_cursors)}"
        )
    if record["is_budget_exhausted"]:
        print("Input budget exhausted; the next run continues from the saved cursors")
    return 0


# --------------------------------------------------------------------------------------- status


def _git_blob_sha(path: Path) -> str | None:
    try:
        content = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def changed_collector_files(
    layout: Layout, source: dict[str, Any], upstream_files: dict[str, str]
) -> list[str]:
    """Collector files that differ between the fetched copy and ``upstream_files`` (name -> git blob sha)."""
    file_sha256 = source.get("file_sha256")
    fetched_names = set(file_sha256) if isinstance(file_sha256, dict) else set()
    return sorted(
        name
        for name in set(upstream_files) | fetched_names
        if name not in upstream_files
        or name not in fetched_names
        or _git_blob_sha(_collector_destination(layout.analytics_dir, name))
        != upstream_files[name]
    )


def _upstream_line(
    layout: Layout, source: dict[str, Any], github_get: GitHubGet
) -> str:
    try:
        upstream_files = parse_collector_listing(
            github_get(
                f"repos/{COLLECTOR_REPO}/contents/{COLLECTOR_DIR_IN_REPO}?ref={UPSTREAM_BRANCH}",
                "application/json",
            )
        )
    except (LocalAnalyticsCopyError, ValueError):
        return f"Upstream: could not check {COLLECTOR_REPO} {UPSTREAM_BRANCH}"
    changed = changed_collector_files(layout, source, upstream_files)
    if not changed:
        return f"Upstream: {COLLECTOR_REPO} {UPSTREAM_BRANCH} has the same collector"
    return (
        f"Upstream: {COLLECTOR_REPO} {UPSTREAM_BRANCH} has moved past the fetched collector "
        f"({', '.join(changed)} changed); run `fetch` to take it"
    )


def _read_index(layout: Layout) -> list[dict[str, Any]]:
    if not layout.index_path.is_file():
        return []
    runs: list[dict[str, Any]] = []
    for line in layout.index_path.read_text().splitlines():
        try:
            run = json.loads(line)
        except ValueError:
            continue
        if isinstance(run, dict):
            runs.append(run)
    return runs


def _human_size(size: int) -> str:
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"


def runs_folder_bytes(layout: Layout) -> int:
    if not layout.runs_dir.is_dir():
        return 0
    return sum(
        path.stat().st_size for path in layout.runs_dir.iterdir() if path.is_file()
    )


def status_lines(
    layout: Layout, github_get: GitHubGet = github_get_through_latchkey
) -> list[str]:
    if not layout.source_path.is_file():
        return ["No collector fetched yet"]
    source = _read_source(layout)
    lines = [
        f"Collector: {source.get('repo', COLLECTOR_REPO)}@{source['commit_sha'][:10]} (fetched {source.get('fetched_at', '?')})",
        _upstream_line(layout, source, github_get),
    ]
    runs = _read_index(layout)
    succeeded = sum(1 for run in runs if run.get("payload_file"))
    lines.append(f"Runs: {len(runs)} ({succeeded} ok, {len(runs) - succeeded} failed)")
    for run in runs[-5:]:
        when = run.get("started_at") or run.get("recovered_at") or "?"
        if run.get("payload_file"):
            detail = str(run.get("record_count_by_source", {}))
            if run.get("error_by_source"):
                detail += f" feed errors: {run['error_by_source']}"
        else:
            detail = f"failed: {run.get('error', 'unknown error')}"
        lines.append(f"  {when} exit={run.get('exit_code')} {detail}")
    size = runs_folder_bytes(layout)
    lines.append(f"Runs folder: {_human_size(size)} ({size} bytes)")
    if size > RUNS_SIZE_WARNING_BYTES:
        lines.append(
            f"Warning: the runs folder is over {_human_size(RUNS_SIZE_WARNING_BYTES)}. "
            "Nothing is deleted automatically; decide whether to keep all of it."
        )
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subcommands.add_parser(
        "fetch", help="Download the collector from mngr-internal"
    )
    fetch_parser.add_argument(
        "--ref",
        default=UPSTREAM_BRANCH,
        help="Branch, tag or commit to fetch (default: main)",
    )
    run_parser = subcommands.add_parser(
        "run", help="Run one collection and keep its payload"
    )
    run_parser.add_argument(
        "--budget-mib",
        type=int,
        default=DEFAULT_BUDGET_MIB,
        help="Input budget for this run",
    )
    subcommands.add_parser(
        "status",
        help="Show the collector version, whether main has a newer one, and recent runs",
    )
    args = parser.parse_args(argv)
    layout = default_layout()
    try:
        if args.command == "fetch":
            command_fetch(layout, args.ref)
        elif args.command == "run":
            return command_run(layout, args.budget_mib)
        else:
            print("\n".join(status_lines(layout)))
    except LocalAnalyticsCopyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
