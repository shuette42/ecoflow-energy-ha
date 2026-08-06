"""Tests for the listen-only capture of devices that have no parser yet."""

import itertools
import logging
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from ecoflow_energy.const import (
    PROBE_WATCHDOG_INTERVAL_S,
    RAW_FRAME_KEYS_MAX,
    RAW_FRAME_PER_KEY_MAX,
)
from ecoflow_energy.device_probe import (
    UnroutedDeviceProbe,
    async_start_probe_watchdog,
    async_start_probes,
)
from ecoflow_energy.ecoflow.cloud_mqtt import EcoFlowMQTTClient

SKIPPED_SN = "RE11TEST00000001"


def _probe(hass: HomeAssistant) -> UnroutedDeviceProbe:
    with patch("ecoflow_energy.device_probe.EcoFlowMQTTClient") as mock_client:
        mock_client.return_value = MagicMock()
        probe = UnroutedDeviceProbe(
            hass, SKIPPED_SN, "Ocean 2", "cert_account", "cert_password", "user123"
        )
    return probe


def _fake_clock(step: float) -> Iterator[float]:
    """Return a stand-in for the capture clock.

    Frame timestamps come from ``time.time()`` inside ``build_frame_entry``.
    Driving them from a counter keeps the span assertions deterministic -
    real-clock arithmetic in a test means the assertion measures the
    machine, not the code.
    """
    return itertools.count(1_000_000.0, step)


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

    async def test_a_rebuilt_session_still_transmits_nothing(
        self, hass: HomeAssistant
    ) -> None:
        """The no-write promise has to survive the watchdog's reconnect.

        ``force_reconnect`` throws the paho client away and builds a new
        one. The promise lives on the wrapper (``_listen_only``), not on
        the paho instance - this pins that, so it cannot quietly move to
        somewhere a rebuild would reset.
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
        client.client = MagicMock()

        with patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client") as paho_cls:
            fresh = paho_cls.return_value
            assert client.force_reconnect() is True
            client._on_connect(fresh, None, {}, 0)

        fresh.publish.assert_not_called()
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

    async def test_a_rare_message_type_survives_a_long_capture(
        self, hass: HomeAssistant
    ) -> None:
        """The reported failure, end to end.

        A Stream Micro recorded for six hours delivered 24 frames spanning
        199 seconds, every one of them the same message type. The frames a
        parser is built from - the battery report, the ones carrying state
        of charge - had been pushed out by the most frequent push.
        """
        probe = _probe(hass)
        frequent = [{"cmd_func": 254, "cmd_id": 21}]
        battery = [{"cmd_func": 32, "cmd_id": 50}]
        counter = itertools.count()
        clock = _fake_clock(2.0)

        def headers(_payload: bytes) -> list[dict[str, int]]:
            # One battery report every 200th frame: at a two-second cadence
            # that is roughly one every seven minutes.
            return battery if next(counter) % 200 == 0 else frequent

        with (
            patch("ecoflow_energy.device_probe.decode_cmd_headers", side_effect=headers),
            patch(
                "ecoflow_energy.ecoflow.frame_capture.time.time",
                side_effect=lambda: next(clock),
            ),
        ):
            for _ in range(10_800):  # 6 h at one frame every 2 s
                probe._on_message("/app/device/property/{sn}", b"\x0a\x01")

        frames = probe.frames
        kept_cmds = [frame["cmds"][0] for frame in frames]
        assert {"cmd_func": 32, "cmd_id": 50} in kept_cmds
        assert {"cmd_func": 254, "cmd_id": 21} in kept_cmds
        # Both types keep their full span, not their last few minutes.
        assert frames[-1]["ts"] - frames[0]["ts"] > 6 * 3600 - 5

    async def test_frames_are_exported_oldest_first(
        self, hass: HomeAssistant
    ) -> None:
        """Buckets are internal; a reader sees one chronological stream."""
        probe = _probe(hass)
        clock = _fake_clock(1.0)
        types = itertools.cycle(
            [
                [{"cmd_func": 254, "cmd_id": 21}],
                [{"cmd_func": 32, "cmd_id": 50}],
                [],
            ]
        )

        with (
            patch(
                "ecoflow_energy.device_probe.decode_cmd_headers",
                side_effect=lambda _payload: next(types),
            ),
            patch(
                "ecoflow_energy.ecoflow.frame_capture.time.time",
                side_effect=lambda: next(clock),
            ),
        ):
            for _ in range(60):
                probe._on_message("/topic", b"\x0a\x01")

        timestamps = [frame["ts"] for frame in probe.frames]
        assert timestamps == sorted(timestamps)

    async def test_buffer_is_bounded(self, hass: HomeAssistant) -> None:
        """Neither the frame count nor the number of types may run away."""
        probe = _probe(hass)
        counter = itertools.count()

        def headers(_payload: bytes) -> list[dict[str, int]]:
            # Far more distinct message types than the key budget allows.
            return [{"cmd_func": 254, "cmd_id": next(counter) % 100}]

        with patch(
            "ecoflow_energy.device_probe.decode_cmd_headers", side_effect=headers
        ):
            for _ in range(5_000):
                probe._on_message("/topic", b"\x0a\x01")

        sampling = probe.sampling
        assert sampling["keys_tracked"] == RAW_FRAME_KEYS_MAX
        assert len(probe.frames) <= RAW_FRAME_KEYS_MAX * RAW_FRAME_PER_KEY_MAX
        assert all(
            key["kept"] <= RAW_FRAME_PER_KEY_MAX
            for key in sampling["per_key"].values()
        )
        assert sampling["frames_seen"] == 5_000

    async def test_sampling_reports_what_was_thinned_away(
        self, hass: HomeAssistant
    ) -> None:
        """A short frame list must not look like a silent device."""
        probe = _probe(hass)

        with patch(
            "ecoflow_energy.device_probe.decode_cmd_headers",
            return_value=[{"cmd_func": 254, "cmd_id": 21}],
        ):
            for _ in range(500):
                probe._on_message("/topic", b"\x0a\x01")

        sampling = probe.sampling
        assert sampling["frames_seen"] == 500
        assert sampling["frames_kept"] == len(probe.frames)
        assert sampling["frames_kept"] < sampling["frames_seen"]

    async def test_json_types_do_not_share_one_bucket(
        self, hass: HomeAssistant
    ) -> None:
        """A JSON device's message types are told apart by their own marker."""
        probe = _probe(hass)

        for _ in range(200):
            probe._on_message("/topic", b'{"typeCode": "pdStatus", "soc": 50}')
        probe._on_message("/topic", b'{"typeCode": "bmsStatus", "temp": 20}')

        payloads = [bytes.fromhex(frame["hex"]) for frame in probe.frames]
        assert any(b"bmsStatus" in payload for payload in payloads)
        assert probe.sampling["keys_tracked"] == 2

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


