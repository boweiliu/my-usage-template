"""The page in a real browser: filters, tooltips, tables, sorting, the raw panel and the empty states.

Each test serves the real app on a spare port over a small synthetic copy whose days sit just
before today, so the "Last 7 days" filter has something to cut.
"""

import threading
from collections.abc import Generator
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from my_usage import runner
from my_usage.testing import gateway_request_count
from my_usage.testing import index_interrupted_run
from my_usage.testing import index_run
from my_usage.testing import message
from my_usage.testing import step
from my_usage.testing import window_opened
from my_usage.testing import workspace_state
from my_usage.testing import write_chat_name
from my_usage.testing import write_run
from my_usage.usage_data import UsageStore

pytestmark = pytest.mark.browser

FORTRESS = "/opt/fortress/tilion-fortress/tilion"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
FIRST_DAY_BACK = 25
EXTRA_CHATS = 12


def _day(days_back: int) -> datetime:
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days_back)


def _at(days_back: int, minute: int = 0) -> str:
    return (_day(days_back) + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(days_back: int) -> str:
    day = _day(days_back)
    return f"{MONTHS[day.month - 1]} {day.day}"


def _write_busy_copy(copy_dir: Path, host_dir: Path, has_app_events: bool = True) -> None:
    """All time: 7 messages, 14 claude-opus-5-5 and 5 claude-sonnet-5 steps, 15 chats.
    Last 7 days: 3 messages, 5 sonnet steps, 2 chats."""
    write_run(
        copy_dir,
        "run-a",
        [
            *(message(f"a-m{n}", "chat-a", _at(20, n)) for n in range(3)),
            step("a-s1", "chat-a", _at(20, 10), "claude-opus-5-5"),
            step("a-s2", "chat-a", _at(20, 11), "claude-opus-5-5"),
            message("m-only", "chat-m", _at(20, 20)),
            *(step(f"x-s{n}", f"extra-{n:02d}", _at(FIRST_DAY_BACK, n), "claude-opus-5-5") for n in range(EXTRA_CHATS)),
        ],
        finished_at=_at(2),
    )
    app_events = (
        [
            window_opened("w1", "files", _at(3)),
            window_opened("w2", "files", _at(3, 1)),
            gateway_request_count("g1", "files", "owner", "allowed", 3),
            gateway_request_count("g2", "files", "user", "denied_not_shared", 2),
        ]
        if has_app_events
        else []
    )
    write_run(
        copy_dir,
        "run-b",
        [
            *(message(f"b-m{n}", "chat-b", _at(3, n)) for n in range(2)),
            *(step(f"b-s{n}", "chat-b", _at(3, 10 + n), "claude-sonnet-5") for n in range(4)),
            message("a-m9", "chat-a", _at(1)),
            step("a-s3", "chat-a", _at(1, 5), "claude-sonnet-5"),
            workspace_state("ws", _at(1), ["chat", "files", "my-usage"]),
            *app_events,
        ],
        finished_at=_at(1),
        error_by_source={"servers": "timed out"},
    )
    # A failed and an interrupted run, newer than both: neither is a collection.
    index_run(copy_dir, "run-failed", _at(0), exit_code=1)
    index_interrupted_run(copy_dir, "run-killed", recovered_at=_at(0, 5))
    write_chat_name(host_dir, "chat-a", "Planning chat")
    write_chat_name(host_dir, "chat-b", "Release notes")


@contextmanager
def _serve(copy_dir: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    monkeypatch.setattr(runner, "STORE", UsageStore(copy_dir=copy_dir, host_dir=host_dir, timezone_name="UTC"))
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(executable_path=FORTRESS, headless=True)
        yield launched
        launched.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1100, "height": 900})
    yield context.new_page()
    context.close()


@pytest.fixture
def busy_page(page: Page, copy_dir: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Page]:
    _write_busy_copy(copy_dir, host_dir)
    with _serve(copy_dir, host_dir, monkeypatch) as url:
        page.goto(url)
        expect(page.locator("#fresh")).to_contain_text("Last collected")
        yield page


