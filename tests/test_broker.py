"""Broker address resolution from the credential response (issue #184)."""

import pytest

from ecoflow_energy.ecoflow.broker import broker_from_credentials
from ecoflow_energy.ecoflow.const import (
    MQTT_HOST,
    MQTT_PORT_TCP,
    MQTT_PORT_WSS,
    MQTT_WSS_PATH,
)

# Shape of the decrypted portal certification response, EU account.
EU_CREDENTIALS = {
    "userName": "app-user",
    "password": "secret",
    "url": "mqtt-e.ecoflow.com",
    "port": 8084,
    "protocol": "wss",
    "path": "/mqtt",
}


class TestRegionAwareBroker:
    def test_uses_the_broker_the_response_names(self) -> None:
        """The whole point: another region gets another host."""
        creds = {**EU_CREDENTIALS, "url": "mqtt-a.ecoflow.com", "port": 8085}

        broker = broker_from_credentials(creds, wss_mode=True)

        assert broker.host == "mqtt-a.ecoflow.com"
        assert broker.port == 8085
        assert broker.path == "/mqtt"

    def test_eu_response_matches_the_built_in_default(self) -> None:
        """No behaviour change for the accounts that already worked."""
        broker = broker_from_credentials(EU_CREDENTIALS, wss_mode=True)

        assert broker.host == MQTT_HOST
        assert broker.port == MQTT_PORT_WSS
        assert broker.path == MQTT_WSS_PATH

    def test_string_port_is_accepted(self) -> None:
        """Ports arrive as strings from some responses."""
        broker = broker_from_credentials({**EU_CREDENTIALS, "port": "8084"}, wss_mode=True)

        assert broker.port == 8084

    @pytest.mark.parametrize(
        "url",
        [
            "wss://mqtt-a.ecoflow.com/mqtt",
            "mqtt-a.ecoflow.com:8084",
            " mqtt-a.ecoflow.com ",
        ],
    )
    def test_url_spellings_reduce_to_the_hostname(self, url: str) -> None:
        """Paho wants a hostname, not a URL."""
        broker = broker_from_credentials({"url": url}, wss_mode=True)

        assert broker.host == "mqtt-a.ecoflow.com"

    def test_path_without_a_leading_slash_is_repaired(self) -> None:
        broker = broker_from_credentials({**EU_CREDENTIALS, "path": "mqtt"}, wss_mode=True)

        assert broker.path == "/mqtt"


class TestFallback:
    """A bad answer must never be worse than no answer."""

    def test_no_credentials_at_all(self) -> None:
        broker = broker_from_credentials(None, wss_mode=True)

        assert broker == (MQTT_HOST, MQTT_PORT_WSS, MQTT_WSS_PATH)

    def test_tcp_mode_defaults_to_the_tcp_port(self) -> None:
        broker = broker_from_credentials({}, wss_mode=False)

        assert broker.port == MQTT_PORT_TCP

    @pytest.mark.parametrize("protocol", ["ws", "mqtt", "tcp", "websocket"])
    def test_a_plaintext_port_is_never_adopted(self, protocol: str) -> None:
        """Every connection here sets TLS, on both transports.

        A port quoted for a plaintext protocol is one this client cannot
        use, so taking it would reproduce the silent refusal the module
        exists to remove. The default is the better address.
        """
        broker = broker_from_credentials(
            {"url": "mqtt-a.ecoflow.com", "port": 1883, "protocol": protocol},
            wss_mode=protocol in ("ws", "websocket"),
        )

        assert broker.host == "mqtt-a.ecoflow.com"
        assert broker.port in (MQTT_PORT_WSS, MQTT_PORT_TCP)
        assert broker.port != 1883

    def test_userinfo_never_reaches_the_address(self) -> None:
        """The address is exported; an account name would be a leak."""
        broker = broker_from_credentials(
            {"url": "wss://account-name@mqtt-a.ecoflow.com/mqtt"}, wss_mode=True
        )

        assert broker.host == "mqtt-a.ecoflow.com"
        assert "@" not in str(broker)

    @pytest.mark.parametrize(
        "url", [None, "", "localhost", "not a host", 42, "http:// /x"]
    )
    def test_unusable_host_falls_back(self, url: object) -> None:
        broker = broker_from_credentials({"url": url}, wss_mode=True)

        assert broker.host == MQTT_HOST

    @pytest.mark.parametrize("port", [None, "", "abc", 0, -1, 99999, 3.5j])
    def test_unusable_port_falls_back(self, port: object) -> None:
        broker = broker_from_credentials(
            {"url": "mqtt-a.ecoflow.com", "port": port}, wss_mode=True
        )

        assert broker.port == MQTT_PORT_WSS

    def test_protocol_never_overrides_the_transport(self) -> None:
        """The auth mode owns the transport, not the response.

        Standard Mode is TCP by design. A response advertising ``wss`` must
        not silently move a developer-key entry onto the app transport -
        that would be the mode mixing the architecture forbids.
        """
        broker = broker_from_credentials(
            {"protocol": "wss", "url": "mqtt-a.ecoflow.com"}, wss_mode=False
        )

        assert broker.port == MQTT_PORT_TCP

    @pytest.mark.parametrize(
        ("protocol", "wss_mode"), [("mqtts", True), ("wss", False)]
    )
    def test_a_port_quoted_for_another_transport_is_not_adopted(
        self, protocol: str, wss_mode: bool
    ) -> None:
        """A port belongs to its protocol.

        Dialling a plain-MQTT port over websockets is a worse address than
        the default, so a mismatched answer keeps the default port while
        the host it names is still used.
        """
        broker = broker_from_credentials(
            {"url": "mqtt-a.ecoflow.com", "port": 1234, "protocol": protocol},
            wss_mode=wss_mode,
        )

        assert broker.host == "mqtt-a.ecoflow.com"
        assert broker.port == (MQTT_PORT_WSS if wss_mode else MQTT_PORT_TCP)

    @pytest.mark.parametrize("protocol", [None, "", "  ", "something-new", 7])
    def test_an_unnamed_or_unknown_protocol_is_taken_at_its_word(
        self, protocol: object
    ) -> None:
        broker = broker_from_credentials(
            {"url": "mqtt-a.ecoflow.com", "port": 1234, "protocol": protocol},
            wss_mode=True,
        )

        assert broker.port == 1234

    def test_the_live_endpoints_agree_with_their_own_ports(self) -> None:
        """Both real responses, as measured on 2026-08-08."""
        portal = broker_from_credentials(EU_CREDENTIALS, wss_mode=True)
        developer = broker_from_credentials(
            {
                "certificateAccount": "acc",
                "certificatePassword": "pw",
                "url": "mqtt-e.ecoflow.com",
                "port": "8883",
                "protocol": "mqtts",
            },
            wss_mode=False,
        )

        assert portal == ("mqtt-e.ecoflow.com", 8084, "/mqtt")
        assert developer.port == 8883

    def test_address_renders_as_host_and_port(self) -> None:
        """Exported into diagnostics, so it has to read cleanly."""
        assert str(broker_from_credentials(EU_CREDENTIALS, wss_mode=True)) == (
            "mqtt-e.ecoflow.com:8084"
        )
