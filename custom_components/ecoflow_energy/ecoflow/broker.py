"""Broker address handed out with the MQTT credentials.

Both credential endpoints - the IoT Developer API certification and the
portal one used by app sign-in - answer with the broker the account is
supposed to use: ``url``, ``port``, ``protocol`` and ``path``. The
integration used to read only the account and the password out of that
answer and connect to a compile-time constant instead, which works for as
long as every account lives in the region that constant points at.

It does not. An account served from another region gets credentials that
the EU broker will not accept, and the failure is silent by construction:
the socket opens, the broker closes it without a CONNACK, and paho reports
a plain disconnect. Nothing in that sequence names a region.

The values are validated rather than trusted: a malformed answer falls
back to the constants, which is exactly the behaviour that was correct for
every account before this existed.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .const import MQTT_HOST, MQTT_PORT_TCP, MQTT_PORT_WSS, MQTT_WSS_PATH


class BrokerAddress(NamedTuple):
    """Where to reach the broker for one set of credentials."""

    host: str
    port: int
    path: str

    def __str__(self) -> str:
        """Return ``host:port``, safe to log and to export.

        A hostname and a port are public infrastructure, not account data -
        unlike the certificate account, which shares the same response.
        """
        return f"{self.host}:{self.port}"


def broker_from_credentials(
    credentials: dict[str, Any] | None, *, wss_mode: bool
) -> BrokerAddress:
    """Return the broker named by the credential response.

    Falls back to the built-in defaults for anything missing or malformed.
    ``wss_mode`` decides which default port applies, and is also what the
    caller has already used to pick a transport - the response's own
    ``protocol`` field is deliberately not allowed to override that, since
    the transport choice belongs to the auth mode.
    """
    default_port = MQTT_PORT_WSS if wss_mode else MQTT_PORT_TCP
    if not credentials:
        return BrokerAddress(MQTT_HOST, default_port, MQTT_WSS_PATH)

    host = _clean_host(credentials.get("url")) or MQTT_HOST
    path = _clean_path(credentials.get("path")) or MQTT_WSS_PATH
    # A port belongs to the protocol it was quoted for. Taking 8883 out of a
    # plain-MQTT answer and dialling it over websockets would be a worse
    # address than the default, so the port is only adopted when the answer
    # is about the transport this connection uses. An answer that names no
    # protocol is taken at its word, which is how it behaved before.
    if _protocol_matches(credentials.get("protocol"), wss_mode=wss_mode):
        port = _clean_port(credentials.get("port")) or default_port
    else:
        port = default_port
    return BrokerAddress(host, port, path)


def _protocol_matches(value: Any, *, wss_mode: bool) -> bool:
    """Return whether the named protocol is the one this client speaks.

    Encryption counts as part of the answer, not only the transport. Every
    connection here calls ``tls_set`` unconditionally, so a port quoted for
    a plaintext protocol is one this client cannot use, and adopting it
    would reproduce the failure this module exists to remove: the socket
    opens, nothing comes back, and the retry loop starts. The default port
    is the better address in that case.
    """
    if not isinstance(value, str) or not value.strip():
        return True
    protocol = value.strip().lower().rstrip(":/")
    if protocol in ("wss", "websockets"):
        return wss_mode
    if protocol in ("mqtts", "ssl", "tls"):
        return not wss_mode
    if protocol in ("ws", "websocket", "mqtt", "tcp"):
        return False
    return True


def _clean_host(value: Any) -> str:
    """Return a bare hostname, or an empty string if it is not one."""
    if not isinstance(value, str):
        return ""
    host = value.strip()
    # Some responses spell the broker as a URL rather than a hostname.
    for prefix in ("wss://", "ws://", "mqtts://", "mqtt://", "ssl://", "tcp://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
            break
    host = host.split("/", 1)[0]
    # Userinfo would be account data, and the resolved address is exported
    # into a diagnostics download that users attach to public issues.
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if not host or " " in host or "." not in host:
        return ""
    return host


def _clean_port(value: Any) -> int:
    """Return a usable TCP port, or 0 if the value is not one."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0
    return port if 0 < port <= 65535 else 0


def _clean_path(value: Any) -> str:
    """Return a websocket path, or an empty string if it is not one."""
    if not isinstance(value, str):
        return ""
    path = value.strip()
    if not path:
        return ""
    return path if path.startswith("/") else f"/{path}"
