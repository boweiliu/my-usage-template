from pathlib import Path

import pytest
from app_schema.testing import assert_api_schema_describes_app
from flask.testing import FlaskClient

from my_usage import runner
from my_usage.runner import app
from my_usage.usage_data import UsageStore


@pytest.fixture
def client(standard_copy: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    monkeypatch.setattr(runner, "STORE", UsageStore(copy_dir=standard_copy, host_dir=host_dir, timezone_name="UTC"))
    return app.test_client()


def test_the_app_describes_its_api_the_way_its_manifest_says() -> None:
    assert_api_schema_describes_app(app.test_client(), Path(__file__).resolve().parents[2] / "app.toml")


def test_usage_route_answers_the_copys_numbers(client: FlaskClient) -> None:
    response = client.get("/api/usage")

    assert response.status_code == 200
    body = response.get_json()
    # The range runs to today, so only the collected days are pinned here.
    assert body["days"][:4] == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    assert body["messages"][:4] == [1, 0, 1, 0]
    assert sum(body["messages"]) == 2
    assert {model: sum(values) for model, values in body["steps_by_model"].items()} == {
        "claude-opus-5-5": 1,
        "claude-sonnet-5": 1,
        "unknown": 1,
    }
    assert [chat["name"] for chat in body["chats"]] == ["chat-c", "Old chat", "Planning chat"]
    assert body["collection"] == {
        "run_count": 2,
        "last_collected_at": "2026-10-04T12:00:00Z",
        "record_count": 20,
        "feed_error_count": 1,
    }


def test_raw_route_answers_a_chats_original_records(client: FlaskClient) -> None:
    response = client.get("/api/chats/chat-a/raw")

    assert response.status_code == 200
    body = response.get_json()
    assert (body["chat_id"], body["name"]) == ("chat-a", "Planning chat")
    assert [record["record"]["event_id"] for record in body["records"]] == ["s3", "s2", "s1"]
    assert {record["file"] for record in body["records"]} == {"runs/run-a.jsonl"}


@pytest.mark.parametrize(
    "path",
    ["/api/chats/chat-unknown/raw", "/api/chats/../raw", "/api/chats/..%2F..%2Fetc/raw", "/api/chats/run-a.jsonl/raw"],
)
def test_raw_route_refuses_a_chat_not_in_the_copy(client: FlaskClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert b"records" not in response.data


def test_the_page_is_served_under_its_security_policy(client: FlaskClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    page = response.get_data(as_text=True)
    assert '<script src="/assets/app.js"></script>' in page
    assert "connectToShell" in page


def test_only_the_pages_own_script_is_served_from_assets(client: FlaskClient) -> None:
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/assets/..%2Frunner.py").status_code == 404


def test_the_only_data_routes_are_the_two_read_only_ones() -> None:
    data_routes = {
        (method, str(rule))
        for rule in app.url_map.iter_rules()
        for method in rule.methods or set()
        if method not in ("HEAD", "OPTIONS") and str(rule).startswith("/api/")
    }

    assert data_routes == {("GET", "/api/usage"), ("GET", "/api/chats/<chat_id>/raw"), ("GET", "/api/schema")}
