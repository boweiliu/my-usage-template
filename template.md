---
title: "My usage"
description: "A page that shows your own usage of a Studio workspace (messages you sent and agent steps per day, steps by model, chats, apps, and the original record behind each number), drawn from a local copy of the analytics Imbue's collection takes, plus the skill that keeps that copy."
thumbnail: "template.svg"
version: v1
format: v2
---

# My usage

This file is the manifest for the **My usage** template (slug:
`my-usage`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A page that shows your own usage of a Studio workspace (messages you sent and agent steps per day, steps by model, chats, apps, and the original record behind each number), drawn from a local copy of the analytics Imbue's collection takes, plus the skill that keeps that copy.

Imbue's analytics service visits explorer-plan workspaces about hourly, runs a
collection script inside them and keeps what the script prints. It never visits
a local workspace, and the person whose usage it is never sees the result. This
template gives them their own copy and a page to read it on. The skill
`local-analytics-copy` downloads the same collection script, runs it against
the workspace every hour (with the same redaction Imbue's run applies) and
keeps each run's output unchanged under `data/.skills/local-analytics-copy/`.
The **My usage** window reads that copy and shows: messages you sent and agent
steps per day, steps by model, your chats (with each chat's original records
one click away), your apps, and a strip saying when the copy was last
collected and whether any collection failed. The template ships no data: the
page shows "Nothing collected yet" until the first collection has run.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/my_usage/`
- `system/supervisord.conf.d/my-usage.conf`
- `.agents/skills/local-analytics-copy/`

- `system/apps/my_usage/` -- the app: a small Flask server (`src/my_usage/runner.py`
  for routes and settings, `usage_data.py` for reading and counting the collected
  files) and a plain HTML/JavaScript page with no build step (`src/my_usage/assets/`).
  It only reads; it stores nothing of its own. It carries its tests, its icon,
  its `app.toml`, and three guides under `docs/`: `RUNBOOK-setup.md` (from
  adoption to a page with your numbers), `RUNBOOK-operations.md` (day-two work)
  and `DESIGN.md` (how the pieces fit).
- `system/supervisord.conf.d/my-usage.conf` -- the supervisord program
  `my-usage`. It syncs the app's own venv (`uv sync --locked --project
  system/apps/my_usage`), registers the app with `forward_port.py --manifest
  system/apps/my_usage/app.toml --url http://localhost:8098`, then runs the
  server on port 8098. The app's `app.toml` sets `stop_when_no_windows = true`,
  so the shell stops it a minute after its last window closes and starts it on
  the next request.
- `.agents/skills/local-analytics-copy/` -- the skill that keeps the copy, with
  three subcommands of `scripts/local_analytics_copy.py`: `fetch` downloads the
  collection script from `imbue-ai/mngr-internal` through the workspace's GitHub
  sign-in (`latchkey curl` against `api.github.com`) and records the commit;
  `run` performs one collection (the script runs with `uv run --no-project
  --script`, which brings its own Python packages, about 100 MB, on first use);
  `status` names the fetched commit, whether `main` has a newer one, recent runs
  and disk use. Its tests and invented-id fixtures are under `scripts/` and
  `tests/fixtures/`.

At runtime: the hourly routine (added during setup, not shipped) calls `run`,
which writes `data/.skills/local-analytics-copy/runs/<run id>.jsonl` (the
payload, byte for byte), `runs.jsonl` (an index with counts and hashes) and
`cursors.json` (where each feed stopped). On each request the app reads any run
file it has not seen yet (it keeps its counts in memory), and serves `/api/usage` (the counts behind the page),
`/api/chats/<id>/raw` (a chat's original records, read from `runs/` by saved
offset) and `/api/schema`. Settings come from environment variables:
`MY_USAGE_PORT` (default 8098), `MY_USAGE_COPY_DIR`, `MNGR_HOST_DIR` and
`MY_USAGE_TIMEZONE_FILE`.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


### Activation

- requires_permission: github-rest-api / github-read-repos (user-approved; the adopting agent initiates it during setup. The collection script is downloaded from the private repo `imbue-ai/mngr-internal`, so the GitHub account signed in must be able to read that repo -- in practice, Imbue staff. Check: `latchkey curl -s -o /dev/null -w '%{http_code}\n' https://api.github.com/repos/imbue-ai/mngr-internal` prints `200`; a `404` means the account cannot see the repo, and the rest of setup cannot proceed.)

Once that check prints `200`, finish activation in this order. Each step has the
check that proves it worked; `system/apps/my_usage/docs/RUNBOOK-setup.md` has the
full commands.

1. Download the collection script:
   `python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py fetch`.
   Check: `... local_analytics_copy.py status` prints `Collector: imbue-ai/mngr-internal@<commit>`.
2. Run the first collection in the background (it downloads about 100 MB of
   Python packages and reads every chat transcript on the machine):
   `python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Collecting the first copy of the usage data" -- python3 .agents/skills/local-analytics-copy/scripts/local_analytics_copy.py run`.
   Check: `status` lists a run with exit 0. "Input budget exhausted" means run it
   again; it continues where it stopped.
3. Add the hourly routine (the `routines add local-analytics-copy ... --every 1h`
   command in the setup guide, step 4). Check: `uv run --no-sync routines list`
   shows it. It is deliberately not shipped: before step 1 it would fail every hour.
4. Open the page: `uv run --no-sync workspace-layout open my-usage`. Check: the
   strip under the title reads "Last collected ... no errors" and the charts show
   the user's own days.

### Adaptation

- **No access to `imbue-ai/mngr-internal`.** Without it there is nothing to
  collect with: the page installs, starts and shows "Nothing collected yet",
  and that is all. A working replacement needs the collection script from some
  other source the user can read; `fetch` would have to be pointed there.
  Otherwise tell the user plainly that this template only works for accounts
  that can read that repo.
- **Opens and request counts per app are missing.** They need the workspace to
  carry default-workspace-template PR #1061 (the share gateway's request counts
  and `window_opened` events in the client-activity log), merged 2026-11-04 and
  in no release at that date. Until the workspace has it, the Apps panel lists
  installed apps and says the counts are not available. Nothing to rewire; tell
  the user, and check again after the next workspace update.
- **Error reports (Sentry) are not covered.** They are a different stream that
  this collection never sees. Tell the user if they ask about errors.
- **Adoption into a fresh workspace has not been tried.** The app and skill ran
  and were tested in the workspace they came from; work through the setup guide
  step by step and check each step's result rather than assuming it.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

(The collection script brings its own Python packages through `uv` the first
time it runs, about 100 MB, into uv's cache; the skill uses `latchkey` and the
`betterleaks` and `kingfisher` scanners the stock workspace already ships.)

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `connect-external-service` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-11-04) -- the My usage page, the skill that keeps a local copy of the usage analytics, and their setup, operations and design guides

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
