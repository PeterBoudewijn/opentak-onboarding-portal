#!/usr/bin/env python3
"""Enforce the airsoft CO access window on issued client certificates.

OpenTAKServer 1.7.13 client certificates are normally valid for years. The
airsoft portal instead requires CORood/COBlauw access to end at 23:00
Europe/Amsterdam (and never last more than 24 real hours).

This patch:
- records the issued certificate serial and the CO access cutoff per EUD;
- validates that serial/cutoff on every active CO TAK stream;
- closes an active CO stream when the CO account is inactive, the cutoff has
  passed, or an old certificate is presented;
- supports multiple simultaneous CO devices because the serial/cutoff is stored
  per EUD rather than per CO username;
- clears the internal CO certificate window when that EUD enrolls as a normal
  non-CO account.

No certificate private keys, passwords or enrollment tokens are logged/stored.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

ENROLL_MARKER = "# AIRSOFT_CO_DAILY_CERT_WINDOW_V1"
STREAM_MARKER = "# AIRSOFT_CO_DAILY_STREAM_GUARD_V1"


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

    required = (
        "# AIRSOFT_AUTHENTICATED_EUD_SWITCH_V1",
        "# AIRSOFT_CO_DEVICE_RESTORE_CLEANUP_V1",
        "# AIRSOFT_CO_HQ_PROFILE_ON_ENROLLMENT_V1",
    )
    missing = [marker for marker in required if marker not in source]
    if missing:
        raise RuntimeError(
            "Required OTS patches missing before CO daily certificate window: "
            + ", ".join(missing)
        )

    if "from zoneinfo import ZoneInfo\n" not in source:
        anchor = "from urllib.parse import urlparse\n"
        if anchor not in source:
            raise RuntimeError(f"urlparse import anchor missing: {path}")
        source = source.replace(
            anchor,
            anchor + "from zoneinfo import ZoneInfo\n",
            1,
        )

    helper_anchor = "\ndef _airsoft_sign_csr(cert_authority, csr_bytes, common_name):\n"
    if helper_anchor not in source:
        raise RuntimeError(f"CO signing helper anchor missing: {path}")

    helper = r'''

# AIRSOFT_CO_DAILY_CERT_WINDOW_V1
_AIRSOFT_CO_CERT_SERIAL_KEY = "airsoftCoCertificateSerial"
_AIRSOFT_CO_VALID_UNTIL_KEY = "airsoftCoValidUntil"


def _airsoft_next_co_cutoff():
    """Next local 23:00, capped at 24 real hours."""
    tz = ZoneInfo("Europe/Amsterdam")
    now = datetime.datetime.now(tz)
    cutoff = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        cutoff += datetime.timedelta(days=1)

    hard_limit = now.astimezone(datetime.timezone.utc) + datetime.timedelta(hours=24)
    if cutoff.astimezone(datetime.timezone.utc) > hard_limit:
        cutoff = hard_limit.astimezone(tz)
    return cutoff


def _airsoft_set_internal_profile(eud, key, value):
    profile = db.session.query(DeviceProfiles).filter_by(
        preference_key=key,
        eud_uid=eud.uid,
    ).first()
    if not profile:
        profile = DeviceProfiles(preference_key=key, eud_uid=eud.uid)

    profile.preference_value = str(value)
    profile.value_class = "class java.lang.String"
    profile.enrollment = False
    profile.connection = False
    profile.active = False
    profile.publish_time = datetime.datetime.now(datetime.timezone.utc)
    db.session.add(profile)


def _airsoft_clear_co_certificate_window(eud):
    for key in (_AIRSOFT_CO_CERT_SERIAL_KEY, _AIRSOFT_CO_VALID_UNTIL_KEY):
        db.session.query(DeviceProfiles).filter_by(
            preference_key=key,
            eud_uid=eud.uid,
        ).delete(synchronize_session=False)


def _airsoft_set_co_certificate_window(username, eud, signed_certificate_pem):
    if not _airsoft_is_co_username(username):
        _airsoft_clear_co_certificate_window(eud)
        db.session.commit()
        return

    certificate = crypto.load_certificate(
        crypto.FILETYPE_PEM,
        signed_certificate_pem.encode("utf-8"),
    )
    serial = certificate.get_serial_number()
    cutoff = _airsoft_next_co_cutoff()

    _airsoft_set_internal_profile(eud, _AIRSOFT_CO_CERT_SERIAL_KEY, serial)
    _airsoft_set_internal_profile(
        eud,
        _AIRSOFT_CO_VALID_UNTIL_KEY,
        int(cutoff.timestamp()),
    )
    db.session.commit()

    logger.info(
        "Bound CO certificate window for EUD %s user %s until %s",
        eud.uid,
        username,
        cutoff.isoformat(),
    )

'''
    source = source.replace(helper_anchor, helper + helper_anchor, 1)

    # Preserve the unstripped PEM returned by the existing CO signing helper.
    # Earlier deployed versions may format the helper call on one line or over
    # several lines, so do not depend on that exact formatting.
    if "signed_csr_pem = signed_csr" not in source:
        sign_start = source.find("signed_csr = _airsoft_sign_csr")
        strip_anchor = '        signed_csr = signed_csr.replace("-----BEGIN CERTIFICATE-----\\n", "")\n'
        strip_pos = source.find(strip_anchor, sign_start if sign_start >= 0 else 0)
        if sign_start < 0 or strip_pos < 0:
            raise RuntimeError(
                f"CO CSR signing/strip anchors missing: {path}"
            )
        source = (
            source[:strip_pos]
            + "        signed_csr_pem = signed_csr\n"
            + source[strip_pos:]
        )

    if "_airsoft_set_co_certificate_window(" not in source[source.find("def sign_csr_v2"):]:
        profile_call = "            _airsoft_apply_co_profile(user.username, eud)\n"
        sign_v2_pos = source.find("def sign_csr_v2")
        profile_pos = source.find(profile_call, sign_v2_pos if sign_v2_pos >= 0 else 0)
        if sign_v2_pos < 0 or profile_pos < 0:
            raise RuntimeError(f"CO certificate profile anchor missing: {path}")
        insert_pos = profile_pos + len(profile_call)
        source = (
            source[:insert_pos]
            + "            _airsoft_set_co_certificate_window(\n"
            + "                user.username,\n"
            + "                eud,\n"
            + "                signed_csr_pem,\n"
            + "            )\n"
            + source[insert_pos:]
        )

    path.write_text(source, encoding="utf-8")
    return True


def patch_eud_handler(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if STREAM_MARKER in source:
        return False

    if "# AIRSOFT_AUTHENTICATED_EUD_STREAM_GUARD_V1" not in source:
        raise RuntimeError(
            "Authenticated EUD stream guard must be installed before CO daily stream guard."
        )

    if "from OpenSSL import crypto\n" not in source:
        anchor = "from flask_security.models import fsqla\n"
        if anchor not in source:
            raise RuntimeError(f"flask_security import anchor missing: {path}")
        source = source.replace(
            anchor,
            anchor + "from OpenSSL import crypto\n",
            1,
        )

    method_anchor = "\n    def handle_cot(self, cot):\n"
    if method_anchor not in source:
        raise RuntimeError(f"handle_cot anchor missing: {path}")

    helper = r'''
    # AIRSOFT_CO_DAILY_STREAM_GUARD_V1
    def _airsoft_co_stream_allowed(self):
        username = str(self.user.username if self.user else "")
        if username.casefold() not in ("corood", "coblauw"):
            return True
        if not self.uid:
            return False

        serial_key = "airsoftCoCertificateSerial"
        cutoff_key = "airsoftCoValidUntil"

        try:
            with self.app.app_context():
                current_user = self.app.security.datastore.find_user(username=username)
                if not current_user or not current_user.active:
                    self.logger.warning(
                        "Closing CO EUD %s because account %s is inactive",
                        self.uid,
                        username,
                    )
                    return False

                rows = db.session.query(DeviceProfiles).filter(
                    DeviceProfiles.eud_uid == self.uid,
                    DeviceProfiles.preference_key.in_((serial_key, cutoff_key)),
                ).all()
                values = {row.preference_key: row.preference_value for row in rows}

            expected_serial = str(values.get(serial_key) or "")
            try:
                valid_until = int(str(values.get(cutoff_key) or "0"))
            except (TypeError, ValueError):
                valid_until = 0

            now_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            if not expected_serial or not valid_until or now_epoch >= valid_until:
                self.logger.warning(
                    "Closing expired/unbound CO EUD %s for %s",
                    self.uid,
                    username,
                )
                return False

            peer_serial = getattr(self, "_airsoft_peer_cert_serial", None)
            if peer_serial is None:
                peer_der = self.request.getpeercert(binary_form=True)
                if not peer_der:
                    self.logger.warning(
                        "Closing CO EUD %s: client certificate unavailable",
                        self.uid,
                    )
                    return False
                peer_certificate = crypto.load_certificate(crypto.FILETYPE_ASN1, peer_der)
                peer_serial = str(peer_certificate.get_serial_number())
                self._airsoft_peer_cert_serial = peer_serial

            if str(peer_serial) != expected_serial:
                self.logger.warning(
                    "Closing stale CO certificate for EUD %s user %s",
                    self.uid,
                    username,
                )
                return False

            return True
        except BaseException:
            self.logger.error(
                "Failed CO certificate-window validation for EUD %s",
                self.uid,
            )
            self.logger.debug(traceback.format_exc())
            return False

'''
    source = source.replace(method_anchor, "\n" + helper + method_anchor.lstrip("\n"), 1)

    publish_old = '''        self.publish_cot(event)
'''
    publish_new = '''        if event and self.uid and not self._airsoft_co_stream_allowed():
            self.close_connection()
            return

        self.publish_cot(event)
'''
    # Replace the handle_cot occurrence only: it is the first publish_cot(event)
    # after the handle_cot definition.
    handle_pos = source.find("    def handle_cot(self, cot):")
    publish_pos = source.find(publish_old, handle_pos)
    if publish_pos < 0:
        raise RuntimeError(f"handle_cot publish anchor missing: {path}")
    source = (
        source[:publish_pos]
        + publish_new
        + source[publish_pos + len(publish_old):]
    )

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
            backups.append((enrollment, backup(enrollment, "co-daily-cert-window")))
        if patch_enrollment(enrollment):
            changed.append(enrollment)

        if STREAM_MARKER not in eud_handler.read_text(encoding="utf-8"):
            backups.append((eud_handler, backup(eud_handler, "co-daily-stream-guard")))
        if patch_eud_handler(eud_handler):
            changed.append(eud_handler)

        py_compile.compile(str(enrollment), doraise=True)
        py_compile.compile(str(eud_handler), doraise=True)
    except Exception:
        for path, saved in backups:
            shutil.copy2(saved, path)
        raise

    if changed:
        print("Installed daily CO certificate-window enforcement.")
        for path in changed:
            print(f"Patched: {path}")
    else:
        print("Daily CO certificate-window enforcement already installed.")

    print("Python compile checks passed.")
    for _, saved in backups:
        print(f"Backup: {saved}")
    print("Restart OpenTAKServer before testing CO cutoff enforcement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
