"""Tests for diagnostics - verifies no credentials are exposed."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_diagnostics_redacts_credentials():
    """The diagnostics module must REDACT all credential fields."""
    with open(REPO_ROOT / "custom_components/ecoflow_energy/diagnostics.py") as f:
        source = f.read()

    # These keys must appear as REDACTED in the output dict
    for key in ("access_key", "secret_key", "email", "password"):
        pattern = rf'"{key}":\s*REDACTED'
        assert re.search(pattern, source), f'"{key}" not REDACTED in diagnostics.py'

    # Email/password are never needed by diagnostics and must never be
    # referenced. Access/secret key ARE read (read-only) to sign the raw
    # quota request for unsupported devices, so they are allowed here - the
    # runtime tests prove their values never reach the output.
    for conf_name in ("CONF_EMAIL", "CONF_PASSWORD"):
        assert conf_name not in source, f"{conf_name} must not appear in diagnostics.py"


def test_diagnostics_no_password_values_in_output():
    """Static analysis: diagnostics must never read email/password from entry.data."""
    with open(REPO_ROOT / "custom_components/ecoflow_energy/diagnostics.py") as f:
        source = f.read()

    # Must not access email/password from entry.data. Access/secret key are
    # read for the read-only quota client (see runtime output-guard tests).
    dangerous_patterns = [
        r'entry\.data\[.*(EMAIL|PASSWORD)',
        r'entry\.data\.get\(.*(email|password)',
    ]
    for pattern in dangerous_patterns:
        assert not re.search(pattern, source, re.IGNORECASE), \
            f"diagnostics.py must not access email/password: {pattern}"