def _tiles(page: Page) -> list[str]:
    return page.locator("#tiles .tile .v").all_inner_texts()


def _chat_names(page: Page) -> list[str]:
    return page.locator("#chats tr td.name").all_inner_texts()


def test_the_page_shows_all_collected_activity(busy_page: Page) -> None:
    expect(busy_page.locator("#heroN")).to_have_text("7")
    assert _tiles(busy_page) == ["19", f"4 of {FIRST_DAY_BACK + 1}", "15"]
    expect(busy_page.locator("#fresh")).to_contain_text(f"Last collected {_label(1)}, 12:00 PM · 2 collections")
    expect(busy_page.locator("#fresh")).to_contain_text("31 records")
    expect(busy_page.locator("#fresh")).to_contain_text("1 feed errors")
    expect(busy_page.locator("#models .barrow")).to_have_count(2)
    assert busy_page.locator("#models .barrow .tag").all_inner_texts() == ["claude-opus-5-5", "claude-sonnet-5"]
    assert busy_page.locator("#models .barrow .barval").all_inner_texts() == ["14", "5"]
    expect(busy_page.locator("#empty")).to_be_hidden()


def test_the_range_filter_scopes_every_number(busy_page: Page) -> None:
    busy_page.locator("#range button[data-days='7']").click()

    expect(busy_page.locator("#heroN")).to_have_text("3")
    assert _tiles(busy_page) == ["5", "2 of 7", "2"]
    assert sorted(_chat_names(busy_page)) == ["Planning chat", "Release notes"]
    expect(busy_page.locator("#range button[data-days='7']")).to_have_attribute("aria-pressed", "true")
    expect(busy_page.locator("#range button[data-days='0']")).to_have_attribute("aria-pressed", "false")
    assert busy_page.locator("#models .barrow .tag").all_inner_texts() == ["claude-sonnet-5"]
    expect(busy_page.locator("#chartA rect.hit")).to_have_count(7)

    busy_page.locator("#range button[data-days='0']").click()
    expect(busy_page.locator("#heroN")).to_have_text("7")


def test_the_model_filter_scopes_steps_and_chats_but_not_messages(busy_page: Page) -> None:
    expect(busy_page.locator("#heroNote")).to_be_hidden()

    busy_page.locator("#model").select_option("claude-sonnet-5")

    expect(busy_page.locator("#heroN")).to_have_text("7")
    expect(busy_page.locator("#heroNote")).to_be_visible()
    assert _tiles(busy_page) == ["5", f"2 of {FIRST_DAY_BACK + 1}", "2"]
    rows = {
        name: steps
        for name, steps in zip(_chat_names(busy_page), busy_page.locator("#chats tr td.n:nth-child(4)").all_inner_texts())
    }
    assert rows == {"Release notes": "4", "Planning chat": "1"}


def test_hovering_a_day_shows_its_count(busy_page: Page) -> None:
    day_index = FIRST_DAY_BACK - 20

    busy_page.locator("#chartA rect.hit").nth(day_index).hover()

    tip = busy_page.locator("#tip")
    expect(tip).to_be_visible()
    expect(tip.locator("b")).to_have_text("4")
    expect(tip.locator("span")).to_have_text(f"messages on {_label(20)}")
    busy_page.mouse.move(5, 5)
    expect(tip).to_be_hidden()


def test_chart_gridlines_are_labelled_with_their_values(busy_page: Page) -> None:
    # Messages peak at 4, so the scale runs to 5 and the middle line sits at 2.5; steps peak at 12.
    assert busy_page.locator("#chartA svg > text[text-anchor='end']:not(.peak)").all_text_contents() == ["0", "2.5", "5"]
    assert busy_page.locator("#chartB svg > text[text-anchor='end']:not(.peak)").all_text_contents() == ["0", "10", "20"]


