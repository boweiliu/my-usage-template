"""Turns the local analytics copy into the numbers the page shows.

The copy (the ``local-analytics-copy`` skill) keeps each collection's output, unchanged, as
``<copy dir>/runs/<run id>.jsonl``: one ``{"source", "record"}`` line per record, then a
``run_summary`` line. This module only reads those files. Each file is read once, in order, and is
never written; the page's numbers are rebuilt from them whenever the app starts, so nothing about
them is stored here. A record is counted once however many files carry it (by its ``event_id``).

What is counted:

- **Messages you sent**: ``client_activity`` ``message`` events (a message sent from one of the
  workspace's pages), per day and per chat. The text was already dropped by the collector.
- **Agent steps**: ``transcripts`` steps whose source is ``agent``, per day, model and chat.
- **Apps**: the latest ``workspace_state`` snapshot's installed apps; ``window_opened`` events from
  ``client_activity`` and ``gateway_request_count`` records from ``gateway_requests`` (both only
  exist once the workspace has the app-usage update) per app.
"""

import json
import re
import threading
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from datetime import tzinfo
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field
from pydantic import JsonValue
from pydantic import PrivateAttr

RAW_RECORDS_PER_CHAT: Final[int] = 5
UNKNOWN_MODEL: Final[str] = "unknown"
_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$"
)
# A chat id safe to use as one folder name: no separators, and not "." or "..".
_CHAT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?!\.+$)[A-Za-z0-9_.-]{1,120}$")
_REQUESTER_BUCKETS: Final[dict[str, str]] = {"owner": "owner", "user": "visitor", "agent": "agent"}


class CollectionInfo(FrozenModel):
    """What the copy has collected so far."""

    run_count: int = Field(description="How many collections finished")
    last_collected_at: str = Field(description="When the last finished collection ended (UTC, ISO); empty if none")
    record_count: int = Field(description="How many records the collections hold")
    feed_error_count: int = Field(description="How many data feeds reported an error across all collections")


class AppRequestRow(FrozenModel):
    """The requests one app's address received at the sharing gateway."""

    service: str = Field(description="The app")
    owner: int = Field(description="Requests made by the workspace owner")
    visitor: int = Field(description="Requests made by people the workspace is shared with")
    agent: int = Field(description="Requests made by agents")
    denied: int = Field(description="Requests the gateway refused")


class AppsInfo(FrozenModel):
    """What the copy shows about the workspace's apps."""

    installed: list[str] = Field(description="The apps installed, from the latest workspace snapshot")
    window_opens: dict[str, int] = Field(description="Window opens per app")
    requests: list[AppRequestRow] = Field(description="Gateway request counts per app")
    has_events: bool = Field(description="Whether any window opens or request counts have been collected")


class ChatRow(FrozenModel):
    """One chat's activity."""

    id: str = Field(description="The chat's id")
    name: str = Field(description="The chat's name")
    harness: str = Field(description="Which agent harness runs it (claude, codex, ...)")
    last_active: str = Field(description="When it last did anything (UTC, ISO); empty if never")
    messages_by_day: list[int] = Field(description="Messages you sent to it, one number per day of the page's range")
    steps_by_model_day: dict[str, list[int]] = Field(description="Agent steps by model, one number per day")


class UsageResponse(FrozenModel):
    """Everything the page draws, for all collected days; the page slices it by the filters."""

    timezone: str = Field(description="The time zone days are counted in")
    days: list[str] = Field(description="Every day from the first activity to today (ISO dates)")
    messages: list[int] = Field(description="Messages you sent per day")
    steps_by_model: dict[str, list[int]] = Field(description="Agent steps per day, by model")
    chats: list[ChatRow] = Field(description="Every chat with activity")
    apps: AppsInfo = Field(description="The workspace's apps")
    collection: CollectionInfo = Field(description="What has been collected")


class RawRecord(FrozenModel):
    """One collected record, exactly as the copy holds it, and where it is."""

    run_id: str = Field(description="The collection it came from")
    file: str = Field(description="The file holding it, relative to the copy's folder")
    offset: int = Field(description="Where in that file the record's line starts, in bytes")
    record: dict[str, JsonValue] = Field(description="The record itself")


class RawResponse(FrozenModel):
    """The latest collected records of one chat."""

    chat_id: str = Field(description="The chat's id")
    name: str = Field(description="The chat's name")
    records: list[RawRecord] = Field(description="Its newest records, newest first")


