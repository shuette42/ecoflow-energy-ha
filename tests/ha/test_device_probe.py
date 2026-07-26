"""Tests for the listen-only capture of devices that have no parser yet."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from ecoflow_energy.const import RAW_FRAME_LOG_MAX
from ecoflow_energy.device_probe import UnroutedDeviceProbe, async_start_probes

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
        # enhanced_mode would activate the energy stream switch, which is a
        # publish to the device.
        assert kwargs["enhanced_mode"] is False
        assert kwargs["wss_mode"] is True
        assert kwargs["device_sn"] == SKIPPED_SN

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

    async def test_topics_are_recorded_without_the_serial(
        self, hass: HomeAssistant
    ) -> None:
        """Which topic delivered tells silence apart from undecodable data."""
        probe = _probe(hass)

        probe._on_message(f"/app/{SKIPPED_SN}/thing/property/get_reply", b"\x0a\x01")

        assert probe.topics == ["/app/{sn}/thing/property/get_reply"]

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
