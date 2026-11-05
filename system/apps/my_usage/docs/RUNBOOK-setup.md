# Setup runbook

From a workspace that has adopted this template to a page showing your own numbers. Each step ends with the check that
proves it worked. Run commands from the workspace root.

The template brings the app (`system/apps/my_usage/`, its program `my-usage`) and the skill that keeps the local copy
of the analytics (`.agents/skills/local-analytics-copy/`). It does not bring the hourly routine: a routine that runs
before the collection script is downloaded would fail every hour, so step 4 adds it.

## 1. GitHub access

The collection script is downloaded from a private repo, `imbue-ai/mngr-internal` (Imbue staff only). Check that the
workspace's GitHub sign-in can read it:

```bash
latchkey curl -s -o /dev/null -w '%{http_code}\n' https://api.github.com/repos/imbue-ai/mngr-internal
```

Check: `200`. A `404` means the signed-in account cannot see the repo. The page still starts and shows "Nothing
collected yet", but there is nothing to collect with.

## 2. Download the collection script

```bash
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py fetch
python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py status
```

Check: `status` prints `Collector: imbue-ai/mngr-internal@<commit>` and a line saying whether main has the same
collector.

## 3. First collection

It downloads the script's Python packages (about 100 MB) and reads every chat transcript on the machine, so start it
in the background:

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py \
  --description "Collecting the first copy of the usage data" -- \
  python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py run
```

Check: when it finishes, its message shows `Run <id>: <n> bytes; records: ...` and `status` lists a run with exit 0. If
it says "Input budget exhausted", run it again: it continues from where it stopped.

## 4. Hourly collection

```bash
uv run --no-sync routines add local-analytics-copy --title "Local analytics copy" \
  --what "Collects the same usage data Imbue's analytics would take from this workspace and keeps a copy on this machine." \
  --every 1h --timeout 30m --retry-after 10m --notify-after-failures 3 -- \
  python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py run
```

Check: `uv run --no-sync routines list` shows it.

## 5. Open the page

```bash
uv run --no-sync workspace-layout open my-usage
```

Check: the strip under the title reads "Last collected ... collections ... records ... no errors", the charts show your
days, and "View raw" on a chat opens its original records.

## 6. Tests

```bash
(cd system/apps/my_usage && uv run --no-sync pytest -m '')
uv run --no-sync pytest .agents/skills/local-analytics-copy
```

## What the page cannot show yet

Opens and request counts per app need the workspace to carry default-workspace-template PR #1061 (the share gateway's
access log and request counts, and `window_opened` in the client-activity log). It was merged on 2026-11-04 and in no
release then. Until the workspace has it, the Apps panel lists installed apps and says so. Error reports (Sentry) are a
different stream and are not covered.
