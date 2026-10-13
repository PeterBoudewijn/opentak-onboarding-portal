#!/usr/bin/env python3
"""Make permanent airsoft CO enrollment work on already-known TAK devices.

This patch extends the existing OpenTAKServer 1.7.13 enrollment hardening:
- CORood/COBlauw may take over an existing EUD only when certificate enrollment
  authenticated with a valid username-bound enrollment token.
- A permanent CO password cannot be used to take over another EUD.
- The pre-CO owner is remembered internally so that owner can later re-enroll
  the same device back to its normal account.
- stale old connections cannot silently claim an EUD back from an active CO.
- concurrent CO certificate signing for the same common name is serialized.

The patch is idempotent, creates backups and compiles both modified modules.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

ENROLL_MARKER = "# AIRSOFT_CO_DEVICE_HANDOVER_V2"
EUD_HANDLER_MARKER = "# AIRSOFT_CO_DEVICE_OWNER_GUARD_V1"


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
    if ENROLL_MARKER in source:
        return False

    if "# AIRSOFT_CERT_ENROLLMENT_IDENTITY_BINDING_V1" not in source:
        raise RuntimeError(
            "Certificate enrollment security patch is required before CO device handover."
        )

    if "import fcntl\n" not in source:
        anchor = "import datetime\n"
        if anchor not in source:
            raise RuntimeError(f"datetime import anchor missing: {path}")
        source = source.replace(anchor, anchor + "import fcntl\n", 1)

    flask_anchor = "from flask import jsonify, request\n"
    if flask_anchor in source:
        source = source.replace(flask_anchor, "from flask import g, jsonify, request\n", 1)
    elif "from flask import g, jsonify, request\n" not in source:
        raise RuntimeError(f"Flask import anchor missing: {path}")

    eud_import = "from opentakserver.models.EUD import EUD\n"
    if "from opentakserver.models.DeviceProfiles import DeviceProfiles\n" not in source:
        if eud_import not in source:
            raise RuntimeError(f"EUD import anchor missing: {path}")
        source = source.replace(
            eud_import,
            eud_import + "from opentakserver.models.DeviceProfiles import DeviceProfiles\n",
            1,
        )

    function_anchor = "\n\n# require basic auth\n"
    if function_anchor not in source:
        raise RuntimeError(f"basic auth function anchor missing: {path}")

    helper = r'''

# AIRSOFT_CO_DEVICE_HANDOVER_V2
_AIRSOFT_CO_USERNAMES = {"corood", "coblauw"}
_AIRSOFT_ORIGINAL_OWNER_KEY = "airsoftOriginalUsername"


def _airsoft_is_co_username(username):
    return str(username or "").casefold() in _AIRSOFT_CO_USERNAMES


def _airsoft_original_owner_profile(eud):
    return db.session.query(DeviceProfiles).filter_by(
        preference_key=_AIRSOFT_ORIGINAL_OWNER_KEY,
        eud_uid=eud.uid,
    ).first()


def _airsoft_remember_original_owner(eud, previous_username):
    previous_username = str(previous_username or "")
    if not previous_username or _airsoft_is_co_username(previous_username):
        return

    profile = _airsoft_original_owner_profile(eud)
    if not profile:
        profile = DeviceProfiles(
            preference_key=_AIRSOFT_ORIGINAL_OWNER_KEY,
            eud_uid=eud.uid,
        )

    # Internal bookkeeping only; never publish this value to TAK clients.
    profile.preference_value = previous_username
    profile.value_class = "class java.lang.String"
    profile.enrollment = False
    profile.connection = False
    profile.active = False
    profile.publish_time = datetime.datetime.now(datetime.timezone.utc)
    db.session.add(profile)


def _airsoft_restore_allowed(eud, requested_username):
    current_username = eud.user.username if eud.user else ""
    if not _airsoft_is_co_username(current_username):
        return False

    profile = _airsoft_original_owner_profile(eud)
    if not profile or not profile.preference_value:
        return False

    return str(profile.preference_value).casefold() == str(requested_username or "").casefold()


def _airsoft_clear_original_owner(eud):
    profile = _airsoft_original_owner_profile(eud)
    if profile:
        db.session.delete(profile)


def _airsoft_sign_csr(cert_authority, csr_bytes, common_name):
    """Serialize same-CN CO signing because OTS 1.7.13 uses shared CN filenames."""
    if not _airsoft_is_co_username(common_name):
        return cert_authority.sign_csr(csr_bytes, common_name, False)

    lock_dir = os.path.join(app.config.get("OTS_CA_FOLDER"), ".airsoft-enrollment-locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, str(common_name).casefold() + ".lock")

    with open(lock_path, "a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            return cert_authority.sign_csr(csr_bytes, common_name, False)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
'''
    source = source.replace(function_anchor, helper + function_anchor, 1)

    old_auth = '''        if verify_password(password, user.password):
            return True

        # AIRSOFT_CERT_ENROLLMENT_IDENTITY_BINDING_V1
        # A token may only authenticate the username stored in its subject.
        return Token.verify_token(password, expected_username=username)
'''
    new_auth = '''        if verify_password(password, user.password):
            g.airsoft_enrollment_token_auth = False
            g.airsoft_enrollment_username = username
            return True

        # AIRSOFT_CERT_ENROLLMENT_IDENTITY_BINDING_V1
        # A token may only authenticate the username stored in its subject.
        token_ok = Token.verify_token(password, expected_username=username)
        g.airsoft_enrollment_token_auth = bool(token_ok)
        g.airsoft_enrollment_username = username if token_ok else None
        return token_ok
'''
    if old_auth not in source:
        raise RuntimeError(f"hardened basic auth anchor missing: {path}")
    source = source.replace(old_auth, new_auth, 1)

    start_anchor = "            if eud.user_id and eud.user_id != user.id:\n"
    end_anchor = "            if user and not eud.user_id:\n"
    start = source.find(start_anchor)
    if start < 0:
        raise RuntimeError(f"EUD ownership start anchor missing: {path}")
    end = source.find(end_anchor, start)
    if end < 0:
        raise RuntimeError(f"EUD ownership end anchor missing: {path}")

    handover_block = '''            if eud.user_id and eud.user_id != user.id:
                previous_username = eud.user.username if eud.user else ""
                requested_username = str(user.username or "")
                target_is_co = _airsoft_is_co_username(requested_username)
                token_authorized = bool(
                    getattr(g, "airsoft_enrollment_token_auth", False)
                    and str(getattr(g, "airsoft_enrollment_username", "") or "").casefold()
                    == requested_username.casefold()
                )
                restore_allowed = _airsoft_restore_allowed(eud, requested_username)

                if target_is_co and token_authorized:
                    # A physical CO portal session issued the bounded enrollment
                    # token. This is the only path that may hand an arbitrary
                    # already-known EUD to a permanent CO identity.
                    _airsoft_remember_original_owner(eud, previous_username)
                    logger.warning(
                        "Authorized CO enrollment reassigning EUD %s from %s to %s",
                        uid,
                        previous_username or "<unknown>",
                        requested_username,
                    )
                    eud.user_id = user.id
                    eud.last_status = "Disconnected"
                    db.session.add(eud)
                    db.session.commit()
                elif restore_allowed:
                    logger.warning(
                        "Restoring EUD %s from CO %s to original owner %s",
                        uid,
                        previous_username or "<unknown>",
                        requested_username,
                    )
                    eud.user_id = user.id
                    eud.last_status = "Disconnected"
                    _airsoft_clear_original_owner(eud)
                    db.session.add(eud)
                    db.session.commit()
                else:
                    logger.warning(
                        "Rejected enrollment for EUD %s owned by %s; requested by %s",
                        uid,
                        previous_username or "<unknown>",
                        requested_username,
                    )
                    return jsonify({"error": "EUD UID belongs to another user"}), 403
'''
    source = source[:start] + handover_block + source[end:]

    sign_old = (
        "        signed_csr = cert_authority.sign_csr(csr.encode(), common_name, False).decode(\"utf-8\")\n"
    )
    sign_new = (
        "        signed_csr = _airsoft_sign_csr(cert_authority, csr.encode(), common_name).decode(\"utf-8\")\n"
    )
    if sign_old not in source:
        raise RuntimeError(f"CSR signing anchor missing: {path}")
    source = source.replace(sign_old, sign_new, 1)

    path.write_text(source, encoding="utf-8")
    return True


def patch_eud_handler(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if EUD_HANDLER_MARKER in source:
        return False

    password_owner_old = '''                        self.logger.debug(
                            "Associating EUD uid {} to user {}".format(eud.uid, user.username)
                        )
                        eud.user_id = user.id
                        db.session.commit()
'''
    password_owner_new = '''                        self.logger.debug(
                            "Associating EUD uid {} to user {}".format(eud.uid, user.username)
                        )
                        current_owner = eud.user.username if eud.user else ""
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
    if password_owner_old not in source:
        raise RuntimeError(f"password-auth EUD owner anchor missing: {path}")
    source = source.replace(password_owner_old, password_owner_new, 1)

    cot_owner_old = '''                eud.last_status = "Connected"
                eud.user_id = self.user.id if self.user else None
'''
    cot_owner_new = '''                eud.last_status = "Connected"

                # AIRSOFT_CO_DEVICE_OWNER_GUARD_V1
                # Once certificate enrollment has handed this physical device
                # to a permanent CO, an old connection/certificate must not
                # silently claim the same UID back.
                current_owner = eud.user.username if eud.user else ""
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
    if cot_owner_old not in source:
        raise RuntimeError(f"CoT EUD owner anchor missing: {path}")
    source = source.replace(cot_owner_old, cot_owner_new, 1)

    handle_old = '''            self.parse_device_info(event)
            # Close the DB connection once the EUD is authenticated and identified
'''
    handle_new = '''            self.parse_device_info(event)
            if self.shutdown:
                return
            # Close the DB connection once the EUD is authenticated and identified
'''
    if handle_old not in source:
        raise RuntimeError(f"handle_cot shutdown anchor missing: {path}")
    source = source.replace(handle_old, handle_new, 1)

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
        if ENROLL_MARKER not in enrollment.read_text(encoding="utf-8"):
            backups.append((enrollment, backup(enrollment, "co-device-handover")))
        if patch_enrollment(enrollment):
            changed.append(enrollment)

        if EUD_HANDLER_MARKER not in eud_handler.read_text(encoding="utf-8"):
            backups.append((eud_handler, backup(eud_handler, "co-owner-guard")))
        if patch_eud_handler(eud_handler):
            changed.append(eud_handler)

        py_compile.compile(str(enrollment), doraise=True)
        py_compile.compile(str(eud_handler), doraise=True)
    except Exception:
        for path, saved in backups:
            shutil.copy2(saved, path)
        raise

    if changed:
        print("Installed permanent CO device handover patch.")
        for path in changed:
            print(f"Patched: {path}")
    else:
        print("Permanent CO device handover patch already installed.")

    print("Python compile checks passed.")
    for _, saved in backups:
        print(f"Backup: {saved}")
    print("Restart OpenTAKServer before testing ATAK/iTAK enrollment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