def test_a_chart_switches_to_a_table_of_its_days(busy_page: Page) -> None:
    toggle = busy_page.locator("button[data-table-for='b']")

    toggle.click()

    expect(busy_page.locator("#tableB")).to_be_visible()
    expect(busy_page.locator("#chartB")).to_be_hidden()
    expect(toggle).to_have_text("Chart")
    rows = busy_page.locator("#tableB tbody tr")
    expect(rows).to_have_count(FIRST_DAY_BACK + 1)
    expect(rows.nth(FIRST_DAY_BACK)).to_have_text(f"{_label(0)}0")
    expect(rows.nth(FIRST_DAY_BACK - 3)).to_have_text(f"{_label(3)}4")
    toggle.click()
    expect(busy_page.locator("#chartB")).to_be_visible()


def test_chats_sort_and_expand_beyond_the_top_ten(busy_page: Page) -> None:
    expect(busy_page.locator("#chats tr")).to_have_count(10)
    more = busy_page.locator("#moreChats")
    expect(more).to_have_text("Show all 15 chats")
    # Most messages first by default.
    assert _chat_names(busy_page)[0] == "Planning chat"

    more.click()
    expect(busy_page.locator("#chats tr")).to_have_count(15)
    busy_page.locator("th button[data-sort='name']").click()
    names = _chat_names(busy_page)
    assert names == sorted(names, key=str.lower, reverse=True)
    busy_page.locator("th button[data-sort='name']").click()
    names = _chat_names(busy_page)
    assert names == sorted(names, key=str.lower)


def test_view_raw_shows_a_chats_original_records(busy_page: Page) -> None:
    busy_page.locator("#chats tr[data-chat='chat-a'] [data-raw]").click()

    panel = busy_page.locator("#raw")
    expect(panel).to_be_visible()
    expect(busy_page.locator("#rawSrc")).to_have_text(
        "The newest 3 collected records of Planning chat, exactly as stored. Each shows the file it is in."
    )
    records = busy_page.locator("#rawBody pre")
    expect(records).to_have_count(3)
    expect(records.nth(0)).to_contain_text('"event_id": "a-s3"')
    expect(records.nth(2)).to_contain_text('"event_id": "a-s1"')
    sources = busy_page.locator("#rawBody p.src").all_inner_texts()
    assert sources[0].startswith("data/.skills/local-analytics-copy/runs/run-b.jsonl, byte ")
    assert sources[2].startswith("data/.skills/local-analytics-copy/runs/run-a.jsonl, byte ")

    busy_page.keyboard.press("Escape")
    expect(panel).to_be_hidden()


def test_view_raw_of_a_chat_with_no_stored_steps_says_so(busy_page: Page) -> None:
    busy_page.locator("#moreChats").click()
    busy_page.locator("#chats tr[data-chat='chat-m'] [data-raw]").click()

    expect(busy_page.locator("#rawSrc")).to_have_text("No original records found for this chat.")
    expect(busy_page.locator("#rawBody pre")).to_have_count(0)
    busy_page.locator("#rawClose").click()
    expect(busy_page.locator("#raw")).to_be_hidden()


def test_the_apps_panel_lists_opens_and_requests(busy_page: Page) -> None:
    rows = busy_page.locator("#apps tbody tr")
    expect(rows).to_have_count(1)
    assert rows.nth(0).locator("td").all_inner_texts() == ["files", "2", "3", "0", "0", "2"]
    assert busy_page.locator("#apps .chip").all_inner_texts() == ["chat", "files", "my-usage"]


def test_the_apps_panel_explains_when_no_app_events_exist(
    page: Page, copy_dir: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_busy_copy(copy_dir, host_dir, has_app_events=False)
    with _serve(copy_dir, host_dir, monkeypatch) as url:
        page.goto(url)

        expect(page.locator("#apps")).to_contain_text("App opens and visits are not recorded in this workspace yet.")
        expect(page.locator("#apps table")).to_have_count(0)
        assert page.locator("#apps .chip").all_inner_texts() == ["chat", "files", "my-usage"]


def test_an_empty_copy_shows_the_empty_state(
    page: Page, copy_dir: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _serve(copy_dir, host_dir, monkeypatch) as url:
        page.goto(url)

        expect(page.locator("#fresh")).to_have_text("Nothing collected yet. Run the local analytics copy to fill this page.")
        expect(page.locator("#empty")).to_be_visible()
        expect(page.locator("#content")).to_be_hidden()
