#!/usr/bin/env python3
"""Patch OpenTAKServer 1.7.13 for isolated Airsoft game teams.

Run with the Python interpreter from the OpenTAKServer 1.7.13 virtualenv.

For authenticated users in exactly one reserved IN group:
  GameRed  -> outgoing __group name="Red",  role="Team Member"
  GameBlue -> outgoing __group name="Blue", role="Team Member"

V3 also fixes device-UID reuse. OTS normally reuses the RabbitMQ UID queue,
which can retain bindings from a previous account/team. For a game account,
the UID queue is deleted and recreated before current group bindings are
applied. This removes stale GameRed/GameBlue/__ANON__/normal-group bindings.

All non-game accounts keep the original OTS behavior.
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
MARKER_V3 = "# AIRSOFT_GAME_TEAM_OVERRIDE_V3"

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

HELPER_V3 = """    # AIRSOFT_GAME_TEAM_OVERRIDE_V3
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

QUEUE_OLD = """                    self.rabbit_channel.queue_declare(queue=self.callsign)
                    self.rabbit_channel.queue_declare(queue=self.uid)

                    with self.app.app_context():
"""

QUEUE_NEW = """                    self.rabbit_channel.queue_declare(queue=self.callsign)

                    # A physical device can move from a normal account to a game
                    # account, or from GameRed to GameBlue, while keeping the same
                    # TAK UID. RabbitMQ queues survive that switch and therefore
                    # retain old bindings. For game users, recreate only the UID
                    # queue before applying the current OTS OUT memberships.
                    if self.airsoft_game_team:
                        self.logger.warning(
                            "Resetting stale group bindings for game EUD %s (%s)",
                            self.uid,
                            self.user.username,
                        )
                        # Declare first so queue_delete cannot fail because a new
                        # device has no pre-existing queue.
                        self.rabbit_channel.queue_declare(queue=self.uid)
                        self.rabbit_channel.queue_delete(queue=self.uid)

                    self.rabbit_channel.queue_declare(queue=self.uid)

                    with self.app.app_context():
"""


def locate_target() -> Path:
    spec = importlib.util.find_spec("opentakserver.eud_handler.EudHandler")
    if spec is None or not spec.origin:
        raise RuntimeError(
            "Could not locate opentakserver.eud_handler.EudHandler. "
            "Run this script with the OpenTAKServer 1.7.13 virtualenv Python."
        )
    return Path(spec.origin).resolve()


def install_core(source: str) -> str:
    if SETUP_NEW not in source:
        if SETUP_OLD not in source:
            raise RuntimeError("Expected OTS 1.7.13 setup() pattern not found.")
        source = source.replace(SETUP_OLD, SETUP_NEW, 1)

    if "_apply_airsoft_game_team" not in source:
        if HELPER_ANCHOR not in source:
            raise RuntimeError("Expected OTS 1.7.13 handle_cot() pattern not found.")
        source = source.replace(HELPER_ANCHOR, HELPER_V3 + HELPER_ANCHOR, 1)

    if "self._apply_airsoft_game_team(event)" not in source:
        if CALL_OLD not in source:
            raise RuntimeError("Expected OTS 1.7.13 handle_cot call site not found.")
        source = source.replace(CALL_OLD, CALL_NEW, 1)

    return source


def upgrade_marker_and_logging(source: str) -> str:
    source = source.replace(V1_LATE_INIT, "", 1)
    source = source.replace(MARKER_V1, MARKER_V3, 1)
    source = source.replace(MARKER_V2, MARKER_V3, 1)
    source = source.replace(
        '                self.logger.info(\n                    "Airsoft game team override for %s: %s",',
        '                self.logger.warning(\n                    "Airsoft game team override for %s: %s",',
        1,
    )
    return source


def install_queue_reset(source: str) -> str:
    if "Resetting stale group bindings for game EUD" in source:
        return source
    if QUEUE_OLD not in source:
        raise RuntimeError(
            "Expected OTS 1.7.13 UID queue declaration block not found. "
            "Refusing to modify the file."
        )
    return source.replace(QUEUE_OLD, QUEUE_NEW, 1)


def main() -> int:
    target = locate_target()
    source = target.read_text(encoding="utf-8")
    print(f"Target: {target}")

    if MARKER_V3 in source and "Resetting stale group bindings for game EUD" in source:
        print("V3 patch is already installed; no changes made.")
        return 0

    had_v1 = MARKER_V1 in source
    had_v2 = MARKER_V2 in source

    patched = install_core(source)
    patched = upgrade_marker_and_logging(patched)
    patched = install_queue_reset(patched)

    # A fresh install_core() inserted the V3 marker already. Older installs are
    # upgraded above.
    if MARKER_V3 not in patched:
        raise RuntimeError("V3 marker missing after patch construction.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(target.name + f".bak-airsoft-team-{stamp}")
    shutil.copy2(target, backup)

    try:
        target.write_text(patched, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise

    if had_v2:
        action = "Upgraded V2 -> V3"
    elif had_v1:
        action = "Upgraded V1 -> V3"
    else:
        action = "Installed V3"

    print(f"Backup: {backup}")
    print(f"{action}; Python compile check passed.")
    print("Restart opentakserver-1713.service and reconnect game devices.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