class RawRef(FrozenModel):
    """Where one record sits in the copy."""

    timestamp: str
    file: str
    offset: int


class ChatTally(MutableModel):
    """What has been counted for one chat so far."""

    harness: str = ""
    last_active: str = ""
    messages_by_day: dict[str, int] = Field(default_factory=dict)
    steps_by_model_day: dict[str, dict[str, int]] = Field(default_factory=dict)
    refs: list[RawRef] = Field(default_factory=list)


def parse_timestamp(value: str) -> datetime | None:
    """A record's timestamp (``...Z``, with up to nanosecond fractions) as an aware datetime, or None."""
    match = _TIMESTAMP_PATTERN.match(value.strip())
    if match is None:
        return None
    base, fraction, zone = match.groups()
    microseconds = (fraction or "")[:6].ljust(6, "0")
    offset = "+00:00" if zone in (None, "Z") else zone
    try:
        return datetime.fromisoformat(f"{base}.{microseconds}{offset}")
    except ValueError:
        return None


def load_timezone(name: str) -> tzinfo:
    """The named time zone, or UTC when it is missing or unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


class UsageStore(MutableModel):
    """Reads the copy's collections as they appear and answers the page's questions."""

    copy_dir: Path = Field(description="The local-analytics-copy folder (holds runs/ and runs.jsonl)")
    host_dir: Path = Field(description="The mngr host folder, where chat names are read from")
    timezone_name: str = Field(default="UTC", description="The time zone days are counted in")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _ingested_files: set[str] = PrivateAttr(default_factory=set)
    _seen_event_ids: set[str] = PrivateAttr(default_factory=set)
    _record_count: int = PrivateAttr(default=0)
    _messages_by_day: dict[str, int] = PrivateAttr(default_factory=dict)
    _steps_by_model_day: dict[str, dict[str, int]] = PrivateAttr(default_factory=dict)
    _chats: dict[str, ChatTally] = PrivateAttr(default_factory=dict)
    _window_opens: dict[str, int] = PrivateAttr(default_factory=dict)
    _requests: dict[str, dict[str, int]] = PrivateAttr(default_factory=dict)
    _installed_apps: list[str] = PrivateAttr(default_factory=list)
    _installed_apps_at: str = PrivateAttr(default="")
    _names: dict[str, str] = PrivateAttr(default_factory=dict)

    # ------------------------------------------------------------------ reading the copy

    def refresh(self) -> None:
        """Read any collection file not read yet (files are complete once named ``*.jsonl``)."""
        with self._lock:
            runs_dir = self.copy_dir / "runs"
            if not runs_dir.is_dir():
                return
            for path in sorted(runs_dir.glob("*.jsonl")):
                if path.name in self._ingested_files:
                    continue
                self._ingested_files.add(path.name)
                # Only a file inside runs/ is read: a link to anywhere else is not part of the copy.
                if not path.is_file() or not self._is_in_runs_dir(path):
                    continue
                try:
                    self._ingest_file(path)
                except OSError as error:
                    # Pruned by the copy between the listing and the read: it is no longer collected.
                    logger.warning("Could not read {}: {}", path.name, error)

    def _is_in_runs_dir(self, path: Path) -> bool:
        return path.resolve().is_relative_to((self.copy_dir / "runs").resolve())

    def _ingest_file(self, path: Path) -> None:
        offset = 0
        with path.open("rb") as handle:
            for raw_line in handle:
                line_offset = offset
                offset += len(raw_line)
                try:
                    parsed = json.loads(raw_line)
                except ValueError:
                    logger.warning("Skipping an unreadable line at byte {} of {}", line_offset, path.name)
                    continue
                if isinstance(parsed, dict):
                    self._ingest_line(parsed, f"runs/{path.name}", line_offset)

    def _ingest_line(self, line: dict[str, JsonValue], file: str, offset: int) -> None:
        source = line.get("source")
        record = line.get("record")
        if source == "run_summary" or not isinstance(record, dict):
            return
        self._record_count += 1
        if source == "transcripts":
            self._ingest_transcript(record, file, offset)
        elif source == "client_activity":
            self._ingest_client_activity(record)
        elif source == "gateway_requests":
            self._ingest_gateway_request(record)
        elif source == "workspace_state":
            self._ingest_workspace_state(record)

    def _is_new_event(self, record: dict[str, JsonValue]) -> bool:
        event_id = record.get("event_id")
        if not isinstance(event_id, str):
            return True
        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(event_id)
        return True

    def _day_of(self, timestamp: str) -> str | None:
        moment = parse_timestamp(timestamp)
        if moment is None:
            return None
        return moment.astimezone(load_timezone(self.timezone_name)).date().isoformat()

    def _ingest_transcript(self, record: dict[str, JsonValue], file: str, offset: int) -> None:
        agent_id = record.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return
        chat = self._chats.setdefault(agent_id, ChatTally())
        emitter = record.get("emitter")
        if isinstance(emitter, str) and not chat.harness:
            chat.harness = emitter.split("/")[0]
        timestamp = record.get("timestamp")
        if record.get("type") != "step" or not isinstance(timestamp, str):
            return
        if not self._is_new_event(record):
            return
        if timestamp > chat.last_active:
            chat.last_active = timestamp
        chat.refs.append(RawRef(timestamp=timestamp, file=file, offset=offset))
        chat.refs.sort(key=lambda ref: ref.timestamp, reverse=True)
        del chat.refs[RAW_RECORDS_PER_CHAT:]
        day = self._day_of(timestamp)
        if record.get("source") != "agent" or day is None:
            return
        model = record.get("model_name")
        model_name = model if isinstance(model, str) and model else UNKNOWN_MODEL
        per_day = self._steps_by_model_day.setdefault(model_name, {})
        per_day[day] = per_day.get(day, 0) + 1
        chat_per_day = chat.steps_by_model_day.setdefault(model_name, {})
        chat_per_day[day] = chat_per_day.get(day, 0) + 1

    def _ingest_client_activity(self, record: dict[str, JsonValue]) -> None:
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str) or not self._is_new_event(record):
            return
        if record.get("type") == "window_opened":
            app = record.get("app")
            if isinstance(app, str) and app:
                self._window_opens[app] = self._window_opens.get(app, 0) + 1
            return
        if record.get("type") != "message":
            return
        day = self._day_of(timestamp)
        if day is None:
            return
        self._messages_by_day[day] = self._messages_by_day.get(day, 0) + 1
        key = record.get("key")
        if isinstance(key, str) and key:
            chat = self._chats.setdefault(key, ChatTally())
            chat.messages_by_day[day] = chat.messages_by_day.get(day, 0) + 1
            if timestamp > chat.last_active:
                chat.last_active = timestamp

    def _ingest_gateway_request(self, record: dict[str, JsonValue]) -> None:
        if record.get("type") != "gateway_request_count" or not self._is_new_event(record):
            return
        service = record.get("service")
        count = record.get("request_count")
        if not isinstance(service, str) or not service or not isinstance(count, int):
            return
        outcome = record.get("outcome")
        kind = record.get("requester_kind")
        if isinstance(outcome, str) and outcome.startswith("denied"):
            bucket = "denied"
        else:
            bucket = _REQUESTER_BUCKETS.get(kind, "other") if isinstance(kind, str) else "other"
        per_service = self._requests.setdefault(service, {})
        per_service[bucket] = per_service.get(bucket, 0) + count

    def _ingest_workspace_state(self, record: dict[str, JsonValue]) -> None:
        timestamp = record.get("timestamp")
        apps = record.get("installed_app_names")
        if not isinstance(timestamp, str) or not isinstance(apps, list) or timestamp < self._installed_apps_at:
            return
        self._installed_apps_at = timestamp
        self._installed_apps = sorted(app for app in apps if isinstance(app, str))

    # ------------------------------------------------------------------ answering the page

    def usage(self, today: date | None = None) -> UsageResponse:
        """All collected numbers, per day from the first activity to ``today`` (default: today in the zone)."""
        self.refresh()
        with self._lock:
            zone = load_timezone(self.timezone_name)
            last_day = today or datetime.now(zone).date()
            active_days = list(self._messages_by_day)
            for per_day in self._steps_by_model_day.values():
                active_days.extend(per_day)
            if not active_days:
                days: list[str] = []
            else:
                first_day = date.fromisoformat(min(active_days))
                last_day = max(last_day, date.fromisoformat(max(active_days)))
                days = [(first_day + timedelta(days=n)).isoformat() for n in range((last_day - first_day).days + 1)]
            chats = [self._chat_row(chat_id, tally, days) for chat_id, tally in self._chats.items() if self._has_activity(tally)]
            chats.sort(key=lambda row: row.last_active, reverse=True)
            return UsageResponse(
                timezone=self.timezone_name,
                days=days,
                messages=[self._messages_by_day.get(day, 0) for day in days],
                steps_by_model={
                    model: [per_day.get(day, 0) for day in days]
                    for model, per_day in sorted(self._steps_by_model_day.items())
                },
                chats=chats,
                apps=self._apps_info(),
                collection=self._collection_info(),
            )

    @staticmethod
    def _has_activity(tally: ChatTally) -> bool:
        return bool(tally.messages_by_day or tally.steps_by_model_day)

    def _chat_row(self, chat_id: str, tally: ChatTally, days: list[str]) -> ChatRow:
        return ChatRow(
            id=chat_id,
            name=self._chat_name(chat_id),
            harness=tally.harness,
            last_active=tally.last_active,
            messages_by_day=[tally.messages_by_day.get(day, 0) for day in days],
            steps_by_model_day={
                model: [per_day.get(day, 0) for day in days] for model, per_day in sorted(tally.steps_by_model_day.items())
            },
        )

    def _chat_name(self, chat_id: str) -> str:
        if chat_id in self._names:
            return self._names[chat_id]
        name = self._read_chat_name(chat_id)
        self._names[chat_id] = name
        return name

    def _read_chat_name(self, chat_id: str) -> str:
        if not _CHAT_ID_PATTERN.match(chat_id):
            return chat_id
        data_file = self.host_dir / "agents" / chat_id / "data.json"
        try:
            name = json.loads(data_file.read_text()).get("name")
        except (OSError, ValueError, AttributeError):
            name = None
        if isinstance(name, str) and name:
            return name
        preserved = self.host_dir / "preserved"
        if preserved.is_dir():
            for entry in preserved.glob(f"*--{chat_id}"):
                return entry.name.removesuffix(f"--{chat_id}")
        return chat_id

    def _apps_info(self) -> AppsInfo:
        requests = [
            AppRequestRow(
                service=service,
                owner=buckets.get("owner", 0),
                visitor=buckets.get("visitor", 0),
                agent=buckets.get("agent", 0),
                denied=buckets.get("denied", 0),
            )
            for service, buckets in sorted(self._requests.items())
        ]
        return AppsInfo(
            installed=list(self._installed_apps),
            window_opens=dict(sorted(self._window_opens.items())),
            requests=requests,
            has_events=bool(self._window_opens or self._requests),
        )

    def _collection_info(self) -> CollectionInfo:
        index_path = self.copy_dir / "runs.jsonl"
        run_count = 0
        last_collected_at = ""
        feed_error_count = 0
        if index_path.is_file():
            for line in index_path.read_text().splitlines():
                try:
                    run = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(run, dict) or run.get("exit_code") != 0 or not run.get("payload_file"):
                    continue
                run_count += 1
                finished = run.get("finished_at")
                if isinstance(finished, str) and finished > last_collected_at:
                    last_collected_at = finished
                errors = run.get("error_by_source")
                feed_error_count += len(errors) if isinstance(errors, dict) else 0
        return CollectionInfo(
            run_count=run_count,
            last_collected_at=last_collected_at,
            record_count=self._record_count,
            feed_error_count=feed_error_count,
        )

    def raw_records(self, chat_id: str) -> RawResponse | None:
        """The chat's newest collected records as stored, or None when the chat is unknown."""
        self.refresh()
        with self._lock:
            tally = self._chats.get(chat_id)
            if tally is None:
                return None
            records = [raw for ref in tally.refs if (raw := self._read_raw(ref)) is not None]
            return RawResponse(chat_id=chat_id, name=self._chat_name(chat_id), records=records)

    def _read_raw(self, ref: RawRef) -> RawRecord | None:
        path = self.copy_dir / ref.file
        if not self._is_in_runs_dir(path):
            return None
        try:
            with path.open("rb") as handle:
                handle.seek(ref.offset)
                parsed = json.loads(handle.readline())
        except (OSError, ValueError):
            return None
        record = parsed.get("record") if isinstance(parsed, dict) else None
        if not isinstance(record, dict):
            return None
        return RawRecord(run_id=path.stem, file=ref.file, offset=ref.offset, record=record)
