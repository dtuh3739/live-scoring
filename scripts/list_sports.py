"""
List every sport key currently exposed by The Odds API.

Run this once locally to discover the right sport keys to put in
config/games.json — particularly to confirm whether rugby union
(or your specific competition) is supported.

Usage:
    ODDS_API_KEY=xxxxxxxx python scripts/list_sports.py
"""

import os
import sys

import requests

API_KEY = os.environ.get("ODDS_API_KEY")
if not API_KEY:
    print("Set ODDS_API_KEY in your environment first.", file=sys.stderr)
    sys.exit(1)

r = requests.get(
    "https://api.the-odds-api.com/v4/sports/",
    params={"apiKey": API_KEY, "all": "true"},
    timeout=15,
)
r.raise_for_status()

sports = sorted(r.json(), key=lambda s: (s["group"], s["key"]))

current_group = None
for s in sports:
    if s["group"] != current_group:
        current_group = s["group"]
        print(f"\n=== {current_group} ===")
    active = "✓" if s["active"] else " "
    has_outrights = " (outrights only)" if s.get("has_outrights") else ""
    print(f"  [{active}] {s['key']:45s} {s['title']}{has_outrights}")

print(f"\nQuota used: {r.headers.get('x-requests-used', '?')} / "
      f"remaining: {r.headers.get('x-requests-remaining', '?')}")
print("(Note: /sports endpoint does not count against quota)")
