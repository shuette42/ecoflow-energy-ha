"""Tests for the Riemann sum energy integrator."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ecoflow_energy.ecoflow.energy_integrator import EnergyIntegrator
from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)

ES22_CAPTURE = (
    Path(__file__).parent
    / "fixtures"
    / "stream_ac5000"
    / "es22_push_capture_masked.json"
)


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "energy_state.json")


@pytest.fixture
def integrator(state_file):
    return EnergyIntegrator(state_file)


def _drive(integrator, readings, *, metric="m", dt=30.0, start=1000.0):
    """Feed readings into the integrator ``dt`` apart on a mocked clock.

    The clock is mocked rather than offset from the real one: a fresh CI
    container has an uptime of seconds, so arithmetic against the real
    monotonic clock can go negative there and nowhere else.
    """
    now = start
    for reading in readings:
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=now,
        ):
            integrator.integrate(metric, reading)
        now += dt
    return integrator.get_total(metric)


def _trapezoid_kwh(readings, dt=30.0):
    """The energy the trapezoidal rule assigns to a series of readings.

    Each interval contributes the mean of the two readings bounding it. This
    is the rule the integrator is being held to, written out, so the tests
    assert against it rather than against a number copied from a past run.
    """
    watt_seconds = sum(
        (first + second) / 2.0 * dt
        for first, second in zip(readings, readings[1:])
    )
    return watt_seconds / 3_600_000.0


class TestStateSnapshot:
    """The read-only view diagnostics reports from."""

    def test_snapshot_reports_the_running_state_not_the_file(self, integrator, state_file):
        """Nothing has been flushed, so the file does not exist yet."""
        integrator._state["solar"] = (5.0, 1000.0, 250.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            integrator.integrate("solar", 250.0)

        snapshot = integrator.state_snapshot()

        assert not Path(state_file).exists()
        assert snapshot["solar"][0] > 5.0
        assert snapshot["solar"][1] == 1030.0
        assert snapshot["solar"][2] == 250.0

    def test_snapshot_includes_a_metric_that_never_produced_a_total(self, integrator):
        """A seeded-once metric is the case a diagnostics reader is after."""
        assert integrator.integrate("solar", 400.0) is None

        snapshot = integrator.state_snapshot()

        assert snapshot["solar"][0] == 0.0
        assert snapshot["solar"][2] == 400.0

    def test_snapshot_is_a_copy(self, integrator):
        """A reader must not be able to disturb the running totals."""
        integrator._state["solar"] = (5.0, 1000.0, 250.0)

        snapshot = integrator.state_snapshot()
        snapshot["solar"] = (999.0, 0.0, 0.0)
        snapshot["invented"] = (1.0, 0.0, 0.0)

        assert integrator.get_total("solar") == 5.0
        assert "invented" not in integrator._state

    def test_snapshot_does_not_read_from_disk(self, integrator, state_file):
        """Diagnostics runs on the event loop - it must not touch the file."""
        Path(state_file).write_text(json.dumps({"solar": [999.0, 1.0, 1.0]}))

        assert integrator.state_snapshot() == {}


class TestBasicIntegration:
    def test_first_call_reports_no_total(self, integrator):
        """First reading seeds state and reports no total.

        It must not report 0.0: the caller would publish that as the sensor
        value, and on a total_increasing sensor a zero reads as a meter reset.
        """
        result = integrator.integrate("solar", 1000.0)
        assert result is None
        # State is seeded even though nothing is reported.
        assert integrator._state["solar"][0] == 0.0

    def test_second_call_integrates(self, integrator):
        """Second call after time delta produces energy."""
        integrator.integrate("solar", 1000.0)
        # Simulate time passing
        integrator._state["solar"] = (0.0, time.monotonic() - 30, 1000.0)
        result = integrator.integrate("solar", 1000.0)
        # 1000W * 30s / 3600 / 1000 = 0.00833 kWh
        assert result is not None
        assert result == pytest.approx(0.00833, abs=0.001)

    def test_zero_power_no_increase(self, integrator):
        """Zero power should not increase the total."""
        integrator.integrate("solar", 0.0)
        integrator._state["solar"] = (0.0, time.monotonic() - 30, 0.0)
        result = integrator.integrate("solar", 0.0)
        assert result == 0.0

    def test_monotonic_never_decreases(self, integrator):
        """Total should never decrease."""
        integrator._state["solar"] = (10.0, time.monotonic() - 30, 500.0)
        result = integrator.integrate("solar", 500.0)
        assert result >= 10.0


class TestGapDetection:
    def test_large_gap_preserves_total(self, integrator):
        """Gap >7min should skip integration but keep total."""
        integrator._state["solar"] = (5.0, time.monotonic() - 500, 1000.0)
        result = integrator.integrate("solar", 2000.0)
        assert result == 5.0  # Total unchanged

    def test_small_delta_skipped(self, integrator):
        """Updates faster than 100ms are skipped."""
        integrator._state["solar"] = (5.0, time.monotonic(), 1000.0)
        result = integrator.integrate("solar", 1000.0)
        assert result == 5.0  # No change


class TestPowerChanges:
    """Every interval is credited with the mean of the readings bounding it.

    A change of more than 50% was once credited with min(last, current)
    instead. Where either reading was zero that minimum was zero, so the
    interval contributed nothing and any metric that kept returning to zero
    counted nothing at all (#177).
    """

    SAMPLES = 240

    @pytest.mark.parametrize(
        ("readings",),
        [
            pytest.param([2000.0] * SAMPLES, id="held-steady"),
            pytest.param(
                [0.0 if n % 2 else 2000.0 for n in range(SAMPLES)],
                id="alternating-with-zero",
            ),
            pytest.param(
                [0.0 if n % 3 else 2000.0 for n in range(SAMPLES)],
                id="two-zeros-then-a-value",
            ),
            pytest.param(
                [1800.0 if n % 2 else 2000.0 for n in range(SAMPLES)],
                id="oscillating-above-zero",
            ),
            pytest.param(
                [100.0 if n % 2 else 2000.0 for n in range(SAMPLES)],
                id="swinging-hard-between-two-nonzero-values",
            ),
        ],
    )
    def test_a_pattern_counts_its_full_trapezoid(self, integrator, readings):
        """The two patterns that touch zero used to count exactly nothing.

        The hard nonzero swing is the case that fails if the minimum comes
        back with only the zero endpoints special-cased: 100 W next to
        2000 W is a change of more than half with neither reading zero, and
        the minimum credited it at 100 W instead of the mean.
        """
        counted = _drive(integrator, readings)

        assert counted == pytest.approx(_trapezoid_kwh(readings))

    def test_alternating_counts_the_same_as_its_mean_held_steady(self, integrator, state_file):
        """Half the time at 2000 W is the same energy as all of it at 1000 W.

        Stated as an equivalence rather than a total, because the point is not
        what the number is but that switching a load on and off does not make
        its energy disappear.
        """
        alternating = [0.0 if n % 2 else 2000.0 for n in range(self.SAMPLES)]
        steady = [1000.0] * self.SAMPLES

        counted = _drive(integrator, alternating)
        reference = _drive(EnergyIntegrator(state_file + ".ref"), steady)

        assert counted == pytest.approx(reference)

    def test_an_isolated_pulse_counts_one_interval_at_its_own_power(self, integrator):
        """A reading seen once between two zeros is worth one interval.

        This is the assertion that fails if the zero-discarding minimum comes
        back: it credited both halves of the pulse at min(0, 2000) = 0.
        """
        counted = _drive(integrator, [0.0, 2000.0, 0.0], dt=30.0)

        assert counted == pytest.approx(2000.0 * 30.0 / 3_600_000.0)

    def test_a_reading_that_stays_at_zero_adds_nothing(self, integrator):
        """The fix must not invent energy where the device reports none."""
        counted = _drive(integrator, [0.0] * 10)

        assert counted == 0.0


class TestSetTotal:
    def test_set_total_monotonic(self, integrator):
        """A lower value beyond tolerance becomes a candidate, not the total."""
        integrator._state["batt"] = (100.0, time.monotonic(), 0.0)
        integrator.set_total("batt", 50.0)  # far below -> unconfirmed candidate
        assert integrator.get_total("batt") == 100.0

    def test_set_total_higher_accepted(self, integrator):
        """set_total accepts a rise within tolerance immediately (ADR-010)."""
        integrator._state["batt"] = (100.0, time.monotonic(), 0.0)
        integrator.set_total("batt", 105.0)  # within max(10% of 100, 5) = 10
        assert integrator.get_total("batt") == 105.0

    def test_set_total_new_metric(self, integrator):
        """set_total creates new metric."""
        integrator.set_total("new", 42.0)
        assert integrator.get_total("new") == 42.0


class TestSetTotalBands:
    """set_total's rise/dip/candidate bands around the stored total (ADR-010).

    Tolerance is max(10% of the stored total, 5 kWh).
    """

    def test_poisoned_value_recovers_after_two_device_readings(self, integrator):
        integrator._state["batt"] = (4_000_000.0, time.monotonic(), 0.0)

        integrator.set_total("batt", 2603.0)
        assert integrator.get_total("batt") == 4_000_000.0  # unconfirmed candidate

        integrator.set_total("batt", 2603.4)
        assert integrator.get_total("batt") == 2603.4  # confirmed

    def test_one_off_jump_never_moves_the_total(self, integrator):
        integrator.set_total("batt", 2603.0)
        integrator.set_total("batt", 4_294_967.295)  # far beyond tolerance
        assert integrator.get_total("batt") == 2603.0
        integrator.set_total("batt", 2603.4)  # agrees with the stored total
        assert integrator.get_total("batt") == 2603.4

    def test_genuine_catch_up_confirms_on_the_second_reading(self, integrator):
        integrator.set_total("batt", 2603.0)
        integrator.set_total("batt", 2900.0)  # beyond tolerance -> candidate
        assert integrator.get_total("batt") == 2603.0
        integrator.set_total("batt", 2900.3)  # confirms the candidate
        assert integrator.get_total("batt") == 2900.3

    def test_small_step_is_accepted_immediately(self, integrator):
        integrator.set_total("batt", 2603.0)
        integrator.set_total("batt", 2603.4)
        assert integrator.get_total("batt") == 2603.4

    def test_repeated_dip_inside_the_band_never_moves_the_total(self, integrator):
        integrator._state["batt"] = (4408.259, time.monotonic(), 0.0)

        integrator.set_total("batt", 4408.258)
        assert integrator.get_total("batt") == 4408.259
        integrator.set_total("batt", 4408.257)
        assert integrator.get_total("batt") == 4408.259

    def test_fresh_small_counter_publishes_every_reading(self, integrator):
        """The 5 kWh floor keeps a young counter out of candidate purgatory."""
        for reading in (0.5, 0.8, 1.3, 2.0):
            integrator.set_total("solar", reading)
            assert integrator.get_total("solar") == reading

    def test_poisoned_installation_recovers_after_restart_via_bands(self, state_file):
        """The end-to-end case from #88 through a state file, not a direct assign.

        Sibling of test_poisoned_installation_recovers_after_restart in
        TestPlausibilityBounds: here the stored total (4,000,000) is itself
        below MAX_TOTAL_KWH, so it survives load_state and must instead be
        displaced by two confirming device readings.
        """
        Path(state_file).write_text(
            json.dumps({"batt_charge_energy_kwh": [4_000_000.0, 100.0, 0.0]})
        )
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()
            integrator.set_total("batt_charge_energy_kwh", 2603.0)
            assert integrator.get_total("batt_charge_energy_kwh") == 4_000_000.0
            integrator.set_total("batt_charge_energy_kwh", 2603.4)

        assert integrator.get_total("batt_charge_energy_kwh") == pytest.approx(2603.4)

    def test_a_poisoned_restored_state_is_healed_by_two_device_readings(
        self, state_file
    ):
        """seed_energy_total no longer calls set_total (ADR-010 addendum A1).

        This test used to assert the opposite of what it asserts now: that
        a poisoned restore (4,000,000, far beyond tolerance) could not move
        a clean integrator total (2603) at all. That was F1 of the
        plan-051-052-energy-guards review - seed_energy_total's caller is
        restore_total, not set_total, and a restored value is not a device
        reading. restore_total takes a restored value above the stored
        total directly, so the poisoned restore becomes the stored total
        (not an unconfirmed candidate), and the recorder books the meter
        reset one poll earlier than the old candidate path did. From there
        it heals exactly like any other poisoned device total: two device
        readings within tolerance of each other, the guarantee ADR-010
        decision 1 already gives a poisoned state file.
        """
        integrator = EnergyIntegrator(state_file)
        integrator.load_state()
        integrator._state["solar"] = (2603.0, time.monotonic(), 0.0)

        integrator.restore_total("solar", 4_000_000.0)  # the restored, poisoned seed
        assert integrator.get_total("solar") == 4_000_000.0

        integrator.set_total("solar", 2603.3)  # a live device reading -> candidate
        assert integrator.get_total("solar") == 4_000_000.0

        integrator.set_total("solar", 2603.5)  # confirms the candidate
        assert integrator.get_total("solar") == 2603.5


class TestRestoreTotal:
    """restore_total: seed_energy_total's entry point (ADR-010 addendum A1).

    A restored Home Assistant sensor state is not a device reading, so it
    does not go through set_total's confirmation bands.
    """

    def test_restore_above_stored_total_is_taken_at_once(self, integrator):
        integrator._state["solar"] = (100.0, 1000.0, 0.0)

        integrator.restore_total("solar", 200.0)
        assert integrator.get_total("solar") == 200.0

        # A device reading 30s later builds on the restored total, not on
        # an unconfirmed candidate - it is already the stored total.
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            total = integrator.integrate("solar", 1000.0)
        assert total is not None and total > 200.0

    def test_restore_at_or_below_stored_total_is_ignored(self, integrator):
        integrator._state["solar"] = (100.0, 1000.0, 0.0)

        integrator.restore_total("solar", 50.0)
        assert integrator.get_total("solar") == 100.0
        assert "solar" not in integrator._candidates

    def test_restore_above_ceiling_is_rejected(self, integrator, caplog):
        integrator._state["solar"] = (100.0, 1000.0, 0.0)

        with caplog.at_level("WARNING"):
            integrator.restore_total("solar", 5.45e7)

        assert integrator.get_total("solar") == 100.0
        assert len(caplog.records) == 1

    def test_restore_seeds_a_metric_without_state(self, integrator):
        integrator.restore_total("fresh", 42.0)
        assert integrator.get_total("fresh") == 42.0

    def test_restore_drops_a_pending_candidate(self, integrator):
        integrator._state["solar"] = (100.0, 1000.0, 0.0)

        integrator.set_total("solar", 300.0)  # beyond tolerance -> candidate
        assert integrator.get_total("solar") == 100.0
        assert integrator._candidates["solar"] == 300.0

        integrator.restore_total("solar", 200.0)
        assert integrator.get_total("solar") == 200.0
        assert "solar" not in integrator._candidates

        # The discarded candidate (300) was measured against the total this
        # restore just replaced. A device reading near it does not confirm
        # anything - it starts a fresh candidate against the new total.
        result = integrator.set_total("solar", 300.2)
        assert result == 200.0
        assert integrator._candidates["solar"] == 300.2


class TestPersistence:
    def test_save_and_load(self, state_file):
        """State survives across instances (explicit load_state call)."""
        i1 = EnergyIntegrator(state_file)
        i1._state["solar"] = (123.456, time.monotonic(), 500.0)
        i1._save_state()

        i2 = EnergyIntegrator(state_file)
        i2.load_state()
        assert i2.get_total("solar") == pytest.approx(123.456)

    def test_missing_file_starts_empty(self, tmp_path):
        """Missing state file starts with empty state."""
        i = EnergyIntegrator(str(tmp_path / "nonexistent.json"))
        i.load_state()
        assert i.get_total("solar") is None

    def test_corrupt_file_starts_empty(self, tmp_path):
        """Corrupt state file starts with empty state."""
        f = tmp_path / "corrupt.json"
        f.write_text("not json{{{")
        i = EnergyIntegrator(str(f))
        i.load_state()
        assert i.get_total("solar") is None

    def test_epoch_timestamp_migration(self, state_file):
        """State files with epoch timestamps (pre-v1.5.1) are migrated on load."""
        import json
        from pathlib import Path

        epoch_ts = 1748000000.0  # Unix epoch from 2025
        data = {"solar": [123.456, epoch_ts, 500.0]}
        Path(state_file).write_text(json.dumps(data))

        integrator = EnergyIntegrator(state_file)
        integrator.load_state()

        # Total must be preserved
        assert integrator.get_total("solar") == pytest.approx(123.456)

        # Timestamp must be migrated to monotonic range (not epoch)
        _, ts, _ = integrator._state["solar"]
        assert ts < 1e9  # monotonic timestamps are never in epoch range

        # Integration must work normally after migration
        integrator._state["solar"] = (123.456, time.monotonic() - 30, 500.0)
        result = integrator.integrate("solar", 1000.0)
        assert result is not None
        assert result > 123.456

    def test_monotonic_clock_reset_after_reboot(self, state_file):
        """State file from before host reboot has last_ts > current monotonic.

        After a host reboot, time.monotonic() restarts near zero while the
        state file retains the old (higher) timestamp. Without migration,
        delta_t_s becomes negative and the integrator is stuck forever.
        """
        from pathlib import Path

        old_uptime_ts = 86400.0  # 24h of uptime before reboot
        data = {"solar": [50.0, old_uptime_ts, 1000.0]}
        Path(state_file).write_text(json.dumps(data))

        # Simulate post-reboot: monotonic is ~10s (fresh boot)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=10.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        # Total must be preserved
        assert integrator.get_total("solar") == pytest.approx(50.0)

        # Timestamp must be reset to current monotonic, not the stale value
        _, ts, _ = integrator._state["solar"]
        assert ts == pytest.approx(10.0)

    def test_integration_works_after_monotonic_reset(self, state_file):
        """After monotonic reset migration, integration resumes normally."""
        from pathlib import Path

        old_uptime_ts = 86400.0
        data = {"solar": [50.0, old_uptime_ts, 1000.0]}
        Path(state_file).write_text(json.dumps(data))

        # Load with post-reboot monotonic
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=100.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        # First integrate call after load: 30s later at 1000W
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=130.0,
        ):
            result = integrator.integrate("solar", 1000.0)

        # 1000W * 30s / 3_600_000 = 0.00833 kWh added to 50.0
        assert result is not None
        assert result == pytest.approx(50.00833, abs=0.001)

    def test_normal_monotonic_not_affected_by_reset_check(self, state_file):
        """Normal case: last_ts < now is not touched by the reboot migration."""
        from pathlib import Path

        data = {"solar": [50.0, 100.0, 1000.0]}
        Path(state_file).write_text(json.dumps(data))

        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        # Timestamp preserved as-is (100.0), not reset to now (200.0)
        _, ts, _ = integrator._state["solar"]
        assert ts == pytest.approx(100.0)


class TestPlausibilityBounds:
    """A single implausible reading must not freeze a counter forever (#88).

    Totals are monotonic, so a value that is wrong by orders of magnitude
    becomes a floor no correct reading can ever pass again. The bounds catch
    that class of fault at every entry point.
    """

    # The value a PowerOcean Plus actually reported against a real 2,638.98 kWh.
    POISONED_KWH = 54_501_280.65

    def test_set_total_rejects_implausible_value(self, integrator):
        integrator.set_total("solar", 2_638.98)
        integrator.set_total("solar", self.POISONED_KWH)
        assert integrator.get_total("solar") == pytest.approx(2_638.98)

    def test_set_total_rejects_infinity_and_nan(self, integrator):
        integrator.set_total("solar", 100.0)
        integrator.set_total("solar", float("inf"))
        integrator.set_total("solar", float("nan"))
        assert integrator.get_total("solar") == pytest.approx(100.0)

    def test_set_total_rejects_negative(self, integrator):
        integrator.set_total("solar", 100.0)
        integrator.set_total("solar", -5.0)
        assert integrator.get_total("solar") == pytest.approx(100.0)

    def test_set_total_accepts_a_genuinely_large_lifetime_counter(self, integrator):
        """500 MWh is a plausible decade of production and must pass."""
        integrator.set_total("solar", 500_000.0)
        assert integrator.get_total("solar") == pytest.approx(500_000.0)

    def test_integrate_rejects_implausible_power(self, integrator):
        """A 1e28 W reading was observed while the Plus payload was misdecoded."""
        integrator._state["solar"] = (12.5, 1000.0, 500.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            result = integrator.integrate("solar", 1e28)
        assert result == pytest.approx(12.5)
        assert integrator.get_total("solar") == pytest.approx(12.5)

    def test_integrate_rejects_infinite_power(self, integrator):
        integrator._state["solar"] = (12.5, 1000.0, 500.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            result = integrator.integrate("solar", float("inf"))
        assert result == pytest.approx(12.5)

    def test_integrate_still_accepts_a_normal_reading(self, integrator):
        """The guard must not disturb ordinary integration."""
        integrator._state["solar"] = (0.0, 1000.0, 1000.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            result = integrator.integrate("solar", 1000.0)
        assert result == pytest.approx(0.00833, abs=0.001)

    def test_load_state_discards_a_poisoned_total(self, state_file):
        from pathlib import Path

        Path(state_file).write_text(
            json.dumps({"solar": [self.POISONED_KWH, 100.0, 0.0], "home": [42.0, 100.0, 0.0]})
        )
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        assert integrator.get_total("solar") is None
        assert integrator.get_total("home") == pytest.approx(42.0)

    def test_poisoned_installation_recovers_after_restart(self, state_file):
        """The end-to-end case from #88, without hand-editing .storage.

        An installation carrying the bad total restarts, the value is dropped,
        and the next correct reading is accepted instead of being discarded
        for being lower.
        """
        from pathlib import Path

        Path(state_file).write_text(
            json.dumps({"solar_energy_kwh": [self.POISONED_KWH, 100.0, 0.0]})
        )
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()
            integrator.set_total("solar_energy_kwh", 2_638.98)

        assert integrator.get_total("solar_energy_kwh") == pytest.approx(2_638.98)

    def test_repeated_rejection_warns_once(self, integrator, caplog):
        """A device stuck on a bad reading must not fill the log."""
        import logging

        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                integrator.set_total("solar", self.POISONED_KWH)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


class TestStreamAc5000Readings:
    """The readings from the device on #177, end to end into a counter.

    Real frames from that unit, through the parser, into the integrator. The
    capture holds one frame per distinct shape rather than a continuous
    recording, so the interval between them is chosen here and the totals are
    not the device's own energy. What the capture does carry is the shape that
    broke the counters: readings that sit at zero and then jump to a value.
    """

    @staticmethod
    def _readings(key):
        frames = json.loads(ES22_CAPTURE.read_text())["frames"]
        values = []
        for frame in frames:
            parsed = parse_stream_ac5000_message(bytes.fromhex(frame["hex"]))
            if not parsed:
                continue
            value = parsed.get(key)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    @pytest.mark.parametrize(
        "key", ["grid_export_power_w", "batt_discharge_power_w"]
    )
    def test_a_counter_climbs_on_readings_that_pass_through_zero(
        self, integrator, key
    ):
        """Both counters read 0,00 on the reporter's dashboard all day."""
        readings = self._readings(key)

        # The capture has to contain the shape for this to prove anything.
        assert any(value == 0.0 for value in readings)
        assert any(value > 0.0 for value in readings)

        counted = _drive(integrator, readings)

        assert counted == pytest.approx(_trapezoid_kwh(readings))
        assert counted > 0.0
