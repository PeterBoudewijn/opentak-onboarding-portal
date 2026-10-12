#!/usr/bin/env python3
"""Patch OpenTAKServer 1.7.13 for isolated Airsoft game teams.

This patch adds four narrowly-scoped fixes:

1. Game team color enforcement
   GameRed  -> outgoing __group name="Red"
   GameBlue -> outgoing __group name="Blue"

2. Per-device RabbitMQ group-binding reset
   When an authenticated SSL EUD reconnects, remove every old group binding
   for that device UID before binding the OUT groups of the currently
   authenticated user.

3. Stale per-device queue cleanup on account changes
   If the same device UID authenticates as a different OTS user, purge
   already-queued messages belonging to the previous identity.

4. OTS 1.7.13 disconnect binding bug
   cot_parser.send_disconnect_cot() logs that it unbound the EUD queue, but
   actually calls queue_bind(). This can asynchronously resurrect a previous
   account's group after the device has already logged in with another account.
   EudHandler owns the binding lifecycle, so cot_parser must not re-bind it.

The script is idempotent, creates timestamped backups, and byte-compiles all
modified modules before reporting success.
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
MARKER_V4 = "# AIRSOFT_GAME_TEAM_OVERRIDE_V4"
COT_MARKER = "# AIRSOFT_GAME_DISCONNECT_BIND_FIX_V1"

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

TEAM_HELPER_V4 = """    # AIRSOFT_GAME_TEAM_OVERRIDE_V4
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
        \"\"\"Drop stale group bindings and queued data from a previous identity.\"\"\"
        if not self.is_ssl or not self.uid or not self.rabbit_channel:
            return

        previous_user_id = None
        existing_eud_row = db.session.execute(
            select(EUD).filter_by(uid=self.uid)
        ).first()
        if existing_eud_row and existing_eud_row[0]:
            previous_user_id = existing_eud_row[0].user_id

        current_user_id = self.user.id if self.user else None
        identity_changed = (
            previous_user_id is not None
            and current_user_id is not None
            and previous_user_id != current_user_id
        )

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

        if identity_changed:
            self.rabbit_channel.queue_purge(queue=self.uid)
            self.logger.warning(
                "Purged stale RabbitMQ UID queue for %s after account change %s -> %s",
                self.uid,
                previous_user_id,
                current_user_id,
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

COT_BAD_BIND = """                        self.rabbit_channel.queue_bind(
                            queue=uid,
                            exchange="groups",
                            routing_key=f"{group.group.name}.{group.direction}",
                        )

                        self.logger.debug(
                            f"Unbound {uid} from {group.group.name}.{group.direction}"
                        )
"""

COT_FIXED_BIND = """                        # AIRSOFT_GAME_DISCONNECT_BIND_FIX_V1
                        # EudHandler owns the UID queue binding lifecycle. An
                        # asynchronous disconnect must not re-bind an old group
                        # after the same UID has already logged in elsewhere.
                        self.logger.debug(
                            f"Disconnect routed for {uid} in {group.group.name}.{group.direction}"
                        )
"""


def locate_module(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise RuntimeError(
            f"Could not locate {module_name}. "
            "Run this script with the OpenTAKServer 1.7.13 virtualenv Python."
        )
    return Path(spec.origin).resolve()


def replace_existing_helper(source: str) -> str:
    start = -1
    for marker in (MARKER_V1, MARKER_V2, MARKER_V3):
        pos = source.find("    " + marker)
        if pos >= 0:
            start = pos
            break

    if start < 0:
        if HELPER_ANCHOR not in source:
            raise RuntimeError("Could not find handle_cot() insertion point.")
        return source.replace(HELPER_ANCHOR, TEAM_HELPER_V4 + HELPER_ANCHOR, 1)

    end = source.find(HELPER_ANCHOR, start)
    if end < 0:
        raise RuntimeError("Could not find end of existing Airsoft helper block.")

    return source[:start] + TEAM_HELPER_V4 + source[end:]


def patch_eud_handler(source: str) -> str:
    if MARKER_V4 in source and BIND_PATCHED in source and SETUP_V3 in source:
        return source

    source = source.replace(V1_LATE_INIT, "", 1)

    if SETUP_V3 not in source:
        if SETUP_V2 in source:
            source = source.replace(SETUP_V2, SETUP_V3, 1)
        elif SETUP_BASE in source:
            source = source.replace(SETUP_BASE, SETUP_V3, 1)
        else:
            raise RuntimeError("Expected OTS 1.7.13 EudHandler.setup() pattern not found.")

    source = replace_existing_helper(source)

    if CALL_PATCHED not in source:
        if CALL_BASE not in source:
            raise RuntimeError("Expected OTS 1.7.13 handle_cot call site not found.")
        source = source.replace(CALL_BASE, CALL_PATCHED, 1)

    if BIND_PATCHED not in source:
        if BIND_ANCHOR not in source:
            raise RuntimeError("Expected OTS 1.7.13 SSL group-binding block not found.")
        source = source.replace(BIND_ANCHOR, BIND_PATCHED, 1)

    return source


def patch_cot_parser(source: str) -> str:
    if COT_MARKER in source:
        return source
    if COT_BAD_BIND not in source:
        raise RuntimeError(
            "Expected OTS 1.7.13 cot_parser disconnect queue_bind() pattern not found."
        )
    return source.replace(COT_BAD_BIND, COT_FIXED_BIND, 1)


def main() -> int:
    eud_target = locate_module("opentakserver.eud_handler.EudHandler")
    cot_target = locate_module("opentakserver.cot_parser.cot_parser")

    eud_source = eud_target.read_text(encoding="utf-8")
    cot_source = cot_target.read_text(encoding="utf-8")

    eud_patched = patch_eud_handler(eud_source)
    cot_patched = patch_cot_parser(cot_source)

    print(f"EudHandler: {eud_target}")
    print(f"cot_parser:  {cot_target}")

    if eud_patched == eud_source and cot_patched == cot_source:
        print("V5 patch is already installed; no changes made.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = {}

    try:
        for target, old_source, new_source in (
            (eud_target, eud_source, eud_patched),
            (cot_target, cot_source, cot_patched),
        ):
            if new_source == old_source:
                continue

            backup = target.with_name(target.name + f".bak-airsoft-team-{stamp}")
            shutil.copy2(target, backup)
            backups[target] = backup
            target.write_text(new_source, encoding="utf-8")
            py_compile.compile(str(target), doraise=True)
            print(f"Backup: {backup}")

    except Exception:
        for target, backup in backups.items():
            shutil.copy2(backup, target)
        raise

    print("Installed V5; Python compile checks passed.")
    print("Restart opentakserver-1713.service and reconnect all test devices.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
