#!/usr/bin/env python3
"""Seed the original owner marker for a device that became CO before handover V2.

This does not reassign the EUD. It only records which normal OTS user may later
re-enroll the device away from CORood/COBlauw.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from opentakserver.app import create_app
from opentakserver.extensions import db
from opentakserver.models.DeviceProfiles import DeviceProfiles
from opentakserver.models.EUD import EUD
from opentakserver.models.user import User

KEY = "airsoftOriginalUsername"
CO_NAMES = {"corood", "coblauw"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()

    app = create_app(cli=True)

    with app.app_context():
        eud = db.session.query(EUD).filter_by(uid=args.uid).first()
        if not eud:
            raise SystemExit(f"STOP: EUD {args.uid} not found")

        current = eud.user.username if eud.user else ""
        if str(current).casefold() not in CO_NAMES:
            raise SystemExit(
                f"STOP: EUD current owner is {current or '<none>'}; expected CORood/COBlauw"
            )

        owner = db.session.query(User).filter_by(username=args.owner).first()
        if not owner:
            raise SystemExit(f"STOP: original owner {args.owner} does not exist")
        if str(owner.username).casefold() in CO_NAMES:
            raise SystemExit("STOP: original owner may not be a CO account")

        profile = db.session.query(DeviceProfiles).filter_by(
            preference_key=KEY,
            eud_uid=eud.uid,
        ).first()
        if not profile:
            profile = DeviceProfiles(preference_key=KEY, eud_uid=eud.uid)

        profile.preference_value = owner.username
        profile.value_class = "class java.lang.String"
        profile.enrollment = False
        profile.connection = False
        profile.active = False
        profile.publish_time = datetime.now(timezone.utc)
        db.session.add(profile)
        db.session.commit()

        print(f"EUD: {eud.uid}")
        print(f"Current CO owner: {current}")
        print(f"Recorded original owner: {owner.username}")
        print("PASS: the original owner may now re-enroll this device.")


if __name__ == "__main__":
    raise SystemExit(main())
