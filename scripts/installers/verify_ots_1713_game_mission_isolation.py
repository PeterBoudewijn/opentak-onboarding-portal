#!/usr/bin/env python3
"""Verify strict GameRed/GameBlue mission isolation in OpenTAKServer 1.7.13."""

from __future__ import annotations

import sys

from opentakserver.app import create_app
from opentakserver.extensions import db
from opentakserver.models.Group import Group
from opentakserver.models.GroupMission import GroupMission
from opentakserver.models.GroupUser import GroupUser
from opentakserver.models.Mission import Mission


EXPECTED = {
    "CORood": {"GameRed": True, "GameBlue": False},
    "COBlauw": {"GameRed": False, "GameBlue": True},
    "3DivisionGameMaster": {"GameRed": True, "GameBlue": True},
    "3DAdmin": {"GameRed": True, "GameBlue": True},
}


def main() -> int:
    app = create_app(cli=True)

    with app.app_context():
        from opentakserver.blueprints.marti_api.mission_marti_api import (
            _airsoft_user_can_access_game_mission,
        )

        print("=== MISSION GROUP ASSOCIATIONS ===")
        association_ok = True
        for mission_name, expected_group in (
            ("GameRed", "GameRed"),
            ("GameBlue", "GameBlue"),
        ):
            mission = db.session.execute(
                db.session.query(Mission).filter_by(name=mission_name)
            ).scalar()
            if not mission:
                print(f"FAIL {mission_name}: mission missing")
                association_ok = False
                continue

            group_names = {
                row.group.name
                for row in db.session.execute(
                    db.session.query(GroupMission).filter_by(
                        mission_name=mission_name
                    )
                ).scalars()
                if row.group
            }
            ok = group_names == {expected_group}
            print(
                f"{'PASS' if ok else 'FAIL'} {mission_name}: "
                f"groups={sorted(group_names)} expected={[expected_group]}"
            )
            association_ok = association_ok and ok

        print()
        print("=== ACCESS MATRIX ===")
        access_ok = True

        for username, expectations in EXPECTED.items():
            user = app.security.datastore.find_user(username=username)
            if not user:
                print(f"FAIL {username}: user missing")
                access_ok = False
                continue

            memberships = sorted(
                {
                    f"{membership.group.name}:{membership.direction}"
                    for membership in db.session.execute(
                        db.session.query(GroupUser).filter_by(
                            user_id=user.id,
                            enabled=True,
                        )
                    ).scalars()
                    if membership.group
                }
            )
            print(f"{username}: memberships={memberships}")

            for mission_name, expected in expectations.items():
                actual = _airsoft_user_can_access_game_mission(
                    user,
                    mission_name,
                )
                ok = actual is expected
                print(
                    f"  {'PASS' if ok else 'FAIL'} {mission_name}: "
                    f"actual={actual} expected={expected}"
                )
                access_ok = access_ok and ok

        print()
        if association_ok and access_ok:
            print("RESULT: PASS - strict red/blue mission isolation is configured correctly.")
            return 0

        print("RESULT: FAIL - mission isolation needs correction.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
