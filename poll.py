"""
Score polling agent.

Reads tracked games from Slack (if SLACK_BOT_TOKEN + SLACK_GAMES_CHANNEL are
set) or falls back to config/games.json.  Calls The Odds API scores endpoint
for each unique sport, diffs against last known state, and pings Slack on any
change (score movement or final whistle).

State is persisted in state/last_scores.json (committed back by GitHub Actions).

Required env vars:
    ODDS_API_KEY          — from the-odds-api.com
    SLACK_WEBHOOK_URL     — incoming webhook URL from Slack

Optional env vars (enable Slack-based game config):
    SLACK_BOT_TOKEN       — xoxb-... bot token with channels:history scope
    SLACK_GAMES_CHANNEL   — channel ID (e.g. C01234ABC) where you post games

Slack message format (one game per line):
    nrl Storm vs Bulldogs tip Storm
    afl Carlton vs Collingwood tip Collingwood
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AEST = ZoneInfo("Australia/Sydney")

import requests

GAME_WINDOW_HOURS = 4  # how long after kick-off we treat a game as potentially active

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "games.json"
STATE_PATH = ROOT / "state" / "last_scores.json"

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SLACK_API_BASE = "https://slack.com/api"

SPORT_ALIASES = {
    "nrl": "rugbyleague_nrl",
    "afl": "aussierules_afl",
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "epl": "soccer_epl",
    "afl_w": "aussierules_afl_womens",
}


def parse_games_message(text: str) -> list[dict]:
    """Parse a Slack message into a games list.

    Each line: <sport> <home> vs <away> [tip <team>]
    """
    games = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        sport_raw, rest = parts
        sport_raw = sport_raw.rstrip(":")  # handle "nrl:" or "afl:"

        tipped = None
        if " tip " in rest.lower():
            idx = rest.lower().index(" tip ")
            tipped = rest[idx + 5:].strip()
            rest = rest[:idx].strip()

        # accept both " vs " and " v " as separator
        rest_lower = rest.lower()
        if " vs " in rest_lower:
            sep, sep_len = " vs ", 4
        elif " v " in rest_lower:
            sep, sep_len = " v ", 3
        else:
            continue
        idx = rest_lower.index(sep)
        home = rest[:idx].strip()
        away = rest[idx + sep_len:].strip()

        sport = SPORT_ALIASES.get(sport_raw.lower(), sport_raw.lower())
        game: dict = {"sport": sport, "home_team": home, "away_team": away}
        if tipped:
            game["tipped"] = tipped
        games.append(game)
    return games


def fetch_games_from_slack() -> list[dict] | None:
    """Read the most recent games-config message from the Slack channel.

    Returns None if env vars aren't set or the call fails (caller falls back
    to config/games.json).
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_GAMES_CHANNEL")
    if not token or not channel:
        return None

    try:
        r = requests.get(
            f"{SLACK_API_BASE}/conversations.history",
            headers={"Authorization": f"Bearer {token}"},
            params={"channel": channel, "limit": 20},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"  ⚠️  Slack read error: {e}", file=sys.stderr)
        return None

    if not r.ok:
        print(f"  ⚠️  Slack read error: HTTP {r.status_code}", file=sys.stderr)
        return None

    data = r.json()
    if not data.get("ok"):
        print(f"  ⚠️  Slack API error: {data.get('error')}", file=sys.stderr)
        return None

    for msg in data.get("messages", []):
        games = parse_games_message(msg.get("text", ""))
        if games:
            return games

    return None


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def _game_in_window(commence_time_str: str, now: datetime) -> bool:
    try:
        commence = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (commence - timedelta(minutes=30)) <= now <= (commence + timedelta(hours=GAME_WINDOW_HOURS))


def should_poll_sport(sport: str, tracked: list[dict], state: dict, now: datetime) -> bool:
    """Return True if any tracked game for this sport needs a live API call.

    Skips the call if all games have a cached commence_time outside the active
    window, or are already completed.  Defaults to True (poll) when unsure —
    e.g. first run before any state is cached.
    """
    sport_games = [g for g in tracked if g["sport"] == sport]
    for tg in sport_games:
        a, b = tg["home_team"].lower(), tg["away_team"].lower()
        cached = next(
            (gs for gs in state.values()
             if gs.get("home_team") and gs.get("away_team")
             and _matches_team(a, gs["home_team"])
             and _matches_team(b, gs["away_team"])),
            None,
        )
        if cached is None:
            return True  # never seen — must discover
        if cached.get("completed"):
            continue  # already finished, no need to poll
        commence = cached.get("commence_time")
        if not commence:
            return True  # no time cached yet — poll to get it
        if _game_in_window(commence, now):
            return True
    return False


