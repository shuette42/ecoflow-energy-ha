"""Tests for the listen-only capture of devices that have no parser yet."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ecoflow_energy.const import RAW_FRAME_LOG_MAX
from ecoflow_energy.device_probe import UnroutedDeviceProbe, async_start_probes
from ecoflow_energy.ecoflow.cloud_mqtt import EcoFlowMQTTClient

SKIPPED_SN = "RE11TEST00000001"


def _probe(hass: HomeAssistant) -> UnroutedDeviceProbe:
    with patch("ecoflow_energy.device_probe.EcoFlowMQTTClient") as mock_client:
        mock_client.return_value = MagicMock()
        probe = UnroutedDeviceProbe(
            hass, SKIPPED_SN, "Ocean 2", "cert_account", "cert_password", "user123"
        )
    return probe


class TestListenOnly:
    async def test_no_write_path_is_configured(self, hass: HomeAssistant) -> None:
        """A device we know nothing about must never be written to."""
        with patch("ecoflow_energy.device_probe.EcoFlowMQTTClient") as mock_client:
            UnroutedDeviceProbe(
                hass, SKIPPED_SN, "Ocean 2", "acc", "pw", "user123"
            )

        kwargs = mock_client.call_args.kwargs
        # enhanced_mode alone does NOT make this listen-only: it suppresses
        # the energy stream switch, while the shared client still fires
        # get-all and latestQuotas from _on_connect. listen_only is the flag
        # that holds the promise - see the behaviour test below.
        assert kwargs["listen_only"] is True
        assert kwargs["enhanced_mode"] is False
        assert kwargs["wss_mode"] is True
        assert kwargs["device_sn"] == SKIPPED_SN

    async def test_connect_callback_transmits_nothing(
        self, hass: HomeAssistant
    ) -> None:
        """The real client, driven through the callback that broke this.

        Asserting constructor kwargs on a mocked client proves nothing about
        behaviour - that is exactly how the shared client's autonomous
        get-all/latestQuotas publishes stayed invisible. This drives the
        actual _on_connect with only the transport mocked.
        """
        client = EcoFlowMQTTClient(
            certificate_account="acc",
            certificate_password="pw",
            device_sn=SKIPPED_SN,
            message_handler=lambda topic, payload: None,
            user_id="user123",
            wss_mode=True,
            enhanced_mode=False,
            listen_only=True,
        )
        paho = MagicMock()
        client.client = paho

        client._on_connect(paho, None, {}, 0)

        paho.publish.assert_not_called()
        assert client.publish("/any/topic", b"payload") is False

    async def test_a_normal_connection_still_requests_data(
        self, hass: HomeAssistant
    ) -> None:
        """Guard against the listen-only flag silencing regular devices."""
        client = EcoFlowMQTTClient(
            certificate_account="acc",
            certificate_password="pw",
            device_sn="HJ31TEST00000001",
            message_handler=lambda topic, payload: None,
            user_id="user123",
            wss_mode=True,
            enhanced_mode=False,
        )
        paho = MagicMock()
        paho.is_connected.return_value = True
        client.client = paho

        client._on_connect(paho, None, {}, 0)

        assert paho.publish.called

    async def test_connection_failures_stay_out_of_the_log(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A device with no entities must not produce warnings.

        The probe reuses the shared client, which reports connection
        trouble at WARNING/ERROR. For an unsupported device none of that is
        actionable - there is nothing the user could fix and nothing is
        broken - so it would be pure noise in their log.
        """
        client = EcoFlowMQTTClient(
            certificate_account="acc",
            certificate_password="pw",
            device_sn=SKIPPED_SN,
            message_handler=lambda topic, payload: None,
            user_id="user123",
            wss_mode=True,
            listen_only=True,
        )
        paho = MagicMock()
        client.client = paho

        with caplog.at_level(logging.DEBUG):
            client._on_connect(paho, None, {}, 5)          # bad credentials
            client.reconnect_attempts = 3
            client._on_disconnect(paho, None, None, 7, None)  # sustained failure

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert any(r.levelno == logging.DEBUG for r in caplog.records)

    async def test_a_normal_connection_still_warns(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The quiet path must not silence real devices."""
        client = EcoFlowMQTTClient(
            certificate_account="acc",
            certificate_password="pw",
            device_sn="HJ31TEST00000001",
            message_handler=lambda topic, payload: None,
            user_id="user123",
            wss_mode=True,
        )
        paho = MagicMock()
        client.client = paho

        with caplog.at_level(logging.DEBUG):
            client._on_connect(paho, None, {}, 5)

        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    async def test_proto_frame_is_captured_with_commands(
        self, hass: HomeAssistant
    ) -> None:
        probe = _probe(hass)

        with patch(
            "ecoflow_energy.device_probe.decode_cmd_headers",
            return_value=[{"cmd_func": 96, "cmd_id": 33}],
        ):
            probe._on_message(f"/app/device/property/{SKIPPED_SN}", b"\x0a\x02\xff\xff")

        frame = probe.frames[0]
        assert frame["format"] == "proto"
        assert frame["cmds"] == [{"cmd_func": 96, "cmd_id": 33}]

    async def test_json_frame_is_captured_too(self, hass: HomeAssistant) -> None:
        """An unknown device may speak JSON, and its keys are the evidence."""
        probe = _probe(hass)

        probe._on_message(f"/app/device/property/{SKIPPED_SN}", b'{"powGetPv": 1}')

        frame = probe.frames[0]
        assert frame["format"] == "json"
        assert bytes.fromhex(frame["hex"]) == b'{"powGetPv": 1}'

    async def test_serial_is_masked_in_captured_frame(
        self, hass: HomeAssistant
    ) -> None:
        probe = _probe(hass)

        probe._on_message("/topic", b"\x0a" + SKIPPED_SN.encode())

        captured = bytes.fromhex(probe.frames[0]["hex"])
        assert SKIPPED_SN.encode() not in captured

    async def test_topics_are_recorded_without_any_identifier(
        self, hass: HomeAssistant
    ) -> None:
        """Which topic delivered tells silence apart from undecodable data.

        Uses the real app topic layout, which carries the EcoFlow user id
        ahead of the serial. Topics land in diagnostics, and diagnostics
        get attached to public issue reports.
        """
        probe = _probe(hass)

        probe._on_message(
            f"/app/user123/{SKIPPED_SN}/thing/property/get_reply", b"\x0a\x01"
        )

        assert probe.topics == ["/app/{uid}/{sn}/thing/property/get_reply"]

    async def test_certificate_account_is_masked_in_topics(
        self, hass: HomeAssistant
    ) -> None:
        probe = _probe(hass)

        probe._on_message(f"/open/cert_account/{SKIPPED_SN}/quota", b"\x0a\x01")

        assert probe.topics == ["/open/{acct}/{sn}/quota"]

    async def test_buffer_is_bounded(self, hass: HomeAssistant) -> None:
        probe = _probe(hass)

        for _ in range(RAW_FRAME_LOG_MAX + 10):
            probe._on_message("/topic", b"\x0a\x01")

        assert len(probe.frames) == RAW_FRAME_LOG_MAX

    async def test_capture_failure_never_raises(self, hass: HomeAssistant) -> None:
        """Ingest of a broken frame must not destabilise the installation."""
        probe = _probe(hass)

        with patch(
            "ecoflow_energy.device_probe.build_frame_entry",
            side_effect=ValueError("boom"),
        ):
            probe._on_message("/topic", b"\x0a\x01")

        assert probe.frames == []


class TestConnectSequence:
    """The probe is useless unless all three client steps run in order.

    ``connect()`` only opens the socket. Without ``start_loop()`` nobody
    reads it: no CONNACK, no subscribe, no frames - yet the probe reports
    success and diagnostics show an empty capture, which is
    indistinguishable from a device that sends nothing.
    """

    async def test_connect_starts_the_network_loop(self, hass: HomeAssistant) -> None:
        probe = _probe(hass)
        probe._client.create_client.return_value = True
        probe._client.connect.return_value = True

        assert probe._connect() is True
        assert [call[0] for call in probe._client.method_calls] == [
            "create_client",
            "connect",
            "start_loop",
        ]

    async def test_no_loop_when_connect_fails(self, hass: HomeAssistant) -> None:
        probe = _probe(hass)
        probe._client.create_client.return_value = True
        probe._client.connect.return_value = False

        assert probe._connect() is False
        probe._client.start_loop.assert_not_called()

    async def test_nothing_runs_when_client_creation_fails(
        self, hass: HomeAssistant
    ) -> None:
        probe = _probe(hass)
        probe._client.create_client.return_value = False

        assert probe._connect() is False
        probe._client.connect.assert_not_called()
        probe._client.start_loop.assert_not_called()


class TestStartProbes:
    async def test_no_skipped_devices_starts_nothing(
        self, hass: HomeAssistant
    ) -> None:
        assert await async_start_probes(hass, [], "a@b.c", "pw") == []

    async def test_missing_credentials_starts_nothing(
        self, hass: HomeAssistant
    ) -> None:
        skipped = [{"sn": SKIPPED_SN, "product_name": ""}]

        assert await async_start_probes(hass, skipped, "", "") == []

    async def test_failed_login_is_not_an_error(self, hass: HomeAssistant) -> None:
        """No support either way, so a failed probe login stays quiet."""
        skipped = [{"sn": SKIPPED_SN, "product_name": ""}]
        api = MagicMock()
        api.login = AsyncMock(return_value=False)

        with patch("ecoflow_energy.ecoflow.app_api.AppApiClient", return_value=api):
            assert await async_start_probes(hass, skipped, "a@b.c", "pw") == []

    async def test_probe_started_per_skipped_device(
        self, hass: HomeAssistant
    ) -> None:
        skipped = [
            {"sn": SKIPPED_SN, "product_name": "Ocean 2"},
            {"sn": "SM3ATEST00000001", "product_name": "Smart Meter"},
        ]
        api = MagicMock()
        api.login = AsyncMock(return_value=True)
        api.get_mqtt_credentials = AsyncMock(
            return_value={
                "certificateAccount": "acc",
                "certificatePassword": "pw",
            }
        )
        api.user_id = "user123"

        with (
            patch("ecoflow_energy.ecoflow.app_api.AppApiClient", return_value=api),
            patch.object(
                UnroutedDeviceProbe, "async_start", AsyncMock(return_value=True)
            ),
            patch("ecoflow_energy.device_probe.EcoFlowMQTTClient", MagicMock()),
        ):
            probes = await async_start_probes(hass, skipped, "a@b.c", "pw")

        assert [probe.device_sn for probe in probes] == [
            SKIPPED_SN,
            "SM3ATEST00000001",
        ]

    async def test_device_without_serial_is_skipped(
        self, hass: HomeAssistant
    ) -> None:
        api = MagicMock()
        api.login = AsyncMock(return_value=True)
        api.get_mqtt_credentials = AsyncMock(
            return_value={"certificateAccount": "acc", "certificatePassword": "pw"}
        )
        api.user_id = "user123"

        with patch("ecoflow_energy.ecoflow.app_api.AppApiClient", return_value=api):
            probes = await async_start_probes(
                hass, [{"product_name": "no serial"}], "a@b.c", "pw"
            )

        assert probes == []
