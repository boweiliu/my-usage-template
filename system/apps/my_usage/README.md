# My usage

A page in a Studio workspace that shows your own usage, drawn from a local copy of the analytics Imbue's collection
takes: messages you sent and agent steps per day, steps by model, your chats, your apps, and the original record behind
each number. The skill `local-analytics-copy` (`.agents/skills/local-analytics-copy/`) keeps that copy by running the
same collection script Imbue runs on explorer-plan workspaces, every hour, and saving each run's output unchanged under
`data/.skills/local-analytics-copy/runs/`. The page only reads those files.

It needs read access, through the workspace's GitHub sign-in, to the private repo the collection script is downloaded
from (`imbue-ai/mngr-internal`, Imbue staff only).

Start with `docs/RUNBOOK-setup.md`. `docs/RUNBOOK-operations.md` covers day-two work and `docs/DESIGN.md` how the
pieces fit.

- `src/my_usage/runner.py`: routes, security policy, settings from environment variables.
- `src/my_usage/usage_data.py`: reads the collected files, counts, answers the routes.
- `src/my_usage/assets/`: the page (plain HTML and JavaScript, no build step).

Run the tests from this folder: `uv run --no-sync pytest -m ''`.
