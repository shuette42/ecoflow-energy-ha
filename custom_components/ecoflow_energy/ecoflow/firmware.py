"""Extract firmware revisions from an HTTP quota response.

Neither device list endpoint reports a firmware version - not the Developer API
one and not the app one - so the quota is the only place a revision can be read
from. Devices that carry one report it per subsystem (`pd.sysVer`,
`inv.sysVer`, `mppt.swVer`, ...) as a 32-bit integer whose four bytes are the
four version components.

Devices that report no such key at all get an empty result. That is a fact
about the device, not a failure: PowerOcean sends 347 quota keys and none of
them is a version.

The decoded form is offered alongside the raw integer, never instead of it. The
byte split matches every sample seen so far, but no owner has confirmed against
what the EcoFlow app displays, so a reader needs the raw value to check.
"""

from __future__ import annotations

from typing import Any

# Substrings that mark a quota key as a firmware or software revision.
# Matched against the last path segment so that unrelated keys carrying "ver"
# inside a word (`pcsOverVolDeratingDaleyTime`) do not qualify.
_VERSION_SUFFIXES = ("sysver", "swver", "softver", "loaderver", "hwversion", "wifiver")


def _is_version_key(key: str) -> bool:
    """Return True if the quota key names a firmware or software revision."""
    leaf = key.rsplit(".", 1)[-1].lower()
    return leaf in _VERSION_SUFFIXES


def decode_version(value: int) -> str:
    """Render a packed 32-bit revision as its four dotted components.

    Returns an empty string for anything that cannot be a packed revision, so
    a caller never has to distinguish "no version" from "unparsable version".
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return ""
    if value <= 0 or value > 0xFFFFFFFF:
        return ""
    major, minor, patch, build = value.to_bytes(4, "big")
    return f"v{major}.{minor}.{patch}.{build}"


def extract_firmware_versions(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect every firmware revision the quota reports, per subsystem.

    Returns {quota_key: {"raw": int, "decoded": str}}. Empty when the device
    reports no revision, which is the normal case for PowerOcean, Delta 3 and
    the Smart Plug.
    """
    versions: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not _is_version_key(key):
            continue
        decoded = decode_version(value)
        if not decoded:
            continue
        versions[key] = {"raw": value, "decoded": decoded}
    return versions
