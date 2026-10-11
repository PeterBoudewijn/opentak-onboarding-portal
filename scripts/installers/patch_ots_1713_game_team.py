#!/usr/bin/env python3
"""Patch OpenTAKServer 1.7.13 for isolated Airsoft game teams.

This patch adds two narrowly-scoped fixes:

1. Game team color enforcement
   GameRed  -> outgoing __group name="Red"
   GameBlue -> outgoing __group name="Blue"

2. Per-device RabbitMQ group-binding reset
   When an authenticated SSL EUD reconnects, remove every old group binding
   for that device UID before binding the OUT groups of the currently
   authenticated user. This prevents a reused phone/tablet UID from retaining
   GameRed, GameBlue, __ANON__, or normal-group subscriptions from a previous
   login.

The script is idempotent, creates a timestamped backup, and byte-compiles the
modified EudHandler.py before reporting success.
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

SETUP_BASE = """    def setup(self):
        self.create_app()
"""

SETUP_V2 = """    def setup(self):
        # BaseRequestHandler calls setup()/handle() from inside super().__init__().
        # Initialize game state here, before the first CoT can be processed.
        self.airsoft_game_team = None
        self.airsoft_game_team_checked = False
        self.create_app()
"""

SETUP_V3 = """    def setup(self):
        # BaseRequestHandler calls setup()/handle() from inside super().__init__().
        # Keep all mutable connection state per handler instance.
        self.airsoft_game_team = None
        self.airsoft_game_team_checked = False
        self.bound_queues = []
        self.group_memberships = []
        self.create_app()
"""

V1_LATE_INIT = """        # Per-connection state for the game-only team override.
        self.airsoft_game_team = None
        self.airsoft_game_team_checked = False
"""

HELPER_ANCHOR = """    def handle_cot(self, cot):
"""

TEAM_HELPER_V3 = """    # AIRSOFT_GAME_TEAM_OVERRIDE_V3
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

    def _reset_ssl_group_bindings(self):
        \"\"\"Drop stale group bindings for this UID before applying current memberships.\"\"\"
        if not self.is_ssl or not self.uid or not self.rabbit_channel:
            return

        # A durable/non-exclusive queue can survive an account/team change for
        # the same device UID. Remove all known group routing keys so the queue
        # cannot retain subscriptions from a previous authenticated user.
        groups = db.session.execute(db.session.query(Group)).all()
        routing_keys = {"__ANON__.IN", "__ANON__.OUT"}

        for row in groups:
            group = row[0]
            if group and group.name:
                routing_keys.add(f"{group.name}.IN")
                routing_keys.add(f"{group.name}.OUT")

        for routing_key in routing_keys:
            self.rabbit_channel.queue_unbind(
                exchange="groups",
                queue=self.uid,
                routing_key=routing_key,
            )

        self.logger.warning(
            "Reset RabbitMQ group bindings for %s (%s)",
            self.uid,
            self.user.username if self.user else "unknown",
        )

"""

CALL_BASE = """        if self.pong(event):
            return

        if event and not self.uid:
"""

CALL_PATCHED = """        if self.pong(event):
            return

        self._apply_airsoft_game_team(event)

        if event and not self.uid:
"""

BIND_ANCHOR = """                        if self.is_ssl:
                            group_memberships = db.session.execute(
"""

BIND_PATCHED = """                        if self.is_ssl:
                            self._reset_ssl_group_bindings()
                            group_memberships = db.session.execute(
"""


def locate_target() -> Path:
    spec = importlib.util.find_spec("opentakserver.eud_handler.EudHandler")
    if spec is None or not spec.origin:
        raise RuntimeError(
            "Could not locate opentakserver.eud_handler.EudHandler. "
            "Run this script with the OpenTAKServer 1.7.13 virtualenv Python."
        )
    return Path(spec.origin).resolve()


def replace_existing_helper(source: str) -> str:
    start = -1
    for marker in (MARKER_V1, MARKER_V2):
        pos = source.find("    " + marker)
        if pos >= 0:
            start = pos
            break

    if start < 0:
        if HELPER_ANCHOR not in source:
            raise RuntimeError("Could not find handle_cot() insertion point.")
        return source.replace(HELPER_ANCHOR, TEAM_HELPER_V3 + HELPER_ANCHOR, 1)

    end = source.find(HELPER_ANCHOR, start)
    if end < 0:
        raise RuntimeError("Could not find end of existing Airsoft helper block.")

    return source[:start] + TEAM_HELPER_V3 + source[end:]


def main() -> int:
    target = locate_target()
    source = target.read_text(encoding="utf-8")
    print(f"Target: {target}")

    if MARKER_V3 in source and BIND_PATCHED in source and SETUP_V3 in source:
        print("V3 patch is already installed; no changes made.")
        return 0

    # Repair the V1 initialization bug if that old patch is encountered.
    source = source.replace(V1_LATE_INIT, "", 1)

    if SETUP_V3 not in source:
        if SETUP_V2 in source:
            source = source.replace(SETUP_V2, SETUP_V3, 1)
        elif SETUP_BASE in source:
            source = source.replace(SETUP_BASE, SETUP_V3, 1)
        else:
            raise RuntimeError("Expected OTS 1.7.13 setup() pattern not found.")

    source = replace_existing_helper(source)

    if CALL_PATCHED not in source:
        if CALL_BASE not in source:
            raise RuntimeError("Expected OTS 1.7.13 handle_cot call site not found.")
        source = source.replace(CALL_BASE, CALL_PATCHED, 1)

    if BIND_PATCHED not in source:
        if BIND_ANCHOR not in source:
            raise RuntimeError("Expected OTS 1.7.13 SSL group-binding block not found.")
        source = source.replace(BIND_ANCHOR, BIND_PATCHED, 1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(target.name + f".bak-airsoft-team-{stamp}")
    shutil.copy2(target, backup)

    try:
        target.write_text(source, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise

    print(f"Backup: {backup}")
    print("Installed V3; Python compile check passed.")
    print("Restart opentakserver-1713.service and reconnect game devices.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
