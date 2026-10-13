#!/usr/bin/env python3
"""Make fresh authenticated enrollment authoritative for EUD ownership.

Airsoft operators intentionally reuse the same physical ATAK/iTAK device with
multiple valid OTS accounts. This upgrade changes the EUD ownership rule:

- a successful certificate enrollment with valid credentials may move that EUD
  to the authenticated username;
- CSR CN must still match that authenticated username;
- username-bound enrollment-token validation remains enforced;
- normal stream reconnects may NOT move ownership, so an old saved server/cert
  cannot steal the EUD back after a fresh registration;
- when leaving CORood/COBlauw, the CO-only device profile is removed;
- only one current Certificate DB row is retained per EUD to avoid ambiguous
  uselist=False relationships after repeated switches.

This preserves identity binding while allowing intentional account switching.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_AUTHENTICATED_EUD_SWITCH_V1"
HANDOVER_MARKER = "# AIRSOFT_CO_DEVICE_HANDOVER_V2"
RESTORE_CLEANUP_MARKER = "# AIRSOFT_CO_DEVICE_RESTORE_CLEANUP_V1"
EUD_HANDLER_MARKER = "# AIRSOFT_AUTHENTICATED_EUD_STREAM_GUARD_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def backup(path: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak-{label}-{stamp}")
    shutil.copy2(path, dest)
    return dest


def patch_enrollment(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False

    if HANDOVER_MARKER not in source:
        raise RuntimeError(
            "Permanent CO device handover patch must be installed before authenticated switching."
        )
    if RESTORE_CLEANUP_MARKER not in source:
        raise RuntimeError(
            "CO restore cleanup patch must be installed before authenticated switching."
        )

    start_anchor = "            if eud.user_id and eud.user_id != user.id:\n"
    end_anchor = "            if user and not eud.user_id:\n"
    start = source.find(start_anchor)
    if start < 0:
        raise RuntimeError(f"EUD ownership start anchor missing: {path}")
    end = source.find(end_anchor, start)
    if end < 0:
        raise RuntimeError(f"EUD ownership end anchor missing: {path}")

    switch_block = '''            if eud.user_id and eud.user_id != user.id:
                # AIRSOFT_AUTHENTICATED_EUD_SWITCH_V1
                previous_username = eud.user.username if eud.user else ""
                requested_username = str(user.username or "")
                authenticated_username = str(
                    getattr(g, "airsoft_enrollment_username", "") or ""
                )

                # basic_auth() has already validated either the real password or
                # a username-bound enrollment token, and CSR CN binding is
                # checked above. Refuse only if that authenticated identity is
                # somehow inconsistent with the target User row.
                if (
                    not authenticated_username
                    or authenticated_username.casefold()
                    != requested_username.casefold()
                ):
                    logger.warning(
                        "Rejected EUD switch for %s: authenticated=%s requested=%s",
                        uid,
                        authenticated_username or "<none>",
                        requested_username or "<none>",
                    )
                    return jsonify({"error": "Authenticated user mismatch"}), 403

                logger.warning(
                    "Authenticated enrollment reassigning EUD %s from %s to %s",
                    uid,
                    previous_username or "<unknown>",
                    requested_username,
                )

                # If a device leaves a CO identity, stop publishing the forced
                # CO callsign/team/HQ preferences. Its normal TAK configuration
                # can then take effect again.
                if (
                    _airsoft_is_co_username(previous_username)
                    and not _airsoft_is_co_username(requested_username)
                ):
                    _airsoft_clear_co_profiles(eud)

                # Legacy owner-bookkeeping is no longer required once fresh
                # authenticated enrollment itself is authoritative.
                _airsoft_clear_original_owner(eud)

                eud.user_id = user.id
                eud.last_status = "Disconnected"
                db.session.add(eud)
                db.session.commit()
'''
    source = source[:start] + switch_block + source[end:]

    cert_anchor = '''        if uid and user:
            _airsoft_apply_co_profile(user.username, eud)

        try:
            certificate = Certificate()
'''
    cert_replacement = '''        if uid and user:
            _airsoft_apply_co_profile(user.username, eud)

            # Repeated account switches previously accumulated Certificate rows
            # even though EUD.certificate is declared uselist=False. Keep only
            # the certificate metadata created by the newest enrollment.
            db.session.query(Certificate).filter(
                Certificate.eud_uid == uid
            ).delete(synchronize_session=False)
            db.session.commit()

        try:
            certificate = Certificate()
'''
    if cert_anchor not in source:
        raise RuntimeError(f"certificate cleanup anchor missing: {path}")
    source = source.replace(cert_anchor, cert_replacement, 1)

    path.write_text(source, encoding="utf-8")
    return True


def patch_eud_handler(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if EUD_HANDLER_MARKER in source:
        return False

    password_old = '''                        current_owner = eud.user.username if eud.user else ""
                        if (
                            str(current_owner or "").casefold() in ("corood", "coblauw")
                            and str(user.username or "").casefold()
                            != str(current_owner or "").casefold()
                        ):
                            self.logger.warning(
                                "Blocked stale credential connection for EUD %s: CO owner=%s incoming=%s",
                                uid,
                                current_owner,
                                user.username,
                            )
                            self.close_connection()
                            return
                        eud.user_id = user.id
                        db.session.commit()
'''
    password_new = '''                        current_owner = eud.user.username if eud.user else ""
                        # AIRSOFT_AUTHENTICATED_EUD_STREAM_GUARD_V1
                        # Ownership changes happen only during fresh certificate
                        # enrollment. A saved/old streaming login must never
                        # overwrite the account selected by the latest registration.
                        if (
                            current_owner
                            and str(user.username or "").casefold()
                            != str(current_owner).casefold()
                        ):
                            self.logger.warning(
                                "Blocked stale credential connection for EUD %s: owner=%s incoming=%s",
                                uid,
                                current_owner,
                                user.username,
                            )
                            self.close_connection()
                            return
                        eud.user_id = user.id
                        db.session.commit()
'''
    if password_old not in source:
        raise RuntimeError(f"password stream guard anchor missing: {path}")
    source = source.replace(password_old, password_new, 1)

    cot_old = '''                current_owner = eud.user.username if eud.user else ""
                incoming_owner = self.user.username if self.user else ""
                if (
                    str(current_owner or "").casefold() in ("corood", "coblauw")
                    and incoming_owner
                    and str(incoming_owner).casefold() != str(current_owner).casefold()
                ):
                    self.logger.warning(
                        "Blocked stale connection reclaim for EUD %s: CO owner=%s incoming=%s",
                        uid,
                        current_owner,
                        incoming_owner,
                    )
                    self.close_connection()
                    return

                eud.user_id = self.user.id if self.user else None
'''
    cot_new = '''                current_owner = eud.user.username if eud.user else ""
                incoming_owner = self.user.username if self.user else ""
                if (
                    current_owner
                    and incoming_owner
                    and str(incoming_owner).casefold() != str(current_owner).casefold()
                ):
                    self.logger.warning(
                        "Blocked stale connection reclaim for EUD %s: owner=%s incoming=%s",
                        uid,
                        current_owner,
                        incoming_owner,
                    )
                    self.close_connection()
                    return

                eud.user_id = self.user.id if self.user else None
'''
    if cot_old not in source:
        raise RuntimeError(f"certificate stream guard anchor missing: {path}")
    source = source.replace(cot_old, cot_new, 1)

    path.write_text(source, encoding="utf-8")
    return True


def main() -> int:
    root = package_root()
    enrollment = root / "blueprints" / "marti_api" / "certificate_enrollment_api.py"
    eud_handler = root / "eud_handler" / "EudHandler.py"

    for path in (enrollment, eud_handler):
        if not path.is_file():
            raise RuntimeError(f"Missing OTS source file: {path}")

    backups = []
    changed = []

    try:
        if MARKER not in enrollment.read_text(encoding="utf-8"):
            backups.append((enrollment, backup(enrollment, "authenticated-eud-switch")))
        if patch_enrollment(enrollment):
            changed.append(enrollment)

        if EUD_HANDLER_MARKER not in eud_handler.read_text(encoding="utf-8"):
            backups.append((eud_handler, backup(eud_handler, "authenticated-stream-guard")))
        if patch_eud_handler(eud_handler):
            changed.append(eud_handler)

        py_compile.compile(str(enrollment), doraise=True)
        py_compile.compile(str(eud_handler), doraise=True)
    except Exception:
        for path, saved in backups:
            shutil.copy2(saved, path)
        raise

    if changed:
        print("Installed authenticated EUD account switching patch.")
        for path in changed:
            print(f"Patched: {path}")
    else:
        print("Authenticated EUD account switching patch already installed.")

    print("Python compile checks passed.")
    for _, saved in backups:
        print(f"Backup: {saved}")
    print("Restart OpenTAKServer before testing account switching.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
