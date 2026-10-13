#!/usr/bin/env python3
"""Add a dedicated audit trail for authenticated EUD account switches.

The audit log contains no passwords, enrollment tokens or certificate private
material. It records only the time, EUD UID, previous username, new username and
whether the enrollment used a password or a username-bound enrollment token.

Log file:
    <OTS_DATA_FOLDER>/logs/airsoft_eud_audit.jsonl

The file is append-only from the application point of view and is created with
mode 0600.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_EUD_AUDIT_LOG_V1"
SWITCH_MARKER = "# AIRSOFT_AUTHENTICATED_EUD_SWITCH_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def main() -> int:
    target = package_root() / "blueprints" / "marti_api" / "certificate_enrollment_api.py"
    source = target.read_text(encoding="utf-8")

    if MARKER in source:
        py_compile.compile(str(target), doraise=True)
        print("EUD audit log patch already installed.")
        return 0

    if SWITCH_MARKER not in source:
        raise RuntimeError(
            "Authenticated EUD switching patch must be installed before the audit log patch."
        )

    if "import json\n" not in source:
        anchor = "import fcntl\n"
        if anchor not in source:
            raise RuntimeError(f"fcntl import anchor missing: {target}")
        source = source.replace(anchor, anchor + "import json\n", 1)

    helper_anchor = "\ndef _airsoft_sign_csr(cert_authority, csr_bytes, common_name):\n"
    if helper_anchor not in source:
        raise RuntimeError(f"CO signing helper anchor missing: {target}")

    helper = r'''

# AIRSOFT_EUD_AUDIT_LOG_V1
def _airsoft_audit_eud_switch(uid, previous_username, requested_username):
    """Append one successful ownership change without storing credentials."""
    auth_method = (
        "enrollment_token"
        if bool(getattr(g, "airsoft_enrollment_token_auth", False))
        else "password"
    )
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "eud_owner_switch",
        "uid": str(uid or ""),
        "from_user": str(previous_username or ""),
        "to_user": str(requested_username or ""),
        "auth_method": auth_method,
    }

    log_dir = os.path.join(app.config.get("OTS_DATA_FOLDER"), "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "airsoft_eud_audit.jsonl")

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

'''
    source = source.replace(helper_anchor, helper + helper_anchor, 1)

    old = '''                eud.user_id = user.id
                eud.last_status = "Disconnected"
                db.session.add(eud)
                db.session.commit()
'''
    new = '''                eud.user_id = user.id
                eud.last_status = "Disconnected"
                db.session.add(eud)
                db.session.commit()
                _airsoft_audit_eud_switch(
                    uid,
                    previous_username,
                    requested_username,
                )
'''

    # The authenticated switching block is the only ownership-changing block
    # we want to audit here. Locate it after its marker before replacing.
    switch_pos = source.find(SWITCH_MARKER)
    if switch_pos < 0:
        raise RuntimeError(f"switch marker missing: {target}")
    change_pos = source.find(old, switch_pos)
    if change_pos < 0:
        raise RuntimeError(f"authenticated switch commit anchor missing: {target}")
    source = source[:change_pos] + new + source[change_pos + len(old):]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = target.with_name(target.name + f".bak-eud-audit-{stamp}")
    shutil.copy2(target, saved)

    try:
        target.write_text(source, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(saved, target)
        raise

    print("Installed EUD account-switch audit log patch.")
    print(f"Patched: {target}")
    print(f"Backup: {saved}")
    print("Audit file: <OTS_DATA_FOLDER>/logs/airsoft_eud_audit.jsonl")
    print("Python compile check passed.")
    print("Restart OpenTAKServer before testing account switching.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
