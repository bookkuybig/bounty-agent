# bounty-agent

Finds paid open-source issues worth your time, and — more importantly — throws
away the ones that are not.

The expensive mistake in bounty hunting is not writing code slowly, it is
picking an issue that already has 40 agent-authored pull requests attached to
it. Merge rates across bounty platforms sit around 15%, and unmerged work pays
nothing. This tool measures competition *before* you open an editor.

**No LLM runs in this pipeline.** Scoring is arithmetic. That is deliberate:
tokens get spent only on the handful of issues that survive, by you or by a
triage step you add later.

## Requirements

- Python 3.11+ (uses stdlib `tomllib`, `sqlite3`) — verified on 3.14
- [GitHub CLI](https://cli.github.com/) authenticated: `gh auth status`

No third-party packages. No API token stored anywhere in this repo — every
request goes through `gh`, so the credential stays in your OS keyring.

Copy `config.example.toml` to `config.toml` and edit it. `config.toml` is
git-ignored because it holds your ntfy topic.

Built with Claude Code.

## Use

```bash
python main.py collect      # one cycle: search, measure, score, store
python main.py report       # ranked candidates, best expected $/hr first
python main.py report --json
python main.py stats        # what was dropped and why
python main.py rate         # remaining GitHub API budget
```

Run every 15 minutes:

```powershell
.\install-schedule.ps1
```

Remove it with `.\install-schedule.ps1 -Remove`.

## How it ranks

```
score = bounty_usd × p_win / estimated_hours        (expected USD per hour)

p_win = 1 / (1 + open_linked_PRs)
        × 0.05   if assigned to someone else
        × 1.4    if untouched 45+ days   (earlier attempts failed; still unclaimed)
        × 0.6    if the repo has 20k+ stars (rivals keep arriving)
        × 0.5    if the language is outside [stack].preferred
```

`open_linked_PRs` comes from the issue timeline — every pull request
cross-referencing the issue. It is the single most predictive signal available,
and the reason this is worth running at all.

Issues with a **merged** linked PR are dropped as `likely-solved` regardless of
issue state, because the reward is gone either way.

## Signals, and how much to trust them

| Signal | Source | Reliability |
|---|---|---|
| open linked PRs | issue timeline | high — this is the real competition |
| assignee | search result | high |
| bounty amount | regex over title, labels, then body | **medium** — Algora posts amounts in a bot *comment*, which search does not expose, so many rows show `?` |
| estimated hours | label and body-length heuristic | low — a placeholder, override it by eye |
| repo language and stars | repo endpoint, cached 7 days | high |

Unknown amounts are ranked using `[scoring].assumed_amount` with a penalty
rather than being dropped. Treat the `$/hr` column as an ordering, not a wage.

## Tuning

Everything lives in `config.toml`. The two settings that matter most:

- `[filter].max_competitors` — raise it to see more, lower it to see only clear runs
- `[stack].preferred` — set your languages, e.g. `["Python", "TypeScript", "Rust"]`

## Cost

Roughly 70–80 GitHub API requests per cycle against a 5000/hour limit, so a
15-minute interval uses about 6% of the budget. `[api].timeline_budget` caps
the expensive part.

## What this does not do

It does not write pull requests, and it should not. Automated PR spam is what
collapsed merge rates in the first place; it gets accounts banned and burns
tokens on work that will never be paid. This finds the openings. You decide
which ones are real, and ask the maintainer to assign you before writing code.
