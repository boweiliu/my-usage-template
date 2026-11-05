---
name: local-analytics-copy
description: "Keep a local copy of the exact usage data Imbue's analytics collection takes from a workspace, so the user can analyze their own usage here. Use when the user asks for their own copy of the analytics, wants to see what Imbue would collect from this workspace, or asks to refresh, re-run or inspect that copy."
compatibility: Needs the user's connected GitHub account (the collector lives in the private mngr-internal repo), `uv`, and the `betterleaks` and `kingfisher` binaries the workspace already ships.
metadata:
  author: user
  crystallized: true
---

# Local analytics copy

Imbue's analytics service visits explorer-plan workspaces about hourly, writes a
collection script into `data/.imbue/analytics/` and runs it; the script's stdout is the
whole payload. This workspace is local, so that never happens here. This skill fetches the same
script from mngr-internal and runs it against this workspace, keeping each run's stdout.
Redaction (structural stripping, secret scanning, PII scrubbing) runs exactly as it would for
Imbue, so the files are what Imbue would receive. The skill never rewrites that output.

## Steps

1. `[script]` **fetch**: download the collector at a commit and record which one.
2. `[script]` **run**: one collection; keeps its output and moves the feed positions on.
3. `[script]` **status**: collector version, whether mngr-internal has a newer one, recent runs,
   disk use.

```bash
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py fetch [--ref main]   # download/refresh the collector
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py run [--budget-mib 64]  # one collection
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py status
```

The `local-analytics-copy` routine runs `run` hourly. It never fetches: the collector is code from a
remote repo running against the user's private data, so it changes only when someone runs `fetch`
on purpose. `status` makes one GitHub call to say whether mngr-internal `main` has a different
collector than the fetched one (or that it could not check); tell the user, and fetch only when they
want the new version. A first `run` downloads the script's Python dependencies (about 100 MB) and
takes a while on a large backlog; start it with `run_in_background.py`. Overlapping runs are skipped
by a lock, and `fetch` refuses to run while a collection is running.

## Where things are

`data/.skills/local-analytics-copy/`:

- `runs/<run_id>.jsonl`: one run's payload, unchanged: `{"source", "record"}` lines and a final
  `run_summary`. Sources: `transcripts` (redacted), `client_activity`, `services`, `servers`,
  `gateway_requests`, `git_numstat`, `workspace_state`.
- `runs.jsonl` (index with counts and a sha256 of each payload; a failed run has `error` and
  `failed_file`), `cursors.json` (where each feed stopped), `runs/<run_id>.stderr.log` (collector
  diagnostics), `runs/<run_id>.failed` (the output of a failed run).
- `analytics/`: the collector as Imbue lays it out, with `SOURCE.json` naming the mngr-internal
  commit and file hashes; `sources.jsonl` keeps every version fetched.

## Things to know

- Payloads are never deleted, by the user's choice. `status` prints the runs folder's size and
  warns once it passes 2 GB, so the user can decide then. Only diagnostics are pruned on each run:
  the newest 100 `.stderr.log` files and the newest 10 `.failed` outputs are kept. A failed run
  leaves the feed positions where they were, so its records are collected again by the next run.
- A run killed part-way (the routine's timeout, a restart) leaves `runs/<run_id>.jsonl.part`; the
  next run keeps it as `.failed` and indexes it as interrupted.
- The input budget drains in feed order, transcripts first. A small `--budget-mib` can leave the
  later feeds empty for that run; the saved cursors make the next run continue.
- `view/` is rebuilt every run and left out of the backup (`.nobackup`): symlink mirrors of the
  workspace root and the mngr host dir, so the collector's own audit files land in `analytics/` and
  not in the real `data/.imbue/`. It also links the shell's client-activity log
  (`data/.state/system_interface/events/client_activity/`) to the path the collector reads
  (`agents/<id>/workspace_layout/events/client_activity/`), which this workspace's layout does not
  otherwise provide; the link is skipped once the agent dir has a `workspace_layout` of its own.
- `gateway_requests` stays empty until the workspace has default-workspace-template PR #1061
  (the share gateway's per-minute request counts).
- Error reports (Sentry) are a different stream and are not covered here: mngr sets up Sentry only
  in the Studio desktop app's forwarder and in Imbue's Modal services, not in this workspace.
