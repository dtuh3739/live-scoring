# score-agent

A weekly betting-club companion: polls The Odds API for tipped games, pings Slack on score changes and final whistles. Runs free on GitHub Actions cron.

## How it works

1. You edit `config/games.json` each week with your tipped games.
2. GitHub Actions starts a job twice per footy day (Thu–Sun); each job runs `poll.py` every 5 minutes for ~5 hours, covering the afternoon and evening windows.
3. The script calls `/v4/sports/{sport}/scores/` once per unique sport, matches games by team name, diffs against `state/last_scores.json`, and posts to Slack on any change.
4. State is committed back to the repo so the next run can diff against it.

> **⚠️ The workflow is fully self-contained — do NOT add any external cron or personal access token to trigger it.** The 5-minute cadence comes from a loop *inside* the job, kicked off by GitHub's own `schedule:`. An external token (`live-scoring-cron`) firing `workflow_dispatch` every 5 minutes, 24/7, is what got the GitHub account suspended in May 2026. That token has been deleted and must stay deleted.

## Setup

### 1. Get an Odds API key

Sign up at <https://the-odds-api.com/>. The free tier is **500 requests/month** — that's tight (see "Quota planning" below). If you'll run this regularly, you likely need the $30/month plan (20k requests).

### 2. Confirm sport availability

Run the discovery helper locally:

```bash
pip install requests
ODDS_API_KEY=your_key_here python scripts/list_sports.py
```

This dumps every sport key. Confirmed working for this project:

- `aussierules_afl` — AFL ✓
- `rugbyleague_nrl` — NRL ✓
- **Rugby union** — The Odds API's marketing materials don't list rugby union as a covered sport. Check the `list_sports.py` output for keys like `rugbyunion_*`. If nothing shows up, you'll need a second source for rugby union (see "Adding rugby union" below).

### 3. Create a Slack incoming webhook

1. Go to <https://api.slack.com/apps> → Create New App → From scratch.
2. Enable **Incoming Webhooks**.
3. Add a webhook to the channel you want to receive pings in (DM yourself, or a betting-club channel).
4. Copy the webhook URL (looks like `https://hooks.slack.com/services/T.../B.../...`).

### 4. Push to GitHub & set secrets

```bash
git init
git add .
git commit -m "Initial commit"
# create a private repo on GitHub, then:
git remote add origin git@github.com:YOU/score-agent.git
git push -u origin main
```

In your repo: **Settings → Secrets and variables → Actions → New repository secret**:

- `ODDS_API_KEY` — your Odds API key
- `SLACK_WEBHOOK_URL` — your Slack webhook URL

### 5. Edit `config/games.json` for the week

Team names can be partial — `"Storm"` matches `"Melbourne Storm"`. The `tipped` field is optional but adds bet status to the Slack message.

```json
{
  "games": [
    {
      "sport": "rugbyleague_nrl",
      "home_team": "Storm",
      "away_team": "Bulldogs",
      "tipped": "Storm"
    }
  ]
}
```

Commit and push. The cron will pick it up on the next run.

### 6. Trigger a manual run to verify

In GitHub: **Actions → Poll scores → Run workflow**. Check the run logs — you should see `👁  first sight` lines for each game (no Slack messages on the first run; that's by design to avoid spam on initial deploy or when adding mid-match games).

## Quota planning

Free tier is 500 requests/month. Polling runs every 5 min during footy windows — two ~5h GitHub Actions jobs per day on Thu–Sun, started by the `schedule:` crons in `.github/workflows/poll.yml` (03:00 and 08:00 UTC). With 3 sports tracked, that's roughly:

- 12 polls/hour × 6 hours × 4 days × 4 weeks × 3 sports ≈ **3,500 requests/month**

Options to stay within budget:

- **Tighten the window** — narrow the start times / shorten `duration_minutes`, or drop the second daily start. Edit `.github/workflows/poll.yml`.
- **Reduce polling cadence** — change `sleep 300` to `sleep 600` (every 10 min) in the loop step; halves the cost.
- **Drop unused sports** — if a given week has no AFL games, comment them out of config (the script only polls sports that appear in config).
- **Upgrade to $30/month** — gets you 20k requests, plenty of headroom.

The script logs `quota used` and `remaining` after each API call so you can watch it.

## Adding rugby union

If The Odds API doesn't have rugby union (or your specific competition), the cleanest pattern is:

1. Add a second fetch function in `poll.py` for an alternative provider — e.g. **API-Sports Rugby** (free tier 100 req/day, covers Super Rugby + Rugby Championship), or **Highlightly Rugby API** (free tier 100 req/day).
2. Tag those games in config with a different `sport` value, e.g. `"sport": "rugby_super_rugby_apisports"`.
3. Branch in `fetch_scores()` based on the prefix.

Happy to flesh that out if/when you confirm rugby union isn't in The Odds API.

## Notification design

- **First sight** of a game (no prior state) → silent. Bookmarks the score. This stops the deploy from spamming you with seven "score updates" on first run.
- **Score change** → ping with new score, scoring side, and your tip's status (leading / trailing / level).
- **Game completion** → ping with final score and your tip's outcome (won / lost / drew).

If you want kickoff alerts (first time a 0-0 game appears), it's a one-line addition in `poll.py` — let me know.

## File layout

```
score-agent/
├── .github/workflows/poll.yml    # schedule → in-job 5-min poll loop + commit-state-back
├── config/games.json             # weekly tipped games (you edit this)
├── state/last_scores.json        # auto-managed; do not edit
├── scripts/list_sports.py        # discovery helper for sport keys
├── poll.py                       # main script
├── requirements.txt
└── README.md
```

## Local testing

```bash
pip install -r requirements.txt
export ODDS_API_KEY=xxx
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
python poll.py
```

First run bookmarks state silently. Run it twice with a wait, mid-match, to see the Slack ping fire.
