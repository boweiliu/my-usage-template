import json
import os
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path

from my_usage.testing import index_interrupted_run
from my_usage.testing import index_run
from my_usage.testing import message
from my_usage.testing import step
from my_usage.testing import write_chat_name
from my_usage.testing import write_run
from my_usage.usage_data import RAW_RECORDS_PER_CHAT
from my_usage.usage_data import RawRef
from my_usage.usage_data import UsageStore
from my_usage.usage_data import parse_timestamp

TODAY = date(2026, 10, 4)


def _store(copy_dir: Path, host_dir: Path, timezone_name: str = "UTC") -> UsageStore:
    return UsageStore(copy_dir=copy_dir, host_dir=host_dir, timezone_name=timezone_name)


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {str(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in root.rglob("*")}


# ---------------------------------------------------------------- the numbers


def test_usage_counts_each_record_once_across_overlapping_collections(standard_copy: Path, host_dir: Path) -> None:
    usage = _store(standard_copy, host_dir).usage(today=TODAY)

    assert usage.model_dump() == {
        "timezone": "UTC",
        "days": ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"],
        "messages": [1, 0, 1, 0],
        "steps_by_model": {
            "claude-opus-5-5": [1, 0, 0, 0],
            "claude-sonnet-5": [0, 1, 0, 0],
            "unknown": [0, 0, 1, 0],
        },
        "chats": [
            {
                "id": "chat-c",
                "name": "chat-c",
                "harness": "codex",
                "last_active": "2026-10-03T12:00:00Z",
                "messages_by_day": [0, 0, 0, 0],
                "steps_by_model_day": {"unknown": [0, 0, 1, 0]},
            },
            {
                "id": "chat-b",
                "name": "Old chat",
                "harness": "",
                "last_active": "2026-10-03T09:00:00Z",
                "messages_by_day": [0, 0, 1, 0],
                "steps_by_model_day": {},
            },
            {
                "id": "chat-a",
                "name": "Planning chat",
                "harness": "claude",
                "last_active": "2026-10-02T10:01:00Z",
                "messages_by_day": [1, 0, 0, 0],
                "steps_by_model_day": {"claude-opus-5-5": [1, 0, 0, 0], "claude-sonnet-5": [0, 1, 0, 0]},
            },
        ],
        "apps": {
            "installed": ["chat", "files", "my-usage"],
            "window_opens": {"chat": 1, "files": 2},
            "requests": [
                {"service": "chat", "owner": 5, "visitor": 0, "agent": 0, "denied": 0},
                {"service": "files", "owner": 3, "visitor": 2, "agent": 1, "denied": 4},
            ],
            "has_events": True,
        },
        "collection": {
            "run_count": 2,
            "last_collected_at": "2026-10-04T12:00:00Z",
            "record_count": 20,
            "feed_error_count": 1,
        },
    }


def test_days_follow_the_workspace_time_zone(copy_dir: Path, host_dir: Path) -> None:
    write_run(
        copy_dir,
        "run-a",
        [
            # 23:30 on Oct 1 in Los Angeles, and 00:30 on Oct 2: the same UTC day.
            message("m1", "chat-a", "2026-10-02T06:30:00Z"),
            step("s1", "chat-a", "2026-10-02T07:30:00.123456789Z"),
        ],
    )

    local = _store(copy_dir, host_dir, "America/Los_Angeles").usage(today=date(2026, 10, 2))
    assert (local.timezone, local.days, local.messages, local.steps_by_model) == (
        "America/Los_Angeles",
        ["2026-10-01", "2026-10-02"],
        [1, 0],
        {"claude-opus-5-5": [0, 1]},
    )
    utc = _store(copy_dir, host_dir, "UTC").usage(today=date(2026, 10, 2))
    assert (utc.days, utc.messages, utc.steps_by_model) == (["2026-10-02"], [1], {"claude-opus-5-5": [1]})


def test_an_unknown_time_zone_counts_in_utc(copy_dir: Path, host_dir: Path) -> None:
    write_run(copy_dir, "run-a", [message("m1", "chat-a", "2026-10-02T06:30:00Z")])

    usage = _store(copy_dir, host_dir, "Not/AZone").usage(today=date(2026, 10, 2))

    assert (usage.days, usage.messages) == (["2026-10-02"], [1])


def test_the_range_runs_to_today_even_after_the_last_activity(copy_dir: Path, host_dir: Path) -> None:
    write_run(copy_dir, "run-a", [message("m1", "chat-a", "2026-10-01T10:00:00Z")])

    usage = _store(copy_dir, host_dir).usage(today=date(2026, 10, 3))

    assert (usage.days, usage.messages) == (["2026-10-01", "2026-10-02", "2026-10-03"], [1, 0, 0])
    assert usage.chats[0].messages_by_day == [1, 0, 0]


def test_records_without_a_usable_timestamp_are_not_counted(copy_dir: Path, host_dir: Path) -> None:
    _, bad_message = message("m1", "chat-a", "yesterday")
    _, no_agent = step("s1", "", "2026-10-01T10:00:00Z")
    write_run(copy_dir, "run-a", [("client_activity", bad_message), ("transcripts", no_agent), "[1, 2]", ""])

    usage = _store(copy_dir, host_dir).usage(today=TODAY)

    assert (usage.days, usage.messages, usage.steps_by_model, usage.chats) == ([], [], {}, [])
    # Both records were collected, even though neither counts on the page.
    assert usage.collection.record_count == 2


def test_parse_timestamp_reads_the_collectors_formats() -> None:
    assert parse_timestamp("2026-10-02T07:30:00.123456789Z") == datetime(2026, 10, 2, 7, 30, 0, 123456, timezone.utc)
    assert parse_timestamp("2026-09-16T14:08:08-07:00") == datetime(2026, 9, 16, 21, 8, 8, tzinfo=timezone.utc)
    assert parse_timestamp("2026-09-16T14:08:08") == datetime(2026, 9, 16, 14, 8, 8, tzinfo=timezone.utc)
    assert parse_timestamp("2026-13-40T99:00:00Z") is None
    assert parse_timestamp("not a time") is None


# ---------------------------------------------------------------- an empty or missing copy


def test_a_missing_copy_answers_an_empty_page(copy_dir: Path, host_dir: Path) -> None:
    usage = _store(copy_dir, host_dir).usage(today=TODAY)

    assert usage.model_dump() == {
        "timezone": "UTC",
        "days": [],
        "messages": [],
        "steps_by_model": {},
        "chats": [],
        "apps": {"installed": [], "window_opens": {}, "requests": [], "has_events": False},
        "collection": {"run_count": 0, "last_collected_at": "", "record_count": 0, "feed_error_count": 0},
    }


def test_an_empty_copy_answers_an_empty_page(copy_dir: Path, host_dir: Path) -> None:
    (copy_dir / "runs").mkdir(parents=True)
    (copy_dir / "runs.jsonl").write_text("")

    usage = _store(copy_dir, host_dir).usage(today=TODAY)

    assert (usage.days, usage.chats, usage.collection.run_count, usage.apps.has_events) == ([], [], 0, False)


def test_apps_without_app_events_say_so(copy_dir: Path, host_dir: Path) -> None:
    write_run(copy_dir, "run-a", [message("m1", "chat-a", "2026-10-01T10:00:00Z")])

    apps = _store(copy_dir, host_dir).usage(today=TODAY).apps

    assert apps.model_dump() == {"installed": [], "window_opens": {}, "requests": [], "has_events": False}


# ---------------------------------------------------------------- the collection status


def test_failed_and_interrupted_runs_are_not_collections(copy_dir: Path, host_dir: Path) -> None:
    write_run(copy_dir, "run-ok", [message("m1", "chat-a", "2026-10-01T10:00:00Z")], finished_at="2026-10-02T00:00:00Z", error_by_source={"servers": "timed out", "services": "bad cursor"})
    # A collector that exited non-zero, one that could not start, and one killed part-way: none has
    # a payload, and the newest of them must not count as "last collected".
    index_run(copy_dir, "run-exit-1", "2026-10-03T00:00:00Z", exit_code=1)
    index_run(copy_dir, "run-no-start", "2026-10-03T01:00:00Z", exit_code=None)
    index_interrupted_run(copy_dir, "run-killed", recovered_at="2026-10-04T00:00:00Z")
    # Exited 0 but its output had no run_summary line: kept as .failed, with no payload.
    with (copy_dir / "runs.jsonl").open("a") as handle:
        no_summary = {"run_id": "run-no-summary", "finished_at": "2026-10-04T01:00:00Z", "exit_code": 0, "payload_file": None, "failed_file": "runs/run-no-summary.failed", "error": "the collector's output did not end with a run_summary line"}
        handle.write(json.dumps(no_summary) + "\n")
    write_run(copy_dir, "run-killed", [message("m2", "chat-a", "2026-10-01T11:00:00Z")], suffix=".failed", is_indexed=False)

    usage = _store(copy_dir, host_dir).usage(today=TODAY)

    assert usage.collection.model_dump() == {
        "run_count": 1,
        "last_collected_at": "2026-10-02T00:00:00Z",
        "record_count": 1,
        "feed_error_count": 2,
    }
    assert sum(usage.messages) == 1


def test_only_failed_runs_mean_nothing_collected(copy_dir: Path, host_dir: Path) -> None:
    index_run(copy_dir, "run-exit-1", "2026-10-03T00:00:00Z", exit_code=1)
    index_interrupted_run(copy_dir, "run-killed", recovered_at="2026-10-04T00:00:00Z")

    collection = _store(copy_dir, host_dir).usage(today=TODAY).collection

    assert collection.model_dump() == {"run_count": 0, "last_collected_at": "", "record_count": 0, "feed_error_count": 0}


# ---------------------------------------------------------------- reading the copy


def test_a_collection_that_appears_later_is_read_on_the_next_request(copy_dir: Path, host_dir: Path) -> None:
    first = write_run(copy_dir, "run-a", [message("m1", "chat-a", "2026-10-01T10:00:00Z")])
    store = _store(copy_dir, host_dir)
    assert store.usage(today=TODAY).messages == [1, 0, 0, 0]

    write_run(copy_dir, "run-b", [message("m1", "chat-a", "2026-10-01T10:00:00Z"), message("m2", "chat-a", "2026-10-03T10:00:00Z")])
    # A file already read is not read again.
    with first.open("a") as handle:
        handle.write(json.dumps({"source": "client_activity", "record": message("m3", "chat-a", "2026-10-02T10:00:00Z")[1]}) + "\n")

    usage = store.usage(today=TODAY)
    assert usage.messages == [1, 0, 1, 0]
    assert (usage.collection.run_count, usage.collection.record_count) == (2, 3)


def test_reading_never_changes_the_copy(standard_copy: Path, host_dir: Path) -> None:
    before = _tree_state(standard_copy)
    store = _store(standard_copy, host_dir)

    store.usage(today=TODAY)
    store.raw_records("chat-a")

    assert _tree_state(standard_copy) == before


def test_a_collection_linked_from_outside_the_copy_is_not_read(copy_dir: Path, host_dir: Path, tmp_path: Path) -> None:
    write_run(copy_dir, "run-a", [message("m1", "chat-a", "2026-10-01T10:00:00Z")])
    outside = write_run(tmp_path / "elsewhere", "secret", [step("s1", "chat-x", "2026-10-01T10:00:00Z")], is_indexed=False)
    os.symlink(outside, copy_dir / "runs" / "linked.jsonl")
    store = _store(copy_dir, host_dir)

    usage = store.usage(today=TODAY)

    assert [chat.id for chat in usage.chats] == ["chat-a"]
    assert usage.steps_by_model == {}
    assert store.raw_records("chat-x") is None


# ---------------------------------------------------------------- chat names


def test_a_chat_id_that_names_a_parent_folder_is_not_read_as_a_path(copy_dir: Path, host_dir: Path) -> None:
    (host_dir / "data.json").write_text(json.dumps({"name": "not a chat"}))
    (host_dir / "agents").mkdir()
    write_run(copy_dir, "run-a", [step("s1", "..", "2026-10-01T10:00:00Z"), step("s2", "../agents", "2026-10-01T11:00:00Z")])

    names = sorted(chat.name for chat in _store(copy_dir, host_dir).usage(today=TODAY).chats)

    assert names == ["..", "../agents"]


def test_a_chat_without_a_readable_name_shows_its_id(copy_dir: Path, host_dir: Path) -> None:
    (host_dir / "agents" / "chat-a").mkdir(parents=True)
    (host_dir / "agents" / "chat-a" / "data.json").write_text("{broken")
    write_chat_name(host_dir, "chat-b", "")
    write_run(copy_dir, "run-a", [message("m1", "chat-a", "2026-10-01T10:00:00Z"), message("m2", "chat-b", "2026-10-01T11:00:00Z")])

    names = sorted(chat.name for chat in _store(copy_dir, host_dir).usage(today=TODAY).chats)

    assert names == ["chat-a", "chat-b"]


# ---------------------------------------------------------------- the original records


def test_raw_records_are_the_chats_newest_steps_exactly_as_stored(standard_copy: Path, host_dir: Path) -> None:
    raw = _store(standard_copy, host_dir).raw_records("chat-a")

    assert raw is not None
    assert (raw.chat_id, raw.name) == ("chat-a", "Planning chat")
    assert [record.record["event_id"] for record in raw.records] == ["s3", "s2", "s1"]
    run_file = standard_copy / "runs" / "run-a.jsonl"
    for record in raw.records:
        assert (record.run_id, record.file) == ("run-a", "runs/run-a.jsonl")
        with run_file.open("rb") as handle:
            handle.seek(record.offset)
            assert json.loads(handle.readline()) == {"source": "transcripts", "record": record.record}


def test_raw_records_keep_only_the_newest_few(copy_dir: Path, host_dir: Path) -> None:
    steps = [step(f"s{n}", "chat-a", f"2026-10-01T1{n}:00:00Z") for n in range(RAW_RECORDS_PER_CHAT + 2)]
    write_run(copy_dir, "run-a", list(reversed(steps)))

    raw = _store(copy_dir, host_dir).raw_records("chat-a")

    assert raw is not None
    assert [record.record["event_id"] for record in raw.records] == ["s6", "s5", "s4", "s3", "s2"]


def test_raw_records_of_a_chat_not_in_the_copy_are_refused(standard_copy: Path, host_dir: Path) -> None:
    store = _store(standard_copy, host_dir)

    assert store.raw_records("chat-unknown") is None
    assert store.raw_records("..") is None


def test_a_chat_with_only_messages_has_no_original_records(standard_copy: Path, host_dir: Path) -> None:
    raw = _store(standard_copy, host_dir).raw_records("chat-b")

    assert raw is not None
    assert (raw.name, raw.records) == ("Old chat", [])


def test_a_record_reference_outside_the_runs_folder_is_never_read(copy_dir: Path, host_dir: Path) -> None:
    write_run(copy_dir, "run-a", [step("s1", "chat-a", "2026-10-01T10:00:00Z")])
    # A file beside runs/ holding a valid record line: reachable only by escaping runs/.
    (copy_dir / "outside.jsonl").write_text(json.dumps({"source": "transcripts", "record": {"secret": True}}) + "\n")
    store = _store(copy_dir, host_dir)
    store.usage(today=TODAY)

    for crafted in ("runs/../outside.jsonl", "../local-analytics-copy/outside.jsonl", str(copy_dir / "outside.jsonl")):
        assert store._read_raw(RawRef(timestamp="2026-10-01T10:00:00Z", file=crafted, offset=0)) is None
    assert store._read_raw(RawRef(timestamp="2026-10-01T10:00:00Z", file="runs/run-a.jsonl", offset=0)) is not None


def test_a_pruned_collection_drops_out_of_the_original_records(copy_dir: Path, host_dir: Path) -> None:
    run_file = write_run(copy_dir, "run-a", [step("s1", "chat-a", "2026-10-01T10:00:00Z")])
    store = _store(copy_dir, host_dir)
    store.usage(today=TODAY)
    run_file.unlink()

    raw = store.raw_records("chat-a")

    assert raw is not None
    assert raw.records == []
