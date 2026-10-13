#!/usr/bin/env python3
"""Verify GameRed/GameBlue hash-addressed MissionContent isolation in OTS 1.7.13."""

from __future__ import annotations

from opentakserver.app import create_app
from opentakserver.extensions import db
from opentakserver.models.MissionContent import MissionContent
from opentakserver.models.MissionContentMission import MissionContentMission


EXPECTED = {
    "CORood": {
        frozenset({"GameRed"}): True,
        frozenset({"GameBlue"}): False,
        frozenset({"GameRed", "GameBlue"}): True,
    },
    "COBLauw": {
        frozenset({"GameRed"}): False,
        frozenset({"GameBlue"}): True,
        frozenset({"GameRed", "GameBlue"}): True,
    },
    "3DivisionGameMaster": {
        frozenset({"GameRed"}): True,
        frozenset({"GameBlue"}): True,
        frozenset({"GameRed", "GameBlue"}): True,
    },
    "3DAdmin": {
        frozenset({"GameRed"}): True,
        frozenset({"GameBlue"}): True,
        frozenset({"GameRed", "GameBlue"}): True,
    },
}


def main() -> int:
    app = create_app(cli=True)

    with app.app_context():
        from opentakserver.blueprints.marti_api.data_package_marti_api import (
            _airsoft_user_can_access_reserved_content,
            _airsoft_user_can_access_reserved_content_missions,
        )

        print("=== HASH CONTENT ACCESS MATRIX ===")
        access_ok = True

        for username, cases in EXPECTED.items():
            user = app.security.datastore.find_user(username=username)
            if not user:
                print(f"FAIL {username}: user missing")
                access_ok = False
                continue

            for mission_names, expected in cases.items():
                actual = _airsoft_user_can_access_reserved_content_missions(
                    user,
                    set(mission_names),
                )
                ok = actual is expected
                print(
                    f"{'PASS' if ok else 'FAIL'} {username} "
                    f"missions={sorted(mission_names)} "
                    f"actual={actual} expected={expected}"
                )
                access_ok = access_ok and ok

        print()
        print("=== EXISTING RESERVED CONTENT ===")
        content_ok = True
        associations = db.session.execute(
            db.session.query(MissionContentMission).filter(
                MissionContentMission.mission_name.in_(("GameRed", "GameBlue"))
            )
        ).scalars()

        content_ids = sorted({row.mission_content_id for row in associations})
        if not content_ids:
            print("INFO no current MissionContent is associated with GameRed/GameBlue")
        else:
            for content_id in content_ids:
                content = db.session.get(MissionContent, content_id)
                if not content:
                    print(f"FAIL missing MissionContent id={content_id}")
                    content_ok = False
                    continue

                mission_names = {
                    row.mission_name
                    for row in db.session.execute(
                        db.session.query(MissionContentMission).filter_by(
                            mission_content_id=content.id
                        )
                    ).scalars()
                    if row.mission_name in ("GameRed", "GameBlue")
                }

                for username in ("CORood", "COBLauw"):
                    user = app.security.datastore.find_user(username=username)
                    expected = (
                        ("GameRed" in mission_names and username == "CORood")
                        or ("GameBlue" in mission_names and username == "COBLauw")
                    )
                    actual = _airsoft_user_can_access_reserved_content(user, content)
                    ok = actual is expected
                    print(
                        f"{'PASS' if ok else 'FAIL'} content={content.id} "
                        f"user={username} missions={sorted(mission_names)} "
                        f"actual={actual} expected={expected}"
                    )
                    content_ok = content_ok and ok

        print()
        if access_ok and content_ok:
            print(
                "RESULT: PASS - GameRed/GameBlue hash-addressed mission content "
                "is isolated correctly."
            )
            return 0

        print("RESULT: FAIL - hash-addressed mission content isolation needs correction.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
