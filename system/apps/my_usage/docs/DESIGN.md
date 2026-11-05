# Design notes

## Pieces

```
Imbue's collection script  --(fetch, by hand)-->  data/.skills/local-analytics-copy/analytics/
        |
        |  run (hourly routine): the script reads the workspace through a symlink mirror ("view/")
        v
runs/<run_id>.jsonl   (payload, byte for byte)      runs.jsonl (index)      cursors.json (feed positions)
        |
        |  read once per file, never written
        v
My usage app:  UsageStore  --/api/usage, /api/chats/<id>/raw-->  the page
```

## The copy (skill)

- `fetch` downloads every file of the collector first and writes them only when all arrived, records the mngr-internal
  commit and each file's sha256 (`SOURCE.json`, `sources.jsonl`). It refuses to run while a collection runs.
- `run` rebuilds `view/` (symlinks to the workspace root and the mngr host folder, with `data/.imbue/analytics`
  pointing at the skill's `analytics/` so the script's own audit files land there and not in the real `data/.imbue/`),
  runs the script with `uv run --no-project --script`, saves its stdout unchanged, and moves the feed positions from
  the `run_summary` line only when the run succeeded. A feed that failed keeps its position.
- The skill never post-processes the payload. Redaction is the script's job and is not repeated or weakened.
- A lock file stops overlapping runs. Payloads are never deleted; only diagnostics are trimmed.

## The app

- `UsageStore` reads each `runs/*.jsonl` once (in name order), skips unreadable lines, ignores `.part` and `.failed`
  files, and counts a record once by its `event_id`. State is in memory, rebuilt on start; nothing is stored.
- Days are in the workspace's time zone (`data/.state/user_timezone`).
- Messages you sent: `client_activity` `message` events. Agent steps: `transcripts` steps whose source is `agent`,
  by model and chat. Chat names: `agents/<id>/data.json` in the host folder, or the folder name of a destroyed agent.
  Apps: latest `workspace_state` snapshot, `window_opened` events and `gateway_request_count` records.
- `GET /api/chats/<id>/raw` answers only for ids present in the collected data and reads a record by its saved byte
  offset inside `runs/`, refusing any path that resolves outside it.
- The page is plain HTML and JavaScript served by the app (no CDN, no build). Filters (date range, model) scope every
  number below them; every chart has a table view; hovering a column shows its value.
- Every HTML response carries the workspace's Content-Security-Policy; the app serves its own scripts.

## What the data on disk is

Everything under `data/.skills/local-analytics-copy/` is collected data and stays when the app or skill is removed.
`data/.apps/my-usage/` is not used by this version.

## Settings (environment variables)

`MY_USAGE_PORT` (default 8098), `MY_USAGE_COPY_DIR` (default `data/.skills/local-analytics-copy`), `MNGR_HOST_DIR`
(default `/home/user/.mngr`), `MY_USAGE_TIMEZONE_FILE` (default `data/.state/user_timezone`).
