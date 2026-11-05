# Operations runbook

Day-two work. Everything below was read from the code or run on the build workspace unless marked unverified.

## Where things are

- The app: `system/apps/my_usage/`, supervisord program `my-usage` (logs `/var/log/supervisor/my-usage-stdout.log` and
  `-stderr.log`). It is an app with `stop_when_no_windows = true`: the workspace stops it a minute after its last window
  closes and starts it again on the next request, so "not running" in `supervisorctl status` is normal when no window
  is open. The first request after a stop reads every collected file again, which takes a couple of seconds.
- The skill: `.agents/skills/local-analytics-copy/`.
- The collected files: `data/.skills/local-analytics-copy/` (`runs/`, `runs.jsonl`, `cursors.json`, `analytics/`,
  `sources.jsonl`). These are your data; the app never writes to them.
- The routine: `system/routines/local-analytics-copy.toml`, shown in the Routines app.

## Commands

```bash
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py status       # version, recent runs, size
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py run          # one collection now
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py fetch        # refresh the collection script
```

## Reading what `run` prints

| Message | Meaning | What to do |
| --- | --- | --- |
| `Run <id>: ... records: ...` | A collection finished. | Nothing. |
| `Input budget exhausted; the next run continues` | A big backlog; it stopped at the byte budget. | Run again, or wait for the hourly run. |
| `Another run is in progress; skipping this one` | The lock is held by an earlier run. | Wait. A routine run is cut off after 30 minutes. |
| `No collector yet; run the fetch command first` | `analytics/` is empty. | Run `fetch` (needs GitHub access to the private repo). |
| `Feed errors: {...}` | One data feed failed; its position did not move, so its records are collected next run. | Read the run's `.stderr.log`. |
| exit 1 and `.failed` file | The collection failed; positions did not move. | Read `runs/<id>.stderr.log`; run again. |

A run killed part-way (a restart, the routine's timeout) leaves `runs/<id>.jsonl.part`; the next run keeps it as a
`.failed` file and records it as interrupted.

## Renewing the GitHub sign-in

`fetch` and `status`' "newer collector?" check go through the workspace's GitHub sign-in. When they fail with a 401 or
"Bad credentials", run `latchkey services info github` and look at `credentialStatus`; if it is not `valid`, send the
`github-rest-api` / `github-git` permission requests again to sign in. Hourly runs do not need GitHub at all.

## Refreshing the collection script

Only when you decide to. The script is code from a remote repo that runs against your private data, so nothing
refreshes it automatically: no routine fetches. `status` makes one GitHub call and tells you if the repo's main has a
different script than the one fetched. `fetch` keeps every version it downloaded in `sources.jsonl`. To pin an older
version: `fetch --ref <commit>`.

## Disk

Collected payloads are never deleted. About 100 KB per hourly run on the build workspace (a full first run is
~10 MB). `status` prints the size of `runs/` and warns above 2 GB. Only diagnostics are trimmed: the newest 100
`.stderr.log` files and the newest 10 `.failed` outputs are kept. The `view/` folder (symlinks only) is rebuilt on
every run and excluded from the backup.

## Safe limits

- Do not share the page. "View raw" shows chat text from the collected transcripts (redacted by the collection
  script, but still chat content). The page is private to you unless you share the app.
- Do not run the first collection while memory is tight: the script loads a language model (a few hundred MB).
  If a run dies with exit 137 it was shed by the memory guard; retry when memory is free.

## Removing

Remove `system/apps/my_usage/`, `system/supervisord.conf.d/my-usage.conf`, `.agents/skills/local-analytics-copy/` and
the `local-analytics-copy` routine (`uv run --no-sync routines remove local-analytics-copy`), then
`supervisorctl reread && supervisorctl update`. `data/.skills/local-analytics-copy/` (the collected files) stays.
