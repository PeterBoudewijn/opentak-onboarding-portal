#!/usr/bin/env python3
"""Synchronize permanent GameRed/GameBlue observer memberships immediately.

This is used by deployment after updating the portal .env so the production
state is correct before the post-deploy verifier runs. It starts the Flask app
in TESTING mode so the scheduler itself is not started by this one-shot helper.
"""

from __future__ import annotations

import os
import sys

os.environ["TESTING"] = "True"

from app import app
from app.api_v1.game import GAME_TEAMS, _ensure_permanent_team_memberships


def main() -> int:
    failures = []

    with app.app_context():
        for team_key in GAME_TEAMS:
            result = _ensure_permanent_team_memberships(team_key)
            team_failures = result.get("failures") or []
            failures.extend(
                {"team": team_key, **item}
                for item in team_failures
                if isinstance(item, dict)
            )
            print(
                f"{team_key}: added={len(result.get('added') or [])} "
                f"removed={len(result.get('removed') or [])} "
                f"failures={len(team_failures)}"
            )

    if failures:
        print("Permanent observer synchronization failed:", file=sys.stderr)
        for item in failures:
            print(
                f"  team={item.get('team')} "
                f"user={item.get('username')} "
                f"direction={item.get('direction')} "
                f"action={item.get('action')} "
                f"error={item.get('error')}",
                file=sys.stderr,
            )
        return 1

    print("Permanent GameRed/GameBlue memberships synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