def fetch_scores(sport_key: str) -> list[dict]:
    """Fetch live + recent (last 1 day) scores for a sport.

    Returns [] if the sport isn't supported by the scores endpoint or the
    request fails — we'd rather miss one sport than crash the whole run.
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores/"
    params = {"apiKey": ODDS_API_KEY, "daysFrom": 1, "dateFormat": "iso"}
    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException as e:
        print(f"  ⚠️  {sport_key}: network error — {e}", file=sys.stderr)
        return []

    if r.status_code == 422:
        print(f"  ⚠️  {sport_key}: not supported by scores endpoint", file=sys.stderr)
        return []
    if r.status_code == 401:
        print(f"  ❌ {sport_key}: invalid API key", file=sys.stderr)
        return []
    if not r.ok:
        print(f"  ⚠️  {sport_key}: HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)
        return []

    remaining = r.headers.get("x-requests-remaining", "?")
    used = r.headers.get("x-requests-used", "?")
    games = r.json()
    print(f"  {sport_key}: {len(games)} games returned (quota used {used}, remaining {remaining})")
    return games


def match_game(tracked: dict, api_games: list[dict]) -> dict | None:
    """Find the API game matching a tracked game by team substring (either side).

    Tracked teams can be partial — "Storm" will match "Melbourne Storm".
    """
    a = tracked["home_team"].lower()
    b = tracked["away_team"].lower()
    for g in api_games:
        h = g["home_team"].lower()
        w = g["away_team"].lower()
        if (a in h and b in w) or (a in w and b in h):
            return g
    return None


def get_score_dict(api_game: dict) -> dict | None:
    """Convert API scores list to {team_name: int}, or None if not yet started."""
    if not api_game.get("scores"):
        return None
    try:
        return {s["name"]: int(s["score"]) for s in api_game["scores"]}
    except (ValueError, TypeError, KeyError):
        return None


def format_message(
    api_game: dict,
    prev_score: dict | None,
    curr_score: dict | None,
    just_completed: bool,
    tipped: str | None,
) -> dict:
    """Build a Slack webhook payload for one game event."""
    home = api_game["home_team"]
    away = api_game["away_team"]
    home_s = (curr_score or {}).get(home, 0)
    away_s = (curr_score or {}).get(away, 0)

    if just_completed:
        if home_s > away_s:
            winner = home
        elif away_s > home_s:
            winner = away
        else:
            winner = None

        if winner:
            line1 = f"🏆 *FINAL* — {home} *{home_s}* – *{away_s}* {away}"
        else:
            line1 = f"🏆 *FINAL (DRAW)* — {home} *{home_s}* – *{away_s}* {away}"

        line2 = ""
        if tipped:
            if winner is None:
                line2 = f"➖ Your tip ({tipped}) drew"
            elif _matches_team(tipped, winner):
                line2 = f"✅ Your tip ({tipped}) won"
            else:
                line2 = f"❌ Your tip ({tipped}) lost"
    else:
        prev_home = (prev_score or {}).get(home, 0)
        prev_away = (prev_score or {}).get(away, 0)
        d_home = home_s - prev_home
        d_away = away_s - prev_away

        if d_home > 0 and d_away <= 0:
            scored = f"+{d_home} {home}"
        elif d_away > 0 and d_home <= 0:
            scored = f"+{d_away} {away}"
        elif d_home > 0 and d_away > 0:
            scored = f"+{d_home} {home}, +{d_away} {away}"
        else:
            scored = "score updated"

        line1 = f"🏉 {home} *{home_s}* – *{away_s}* {away}"
        line2 = scored

        if tipped:
            if home_s > away_s:
                leader = home
            elif away_s > home_s:
                leader = away
            else:
                leader = None

            if leader is None:
                status = "scores level"
            elif _matches_team(tipped, leader):
                status = f"your tip ({tipped}) leading"
            else:
                status = f"your tip ({tipped}) trailing"
            line2 += f" · {status}"

    text = f"{line1}\n{line2}".strip()
    return {"text": text}


def _matches_team(query: str, full_name: str) -> bool:
    return query.lower() in full_name.lower() or full_name.lower() in query.lower()


def post_to_slack(payload: dict) -> None:
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()


_SPORT_ALIASES_INV = {v: k for k, v in SPORT_ALIASES.items()}


def _alert_no_match(tg: dict, label: str) -> None:
    sport_short = _SPORT_ALIASES_INV.get(tg["sport"], tg["sport"])
    tip_part = f" tip {tg['tipped']}" if tg.get("tipped") else ""
    example = f"{sport_short} {tg['home_team']} vs {tg['away_team']}{tip_part}"
    text = (
        f"⚠️ *Game not found in API:* {label}\n"
        f"Check your Slack game config. To fix, post:\n"
        f"`{example}`"
    )
    try:
        post_to_slack({"text": text})
    except Exception as e:
        print(f"  ❌ Could not send no-match alert: {e}", file=sys.stderr)


def main() -> int:
    slack_games = fetch_games_from_slack()
    if slack_games is not None:
        tracked = slack_games
        print(f"Using {len(tracked)} game(s) from Slack:")
        for g in tracked:
            sport_short = _SPORT_ALIASES_INV.get(g["sport"], g["sport"])
            tip_part = f" (tip: {g['tipped']})" if g.get("tipped") else ""
            print(f"  • {sport_short} {g['home_team']} vs {g['away_team']}{tip_part}")
    else:
        config = load_config()
        tracked = config.get("games", [])

    if not tracked:
        print("No games to track — post a games list to Slack or edit config/games.json")
        return 0

    state = load_state()
    no_match_alerts = state.get("_no_match_alerts", {})
    new_state: dict = {}
    notifications = 0
    now = datetime.now(timezone.utc)

    sports = sorted({g["sport"] for g in tracked})
    print(f"Polling {len(sports)} sport(s) for {len(tracked)} tracked game(s)")
    polled_sports: set[str] = set()
    api_games_by_sport: dict = {}
    for sport in sports:
        if should_poll_sport(sport, tracked, state, now):
            api_games_by_sport[sport] = fetch_scores(sport)
            polled_sports.add(sport)
        else:
            print(f"  {sport}: no active games in window — skipping API call")
            api_games_by_sport[sport] = []

    new_no_match_alerts: dict = {}
    for tg in tracked:
        sport = tg["sport"]
        api_game = match_game(tg, api_games_by_sport.get(sport, []))
        label = f"{tg['home_team']} vs {tg['away_team']}"
        alert_key = f"{sport}:{tg['home_team'].lower()}:{tg['away_team'].lower()}"

        if not api_game:
            # Alert once via Slack when we polled but couldn't find the game
            if sport in polled_sports and not no_match_alerts.get(alert_key):
                print(f"  ⚠️  {label}: no match — sending Slack alert")
                _alert_no_match(tg, label)
                new_no_match_alerts[alert_key] = True
            elif no_match_alerts.get(alert_key):
                new_no_match_alerts[alert_key] = True  # keep suppressed
                print(f"  • {label}: no match (alert already sent)")
            else:
                print(f"  • {label}: no match in API response yet")
            continue

        # Game found — clear any prior no-match alert so it can re-fire if config drifts again
        # (alert_key intentionally not carried to new_no_match_alerts)

        gid = api_game["id"]
        curr_score = get_score_dict(api_game)
        completed = bool(api_game.get("completed", False))
        prev = state.get(gid)

        # Always update state
        new_state[gid] = {
            "score": curr_score,
            "completed": completed,
            "home_team": api_game["home_team"],
            "away_team": api_game["away_team"],
            "commence_time": api_game.get("commence_time"),
        }

        if prev is None:
            # First sight — bookmark silently. Avoids spamming on initial deploy
            # or when a game is added mid-match.
            state_desc = f"{curr_score}" if curr_score else "not started"
            print(f"  👁  {label}: first sight ({state_desc})")
            continue

        prev_score = prev.get("score")
        prev_completed = bool(prev.get("completed", False))
        score_changed = curr_score is not None and curr_score != prev_score
        just_completed = completed and not prev_completed

        if score_changed or just_completed:
            payload = format_message(
                api_game, prev_score, curr_score, just_completed, tg.get("tipped")
            )
            try:
                post_to_slack(payload)
                notifications += 1
                event = "FINAL" if just_completed else "SCORE"
                score_str = f"{curr_score}" if curr_score else "?"
                print(f"  📤 {event}: {label} {score_str}")
            except requests.HTTPError as e:
                print(f"  ❌ Slack error for {label}: {e}", file=sys.stderr)
        else:
            if curr_score is not None and not completed:
                home = api_game["home_team"]
                away = api_game["away_team"]
                home_s = curr_score.get(home, 0)
                away_s = curr_score.get(away, 0)
                time_str = now.astimezone(AEST).strftime("%-I:%M%p").lower()
                try:
                    post_to_slack({"text": f"⏱ {time_str} · {home} *{home_s}* – *{away_s}* {away}"})
                    notifications += 1
                    print(f"  📤 TICK: {label} {curr_score}")
                except requests.HTTPError as e:
                    print(f"  ❌ Slack error for {label}: {e}", file=sys.stderr)
            else:
                print(f"  · {label}: no change")

    # Preserve prior state for games we didn't see this run (e.g. far-future games)
    for gid, prev in state.items():
        if not gid.startswith("_"):
            new_state.setdefault(gid, prev)

    if new_no_match_alerts:
        new_state["_no_match_alerts"] = new_no_match_alerts

    save_state(new_state)
    print(f"\nDone. {notifications} notification(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
