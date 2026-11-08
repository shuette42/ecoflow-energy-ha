# Smart Meter Dual CT - Entity Reference

The EcoFlow Smart Meter Dual CT (`ES81` series) is represented as a separate Home Assistant device in EcoFlow Energy Enhanced Mode, with its own grid measurements and native energy counters.

**Totals:** 17 sensors (16 measurements and one disabled diagnostic), no binary sensors or controls

The meter needs **Enhanced Mode**, using the EcoFlow account sign-in and MQTT push. Select the ES81 meter in the integration's device list. Its own device identity and push stream feed its parser and sensor set, separate from both the Smart Meter (`BK21`) and any STREAM battery. Standard Mode does not offer this profile. A separate Home Assistant device does not mean a local-only connection: this profile receives the meter's cloud MQTT telemetry.

The device sends partial updates: power arrives every few seconds, while voltage, current and energy can arrive less often. Nothing is integrated from power to create the energy readings below.

## Relationship to a linked STREAM

[STREAM 5000 and STREAM AC 5000](stream-ac-5000.md#sensors---smart-meter) can already expose phase power, voltage and current from a linked meter. Those entities remain attached to the STREAM device. This profile adds the ES81 as its own device and reads its own messages; it does not create another set of linked-meter entities on a STREAM coordinator or change the app's meter and group configuration.

The two paths may describe the same physical grid measurements. Choose the source used by your dashboard or automation rather than adding both together. The six native Wh counters below come from the ES81 itself; the STREAM's existing Grid Import Energy and Grid Export Energy are computed from its power-flow readings and should not be treated as identical counters with the same baseline or accounting.

In one saved two-STREAM installation, the reported MASTER carried a populated `LocalMeter` block identifying the Dual CT model, while the SLAVE's block was empty. This explains why linked-meter entities need not appear on both batteries. It does not establish a fixed physical gateway or prove the exact network path. The standalone HA profile does not reconfigure these roles or qualify meter operation without a linked STREAM.

## Sensors

| Entity | Unit | Default | Description |
|:---|:---:|:---:|:---|
| Grid Power | W | enabled | Signed total power at the grid connection, positive for import and negative for export |
| Grid L1 Power | W | enabled | Signed active power on phase L1, positive for import and negative for export |
| Grid L2 Power | W | enabled | Signed active power on phase L2, positive for import and negative for export |
| Grid L3 Power | W | enabled | Signed active power on phase L3, positive for import and negative for export |
| Phase A Voltage | V | enabled | Line voltage on L1 |
| Phase B Voltage | V | enabled | Line voltage on L2 |
| Phase C Voltage | V | enabled | Line voltage on L3 |
| Phase A Current | A | enabled | Native signed current on L1, positive for import and negative for export |
| Phase B Current | A | enabled | Native signed current on L2, positive for import and negative for export |
| Phase C Current | A | enabled | Native signed current on L3, positive for import and negative for export |

L1, L2 and L3 correspond to phase A, B and C. Current is read directly from the meter, not calculated from power and voltage. Its sign follows the phase power on the measured unit: positive for import and negative for export. A reference meter's unsigned current magnitude can therefore agree while its sign differs.

## Sensors - Energy

| Entity | Unit | Default | Description |
|:---|:---:|:---:|:---|
| Grid Import Energy | Wh | enabled | Native cumulative import, summing the importing phases before whole-house netting, published as `total_increasing` |
| Grid Export Energy | Wh | enabled | Native cumulative export, summing the exporting phases before whole-house netting, published as `total_increasing` |
| Grid Net Energy | Wh | enabled | Native signed net counter, import minus export, published as `total` |
| Phase A Net Energy | Wh | enabled | Native signed net counter on L1, published as `total` |
| Phase B Net Energy | Wh | enabled | Native signed net counter on L2, published as `total` |
| Phase C Net Energy | Wh | enabled | Native signed net counter on L3, published as `total` |

**Choose the grid accounting you want before using the Energy Dashboard.** Grid Import Energy and Grid Export Energy can fill its grid consumption and return-to-grid slots. They count import and export on separate phases at the same time. For example, importing 100 W on one phase and exporting 100 W on another for one hour can add 100 Wh to each directional counter, while net energy changes by about zero. A billing meter that nets all phases first can count that interval differently. These entities preserve the device's accounting and do not reconstruct billing-netted energy.

Grid Net Energy and the three phase net counters can decrease and become negative. They are plain totals rather than monotonic counters and do not belong in the Energy Dashboard's directional grid slots. None of the six is a daily counter. On the measured unit the counters continued across local midnight; a general device-reboot or factory-reset convention has not been verified.

**Counter decreases and restarts.** The integration's existing in-session guard can hold a directional counter at its previous value when the device reports a lower one, including after a device counter reset. After a Home Assistant restart, the first fresh native value can replace a higher restored value. There is no persistent reset compensation or offset adjustment. The net counters are allowed to fall normally. Device resets have not been qualified as universally handled.

## Sensors - Diagnostics and missing readings

| Entity | Unit | Default | Description |
|:---|:---:|:---:|:---|
| Reported Wi-Fi Signal | - | disabled | Reported protocol signal value, with no assumed unit or calibration |

The optional reported Wi-Fi signal comes directly from field602. The EcoFlow
app 6.17.7 Smart Meter Dual view reads it as `moduleWifiRssi` and uses it for
the Wi-Fi icon. Retained ES81 messages contain changing float32 values for
this field. The app mapping does not establish a dBm unit or calibration,
so this diagnostic deliberately has no unit, signal-strength class or
long-term statistics class. It is a protocol observation rather than
a qualified signal-quality threshold. Its value may remain cached between
explicit field updates, like the other partial readings. No extra polling is
introduced. The parser and coordinator retain the native float32 value; the
entity presents it rounded to two decimal places without scaling. Ambiguous
duplicate signal fields are omitted without rejecting valid grid readings.
Do not use it as a calibrated automation threshold.

The two common cloud diagnostics, MQTT Status and Connection Mode, are disabled by default and are additional to the 17 defined sensors. Enable them under **Settings > Devices & services > Entities**, with disabled entities shown.

A missing field is not replaced with zero. A field that has never been received stays unknown; a partial update can leave an earlier reading in place. Availability follows the device connection, not the age of each individual field. Continued power updates therefore do not prove that a displayed voltage, current or counter was measured just as recently. The standard Enhanced Mode availability windows apply; see [When the Connection Drops](../../README.md#when-the-connection-drops).

## Evidence and scope

The grid profile was checked on one Smart Meter Dual CT with firmware **1.0.0.41**. Short paired readings against a Shelly reference supported the voltage and current units and phase mapping, including a negative current on an exporting phase. The small number of paired readings is not a calibration of either meter. Overnight counter changes supported the Wh scale and phase-directional accounting, with offsets and differences between meters remaining possible.

The native counters have not been shown to use the same reporting period or baseline as the totals displayed in the EcoFlow app. Available app screenshots and nearby native counter records did not agree within display rounding. Their exact correspondence remains unconfirmed; no scale correction or fixed offset is applied to make them match.

**The second measurement path was disconnected on that installation.** No PV measurement from that path was qualified, so no PV entity or second-path support is claimed here. The related request in [#596](https://github.com/shuette42/ecoflow-energy-ha/issues/596) remains open. Exact hardware variant suffixes and behavior on other firmware versions remain unverified.

The Reported Wi-Fi Signal label is supplied in English in every language file.
Its German, French, Italian, Dutch and Polish translations remain for the maintainer.

No controls are exposed. The profile does not send meter settings or derive counters with a power integrator.

The ES81 parser validates the protobuf wire types of command metadata before
decoding readings. A malformed header cannot update retained sensor values;
this validation is specific to ES81 and does not change other device parsers.