class TestStayingConnected:
    """A 24 hour capture that stops at the first drop records nothing.

    Paho retries a lost session on its own, but it retries with the client
    id it was built with, and this broker refuses one it has already seen.
    Only ``force_reconnect`` - which ``try_reconnect`` calls - builds a new
    one, and the coordinator that normally drives it does not exist for a
    device that was skipped.
    """

    async def test_a_dropped_session_is_rebuilt(self, hass: HomeAssistant) -> None:
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        probe.async_check_connection()
        await hass.async_block_till_done()

        probe._client.try_reconnect.assert_called_once()

    async def test_a_live_session_is_left_alone(self, hass: HomeAssistant) -> None:
        probe = _probe(hass)
        probe._client.is_connected.return_value = True

        probe.async_check_connection()
        await hass.async_block_till_done()

        probe._client.try_reconnect.assert_not_called()

    async def test_watchdog_checks_every_probe(self, hass: HomeAssistant) -> None:
        probes = [_probe(hass), _probe(hass)]
        for probe in probes:
            probe._client.is_connected.return_value = False

        unsub = async_start_probe_watchdog(hass, probes)
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=PROBE_WATCHDOG_INTERVAL_S + 1)
        )
        await hass.async_block_till_done()
        unsub()

        for probe in probes:
            probe._client.try_reconnect.assert_called_once()

    async def test_watchdog_stops_when_unsubscribed(
        self, hass: HomeAssistant
    ) -> None:
        """The capture expires on its own - the timer must go with it."""
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        unsub = async_start_probe_watchdog(hass, [probe])
        unsub()
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=PROBE_WATCHDOG_INTERVAL_S + 1)
        )
        await hass.async_block_till_done()

        probe._client.try_reconnect.assert_not_called()

    async def test_a_stopped_probe_is_not_reconnected(
        self, hass: HomeAssistant
    ) -> None:
        """Unload stops the probes before the timer is cancelled.

        Home Assistant runs the entry's on_unload callbacks - the timer's
        cancel among them - only after async_unload_entry returns, and
        async_unload_entry awaits in between. A tick landing in one of
        those awaits finds the session down (it was just disconnected)
        and, without the guard, rebuilds it into a connection nothing
        ever tears down.
        """
        probe = _probe(hass)
        probe._client.is_connected.return_value = False
        await probe.async_stop()

        probe.async_check_connection()
        await hass.async_block_till_done()

        probe._client.try_reconnect.assert_not_called()

    async def test_a_stop_landing_mid_attempt_still_ends_disconnected(
        self, hass: HomeAssistant
    ) -> None:
        """A reconnect already past the guard re-checks after its attempt.

        The stop's own disconnect can run before the attempt builds its
        fresh session, in which case the stop misses it. The attempt has
        to notice the stop afterwards and tear its own work down again.
        """
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        def stop_lands_mid_attempt() -> bool:
            probe._stopped = True
            return True

        probe._client.try_reconnect.side_effect = stop_lands_mid_attempt
        probe._reconnect()

        probe._client.disconnect.assert_called_once()


