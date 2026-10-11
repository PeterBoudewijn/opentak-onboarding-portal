#!/usr/bin/env python3
"""Apply the Airsoft GameRed/GameBlue team-color override to OpenTAKServer 1.7.13.

Run with the Python interpreter from the OpenTAKServer 1.7.13 virtualenv.
The script supports both a clean OTS 1.7.13 install and repairing the earlier
V1 version of this patch.

Only authenticated users with an enabled IN membership in exactly one
reserved group are changed:
  GameRed  -> __group name="Red",  role="Team Member"
  GameBlue -> __group name="Blue", role="Team Member"
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER_V1 = "# AIRSOFT_GAME_TEAM_OVERRIDE_V1"
MARKER_V2 = "# AIRSOFT_GAME_TEAM_OVERRIDE_V2"

V1_LATE_INIT = """        # Per-connection state for the game-only team override.
        self.airsoft_game_team = None
        self.airsoft_game_team_checked = False
"""

SETUP_OLD = """    def setup(self):
        self.create_app()
"""

SETUP_NEW = """    def setup(self):
        # BaseRequestHandler calls setup()/handle() from inside super().__init__().
        # Initialize game state here, before the first CoT can be processed.
        self.airsoft_game_team = None
        self.airsoft_game_team_checked = False
        self.create_app()
"""

HELPER_ANCHOR = """    def handle_cot(self, cot):
"""

HELPER = """    # AIRSOFT_GAME_TEAM_OVERRIDE_V2
    def _apply_airsoft_game_team(self, event):
        \"\"\"Force reserved game accounts to publish the configured team color.\"\"\"
        if not event or not self.is_ssl or not self.user:
            return

        if not self.airsoft_game_team_checked:
            with self.app.app_context():
                memberships = db.session.execute(
                    db.session.query(GroupUser).filter_by(
                        user_id=self.user.id,
                        direction=Group.IN,
                        enabled=True,
                    )
                ).all()

                group_names = {
                    membership[0].group.name
                    for membership in memberships
                    if membership and membership[0].group
                }

            reserved = [
                group_name
                for group_name in ("GameRed", "GameBlue")
                if group_name in group_names
            ]

            if len(reserved) == 1:
                self.airsoft_game_team = {
                    "GameRed": "Red",
                    "GameBlue": "Blue",
                }[reserved[0]]
                # WARNING is deliberate: visible even when OTS INFO logging is filtered.
                self.logger.warning(
                    "Airsoft game team override for %s: %s",
                    self.user.username,
                    self.airsoft_game_team,
                )
            elif len(reserved) > 1:
                self.logger.warning(
                    "Airsoft game team override skipped for %s: user belongs to both reserved groups",
                    self.user.username,
                )

            self.airsoft_game_team_checked = True

        if not self.airsoft_game_team:
            return

        group = event.find("__group")
        if group:
            group.attrs["name"] = self.airsoft_game_team
            group.attrs["role"] = "Team Member"
            return

        detail = event.find("detail")
        if detail:
            group = BeautifulSoup(
                '<__group role="Team Member" name="{}"/>'.format(self.airsoft_game_team),
                "xml",
            ).find("__group")
            detail.append(group)

"""

CALL_OLD = """        if self.pong(event):
            return

        if event and not self.uid:
"""

CALL_NEW = """        if self.pong(event):
            return

        self._apply_airsoft_game_team(event)

        if event and not self.uid:
"""


def locate_target() -> Path:
    spec = importlib.util.find_spec("opentakserver.eud_handler.EudHandler")
    if spec is None or not spec.origin:
        raise RuntimeError(
            "Could not locate opentakserver.eud_handler.EudHandler. "
            "Run this script with the OpenTAKServer 1.7.13 virtualenv Python."
        )
    return Path(spec.origin).resolve()


def patch_fresh(source: str) -> str:
    if SETUP_OLD not in source:
        raise RuntimeError("Expected OTS 1.7.13 setup() pattern not found.")
    if HELPER_ANCHOR not in source:
        raise RuntimeError("Expected OTS 1.7.13 handle_cot() pattern not found.")
    if CALL_OLD not in source:
        raise RuntimeError("Expected OTS 1.7.13 handle_cot call site not found.")

    source = source.replace(SETUP_OLD, SETUP_NEW, 1)
    source = source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    source = source.replace(CALL_OLD, CALL_NEW, 1)
    return source


def repair_v1(source: str) -> str:
    # V1 put these instance attributes after super().__init__(), but
    # BaseRequestHandler has already run handle() by then. Remove that late init.
    source = source.replace(V1_LATE_INIT, "", 1)

    if SETUP_NEW not in source:
        if SETUP_OLD not in source:
            raise RuntimeError("Could not find setup() while repairing V1 patch.")
        source = source.replace(SETUP_OLD, SETUP_NEW, 1)

    # Keep the already-installed helper/call, but upgrade its marker and make
    # the successful override visible at WARNING level.
    source = source.replace(MARKER_V1, MARKER_V2, 1)
    source = source.replace(
        '                self.logger.info(\n                    "Airsoft game team override for %s: %s",',
        '                self.logger.warning(\n                    "Airsoft game team override for %s: %s",',
        1,
    )
    return source


def main() -> int:
    target = locate_target()
    source = target.read_text(encoding="utf-8")
    print(f"Target: {target}")

    if MARKER_V2 in source:
        print("V2 patch is already installed; no changes made.")
        return 0

    if MARKER_V1 in source:
        patched = repair_v1(source)
        action = "Repaired V1 -> V2"
    else:
        patched = patch_fresh(source)
        action = "Installed V2"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(target.name + f".bak-airsoft-team-{stamp}")
    shutil.copy2(target, backup)

    try:
        target.write_text(patched, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise

    print(f"Backup: {backup}")
    print(f"{action}; Python compile check passed.")
    print("Restart opentakserver-1713.service to activate it.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
