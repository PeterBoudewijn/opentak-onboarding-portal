#!/usr/bin/env python3
"""Harden OpenTAKServer 1.7.13 certificate enrollment.

Fixes three authorization problems in upstream 1.7.13:
1. Enrollment tokens are accepted for any supplied username instead of being
   bound to the token's subject.
2. A successfully authenticated user can submit a CSR whose Common Name is a
   different OTS username. OTS later authenticates client certificates by CN.
3. An enrollment request can name an EUD UID that already belongs to another
   user, which can corrupt certificate/EUD metadata.

The patch is idempotent, creates timestamped backups, and compiles the modified
modules before returning success.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

TOKEN_MARKER = "# AIRSOFT_ENROLLMENT_TOKEN_SUBJECT_BINDING_V1"
ENROLL_MARKER = "# AIRSOFT_CERT_ENROLLMENT_IDENTITY_BINDING_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run this with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak-enrollment-security-{stamp}")
    shutil.copy2(path, dest)
    return dest


def patch_token(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if TOKEN_MARKER in source:
        return False

    old_sig = "    def verify_token(token: str) -> bool:\n"
    new_sig = (
        f"    {TOKEN_MARKER}\n"
        "    def verify_token(token: str, expected_username: str = None) -> bool:\n"
    )
    if old_sig not in source:
        raise RuntimeError(f"Token.verify_token signature anchor missing: {path}")
    source = source.replace(old_sig, new_sig, 1)

    anchor = '''                decoded_token: dict = jwt.decode(
                    token, key.read(), algorithms=["RS256"], audience="OpenTAKServer"
                )

                sha256 = hashlib.sha256()
'''
    replacement = '''                decoded_token: dict = jwt.decode(
                    token, key.read(), algorithms=["RS256"], audience="OpenTAKServer"
                )

                # Enrollment tokens are bearer credentials for exactly one OTS
                # username. Upstream 1.7.13 only checked that the token itself
                # was valid, allowing it to be paired with another username.
                if expected_username is not None:
                    subject = str(decoded_token.get("sub") or "")
                    if subject.casefold() != str(expected_username).casefold():
                        logger.error(
                            "Token subject %s does not match requested username %s",
                            subject,
                            expected_username,
                        )
                        return False

                sha256 = hashlib.sha256()
'''
    if anchor not in source:
        raise RuntimeError(f"Token decode anchor missing: {path}")
    source = source.replace(anchor, replacement, 1)
    path.write_text(source, encoding="utf-8")
    return True


def patch_enrollment(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if ENROLL_MARKER in source:
        return False

    old_auth = '''        user = app.security.datastore.find_user(username=username)
        if not user:
            logger.error(f"User {username} doesn't exist")
            return False
        elif verify_password(password, user.password):
            return True
        else:
            return Token.verify_token(password)
'''
    new_auth = f'''        user = app.security.datastore.find_user(username=username)
        if not user:
            logger.error(f"User {{username}} doesn't exist")
            return False
        if not user.active:
            logger.error(f"User {{username}} is deactivated")
            return False
        if verify_password(password, user.password):
            return True

        {ENROLL_MARKER}
        # A token may only authenticate the username stored in its subject.
        return Token.verify_token(password, expected_username=username)
'''
    if old_auth not in source:
        raise RuntimeError(f"Enrollment authentication anchor missing: {path}")
    source = source.replace(old_auth, new_auth, 1)

    old_cn = '''        common_name = x509.get_subject().CN
        logger.debug("Attempting to sign CSR for {}".format(common_name))

        cert_authority = CertificateAuthority(logger, app)
'''
    new_cn = '''        common_name = x509.get_subject().CN
        logger.debug("Attempting to sign CSR for {}".format(common_name))

        # OTS authenticates later TLS connections by certificate Common Name.
        # Therefore the CSR CN must be the same identity that authenticated
        # this enrollment request.
        auth_username, _auth_secret = (
            base64.b64decode(request.headers.get("Authorization").split(" ")[-1].encode("utf-8"))
            .decode("utf-8")
            .split(":", 1)
        )
        auth_username = bleach.clean(auth_username)
        if not common_name or common_name.casefold() != auth_username.casefold():
            logger.warning(
                "Rejected CSR identity mismatch: authenticated=%s csr_cn=%s",
                auth_username,
                common_name,
            )
            return jsonify({"error": "CSR common name does not match authenticated user"}), 403

        cert_authority = CertificateAuthority(logger, app)
'''
    if old_cn not in source:
        raise RuntimeError(f"CSR common-name anchor missing: {path}")
    source = source.replace(old_cn, new_cn, 1)

    old_eud = '''        try:
            if uid:
                eud = EUD()
                eud.uid = uid
                eud.user_id = user.id

                db.session.add(eud)
                db.session.commit()
        except sqlalchemy.exc.IntegrityError:
            db.session.rollback()
            eud = db.session.execute(db.session.query(EUD).filter_by(uid=uid)).first()[0]
            if user and not eud.user_id:
                eud.user_id = user.id
                db.session.add(eud)
                db.session.commit()
'''
    new_eud = '''        try:
            if uid:
                eud = EUD()
                eud.uid = uid
                eud.user_id = user.id

                db.session.add(eud)
                db.session.commit()
        except sqlalchemy.exc.IntegrityError:
            db.session.rollback()
            eud = db.session.execute(db.session.query(EUD).filter_by(uid=uid)).first()[0]
            if eud.user_id and eud.user_id != user.id:
                logger.warning(
                    "Rejected enrollment for EUD %s owned by another user",
                    uid,
                )
                return jsonify({"error": "EUD UID belongs to another user"}), 403
            if user and not eud.user_id:
                eud.user_id = user.id
                db.session.add(eud)
                db.session.commit()
'''
    if old_eud not in source:
        raise RuntimeError(f"EUD ownership anchor missing: {path}")
    source = source.replace(old_eud, new_eud, 1)

    path.write_text(source, encoding="utf-8")
    return True


def main() -> int:
    root = package_root()
    token_py = root / "models" / "Token.py"
    enrollment_py = root / "blueprints" / "marti_api" / "certificate_enrollment_api.py"

    for path in (token_py, enrollment_py):
        if not path.is_file():
            raise RuntimeError(f"Missing OTS source file: {path}")

    backups = []
    changed = []

    try:
        for path, patcher, marker in (
            (token_py, patch_token, TOKEN_MARKER),
            (enrollment_py, patch_enrollment, ENROLL_MARKER),
        ):
            if marker not in path.read_text(encoding="utf-8"):
                backups.append((path, backup(path)))
            if patcher(path):
                changed.append(path)

        py_compile.compile(str(token_py), doraise=True)
        py_compile.compile(str(enrollment_py), doraise=True)
    except Exception:
        for path, saved in backups:
            shutil.copy2(saved, path)
        raise

    if changed:
        print("Installed OTS 1.7.13 certificate-enrollment security patch.")
        for path in changed:
            print(f"Patched: {path}")
    else:
        print("Certificate-enrollment security patch already installed.")

    print("Python compile check passed.")
    for _, saved in backups:
        print(f"Backup: {saved}")
    print("Restart OpenTAKServer before testing enrollment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