class TestConnectionReport:
    """An empty capture has to say why it is empty.

    The reader of a diagnostics download cannot see the machine it came
    from. "connected: false" on its own fits a refused login, a link that
    died hours ago, and a silent device equally well - and a listen-only
    session is demonstrably not silent, a device at rest sends a frame
    every few seconds.
    """

    async def test_refused_session_reports_the_reason(
        self, hass: HomeAssistant
    ) -> None:
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        probe._on_status("connect_failed", 5, "Auth failed")

        report = probe.connection
        assert report["ever_connected"] is False
        assert report["last_rc"] == 5
        assert "Auth failed" in report["last_rc_reason"]
        assert "never connected" in report["verdict"]

    async def test_no_reply_at_all_is_distinguishable(
        self, hass: HomeAssistant
    ) -> None:
        """Nothing came back from the broker - not even a refusal."""
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        report = probe.connection
        assert report["ever_connected"] is False
        assert "last_rc" not in report
        assert report["verdict"] == "never connected - no reply from the broker"

    async def test_a_lost_session_reads_as_lost_not_as_never(
        self, hass: HomeAssistant
    ) -> None:
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        probe._on_status("connected", 0, "Connected")
        probe._on_status("disconnected", 7, "Disconnected (rc=7)")

        report = probe.connection
        assert report["ever_connected"] is True
        assert report["sessions"] == 1
        assert report["disconnects"] == 1
        assert "connected earlier" in report["verdict"]
        # The client's own wording, not the CONNACK table: disconnect codes
        # are a different namespace, and captioning a drop with connect-
        # refusal language ("Bad username/password" for a lost link) would
        # mislead exactly the reader this report exists for.
        assert report["last_rc_reason"] == "Disconnected (rc=7)"

    async def test_ages_are_reported_from_the_capture_clock(
        self, hass: HomeAssistant
    ) -> None:
        with patch("ecoflow_energy.device_probe.time.time", return_value=1000.0):
            probe = _probe(hass)
            probe._on_status("connected", 0, "Connected")
        probe._client.is_connected.return_value = True

        with patch("ecoflow_energy.device_probe.time.time", return_value=1300.0):
            report = probe.connection

        assert report["capture_age_s"] == 300
        assert report["last_connect_age_s"] == 300
        assert report["verdict"] == "listening"

    async def test_reconnect_attempts_are_counted(self, hass: HomeAssistant) -> None:
        """Otherwise a link retried all day looks the same as one never tried."""
        probe = _probe(hass)
        probe._client.is_connected.return_value = False

        probe._reconnect()
        probe._reconnect()

        assert probe.connection["connect_attempts"] == 2

    async def test_a_refused_connect_reaches_the_report(
        self, hass: HomeAssistant
    ) -> None:
        """The real client, not a stand-in for it.

        A refused CONNACK never reaches ``_on_disconnect``, so if the
        client does not report it the probe has no way to learn the reason
        and the capture stays mute about its own failure.
        """
        seen: list[tuple[str, int, str]] = []
        client = EcoFlowMQTTClient(
            certificate_account="acc",
            certificate_password="pw",
            device_sn=SKIPPED_SN,
            message_handler=lambda topic, payload: None,
            user_id="user123",
            wss_mode=True,
            listen_only=True,
            status_handler=lambda status, rc, msg: seen.append((status, rc, msg)),
        )
        paho = MagicMock()
        client.client = paho

        client._on_connect(paho, None, {}, 5)

        assert seen == [("connect_failed", 5, "Auth failed (credentials expired?)")]


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
