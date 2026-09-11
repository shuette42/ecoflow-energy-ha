# Adding Your Device

A walk through every file a new EcoFlow device touches, written for someone who
runs Home Assistant, reads Python, and has never opened this repository. The
worked example is the WAVE 3 portable air conditioner (serial prefix `AC71`),
which arrived in two pull requests by the maintainer:
[PR #358](https://github.com/shuette42/ecoflow-energy-ha/pull/358) added it
read-only (parser, sensors, binary sensors, translations, docs, tests, 21 files),
[PR #360](https://github.com/shuette42/ecoflow-energy-ha/pull/360) the fourteen
controls the EcoFlow app offers as switches, numbers and selects (27 files).
That split is the one to copy: a read-only device is a complete contribution,
and controls come second in their own PR once the readings are confirmed. Line
numbers below are "around line N" because they shift, the names do not.

## Before you write code

**Is it a new device or an accessory?** Read the opening of the "Adding Device
Support" section in [CONTRIBUTING.md](../CONTRIBUTING.md) first. An accessory
whose data only arrives through the main unit (PowerGlow, PowerPulse on a
PowerOcean) belongs in the existing parser, not in a new device type. Getting
this wrong is the most common reason a device PR has to be rebuilt.

**Open an [issue](https://github.com/shuette42/ecoflow-energy-ha/issues) before
the first line of code**, with the capture attached (next section). The
maintainer confirms the data path, device or accessory, and the mode, and that
answer decides which of the files below you touch.

## Four things you cannot guess

### 1. A capture comes first

No field is added from a message definition, a forum post or another device's
field list. Every field needs a frame your device actually sent: the
definitions describe what a device could send, the frames show what yours does.

To get one: add the device to the integration (it shows up as "not supported
yet" with two diagnostic sensors, which is expected), switch on **Record extra
diagnostic data for 24 hours** in the integration options, wait for the device
to run through its states, then use **Download Diagnostics** on the integration
entry. The download carries the recorded frames with serial numbers masked.
Attach it to the issue. That option is offered only on an entry with account
sign-in (Enhanced Mode), because it records the push stream and Standard Mode
has none. On a Standard Mode entry nothing needs switching on: the diagnostics
download already carries the raw data of an unsupported device.

For a control, the same recording captures what the EcoFlow app writes: with
the recording on, change ONE setting in the app, change it back, download. The
write frame and the device's answer are in the file. That is how the WAVE 3
controls were built, it is the only way to build a control for a model the
maintainer does not own, and it is what "Read-only first" in CONTRIBUTING.md
means: a sensor needs one recording, a control needs a recording of the write.

### 2. Scaling is per field, not per name

The same message mixes watts with tenths of a watt and watt-hours with
kilowatt-hours, and nothing in a field's name tells you which. The proof is the
raw value next to what the EcoFlow app displayed at the same moment, and the PR
description states both. This matters most on energy counters: a sensor with
state class `total_increasing` feeds long-term statistics, and a wrong divisor
there corrupts every user's energy history beyond repair by a later fix. Just
as unforgiving: never publish `0.0` to mean "no value yet". Home Assistant reads
a zero on a `total_increasing` sensor as a meter reset and adds the previous
total again. When a field is absent from a frame, the parser emits no key.

### 3. A control needs read-back

A switch that only sends a frame is not accepted, because a frame that was sent
proves nothing about what the device did with it. The shape the WAVE 3 uses:
the integration sends the exact frame the app sends for that setting, one
setting per frame. The device answers with a push of its own carrying the new
value, usually within 2 seconds. The entity shows the requested value for 5
seconds and from then on what the device reports.

The tests mirror that shape. `tests/test_wave3_commands.py` asserts, per
control, that the builder's bytes equal the app's frame from the recording.
`tests/ha/test_wave3_controls.py` (around line 152) feeds a recorded status
frame, sends one write, and asserts a single publish with the expected field
bytes (`pdata`), and around line 265 it feeds the device's recorded reply.
`tests/ha/test_wave3_entities.py` (around line 331) covers the hold: the
requested value expires onto what the device reported. An entity that sets its
own state after sending, with no device frame behind it, is optimistic state,
and optimistic state is not evidence. Only a device frame recorded outside Home
Assistant counts.

### 4. The device decides the mode, not you

The integration has two modes, never mixed in one entry: Standard Mode uses
Developer API keys and HTTP polling, Enhanced Mode the account sign-in and a
push connection. Some devices exist only on the account connection: the
Developer API lists them but rejects every quota request for their serial with
error `1006`. To see it, add the device to a Standard Mode entry: the Home
Assistant log for the integration then reports `1006` for that serial and the
entities stay unavailable (the "Connection" note under the
[device table in README.md](../README.md#supported-devices) says the same).
Such a device is Enhanced only, like the WAVE 3, and its type goes into
`ENHANCED_ONLY_DEVICE_TYPES` in `const.py` (around line 364) so a Standard Mode
entry never offers it. A device that answers the quota request can be supported
in Standard Mode, and in Enhanced Mode too if it also pushes. Say in the issue
which one you tested.

## The read-only PR, file by file

All paths are under `custom_components/ecoflow_energy/` unless they start with
`tests/`, `documentation/` or are a root file.

| Step | File | What changes | WAVE 3 |
|:---|:---|:---|:---|
| 1 | `ecoflow/const.py` | A device type constant (around line 60), the serial prefix in the prefix map (around 257), the display name by prefix (around 284), and a mention in the `get_device_type` docstring (around 380). The prefix map is what turns a serial into a device type, and every later `if` keys on that type | `DEVICE_TYPE_WAVE3 = "wave3"`, `"AC71": DEVICE_TYPE_WAVE3`, `"AC71": "WAVE 3"` |
| 2 | `ecoflow/parsers/<family>_proto.py` | New file. One function taking the raw payload and returning a flat dict of key to value, with absent fields absent from the dict | `wave3_proto.py`, `parse_wave3_message` around line 426 |
| 3 | `coordinator/mqtt_ingest.py` | Route incoming frames to your parser by device type, at both parse sites inside `_parse_message` (around 499 and 618) | `if self.device_type == DEVICE_TYPE_WAVE3: return parse_wave3_message(payload)` |
| 4 | `coordinator/core.py` | Select the energy-from-power table for your device type (around 453). The tables live in `const.py` (around 7271) and may be empty | `WAVE3_POWER_TO_ENERGY`, `WAVE3_ENERGY_FROM_API` |
| 5 | `coordinator/state_apply.py` | Only if a value must be derived from accumulated state rather than one frame (around 251 and 279). Most devices need nothing here | Firmware to the device registry, active-mode keys re-derived after a mode switch |
| 6 | `const.py` | The display name by device type (around 356), membership in `ENHANCED_ONLY_DEVICE_TYPES` if the device answers `1006` (around 364), and one definition block per platform the device has (see naming below) | `WAVE3_SENSORS` around 6554, `WAVE3_BINARY_SENSORS` around 6753 |
| 7 | `sensor.py`, `binary_sensor.py` | One branch each in the dispatcher returning your block, for the platforms the device has | `sensor.py` around 366, `binary_sensor.py` around 191 |
| 8 | `strings.json`, `translations/en.json`, `translations/de.json` | An entry per entity key in all three, and state translations for every enum option | 104 lines in each |
| 9 | `tests/` | Fixture, parser test, entity test, prefix test, device picker test (table in the test section) | `tests/test_wave3_parser.py`, `tests/ha/test_wave3_entities.py` |
| 10 | `documentation/entities/<device>.md`, `documentation/README.md`, `README.md`, `CHANGELOG.md` | The entity page, one line in each index, one changelog entry | `documentation/entities/wave-3.md` |
| 11 | `coordinator/availability.py` | Only if the device pushes less often than every 35 s: its own stale and unavailable thresholds, plus a row in the README availability table (see step 10) | `WAVE3_STALE_THRESHOLD_S`, `WAVE3_SOFT_UNAVAILABLE_S`, PR #361 |

**Step 2, the parser.** The WAVE 3 parser's module docstring is the model: which
messages the device sends, how often, and which fields each carries. It reuses
the BK-series envelope: `wave3_proto.py` (lines 10-14 and 79-81) decodes the
header with `decode_header_message` from `ecoflow/proto/decoder.py` and takes
the XOR-masked property push and the field walk from `ecoflow/parsers/stream_proto.py`.
The header's `cmd_func`/`cmd_id` pair names the message, and the fixture's
`cmds` column is that pair. A device with a different envelope needs its own
header decode, as the PowerPulse 2 on `cmd_func` 2 has in `powerpulse_proto.py`.
Every key the parser returns is a key a definition in step 6 references: a key
with no definition is silently dropped, a definition with no key sits at `unknown`.

**Step 3, the two parse sites.** Both sit inside `_parse_message` in
`mqtt_ingest.py` (around line 458). The site around 499 handles the `get_reply`
bundle, a protobuf answer to the get-all request the integration sends once,
under a `b"\x0a"` check, and the site around 618 handles the property push. A
device-type branch before the registry lookup at each site is all a new type
needs. A frame whose cmd pair is registered nowhere and matches no branch is
recorded by the capture and otherwise dropped.

**Step 4, energy from power.** Add a pair to `<FAMILY>_POWER_TO_ENERGY` when the
device reports a power but no energy counter for it: the coordinator integrates
the power over time, and the `_kwh` key must then exist as a `total_increasing`
sensor in step 6 (`const.py` around 7271, `"ac_input_power_w": "ac_input_energy_kwh"`).
`<FAMILY>_ENERGY_FROM_API` lists the counters the device reports itself and is
empty for the WAVE 3 (around 7275). An empty table is valid.

**Step 6, the naming convention.** Definition blocks are named
`<FAMILY>_<PLATFORM>` with exactly one family token: `WAVE3_SENSORS`, not
`WAVE_3_SENSORS`. `tests/test_entity_definitions.py` enforces this (around
line 151), because a two-token name is invisible to every reachability and
translation test in the suite. It also fails when a block exists that no
dispatcher returns (around 213) and when a device type has no sensor block
(around 256). Only the sensor block is required: for a platform the device has
nothing on, omit the block and the dispatcher branch in step 7. A definition
carries the key, display name, unit, device class, state class and icon, plus
`suggested_display_precision` where a reading has one and
`disabled_by_default=True` for fields that only exist on some installations (an
optional accessory, a hardware variant). Copy a neighbouring block.

**Step 8, translations.** The entry lives at `entity.<platform>.<key>.name`,
with enum states under `entity.<platform>.<key>.state.<option>`, and the key is
the parser key with no family prefix (`strings.json` around lines 162 and 1484).
`en.json` is a copy of `strings.json`, and `tests/test_translations.py` (around
line 732) fails when they drift. There is no generator, you edit both by hand
and `de.json` alongside. `tests/test_entity_translations.py` checks both
directions (around 91 and 101): every key a definition references has a
translation in every language, and every translation has a definition.

**Step 10, docs.** Copy the first fifteen lines of
`documentation/entities/wave-3.md` as the template: title, one sentence on what
the device is, the totals line, which mode it needs and why, what the device
sends by itself and how often. Then the entity tables. The counts in that totals
line, in `documentation/README.md` and in the `README.md` table are per device,
so keep all three in step, and the `README.md` row has an Energy Dashboard
column counting the sensors made for it (the note under that table says so).
The push cadence you just wrote down decides availability: by default a device
counts as stale after 35 s, degraded after 5 min and unavailable after 10 min
([README.md, "When the Connection Drops"](../README.md#when-the-connection-drops),
with a per-device table). A device that pushes less often than every 35 s needs
its own thresholds (step 11) and a row in that table.

## The controls PR

Only after the read-only PR is merged and the readings are confirmed on
hardware. The WAVE 3 controls PR touched these on top:

| File | What changes | WAVE 3 |
|:---|:---|:---|
| `ecoflow/<family>_commands.py` | New file. A table of controls (key, wire field, allowed values) and a builder that returns the frame bytes the app sends, through the shared builder below | `wave3_commands.py`, `WAVE3_CONTROLS` around line 120, `build_write` around 328 |
| `ecoflow/energy_stream.py`, `ecoflow/proto_encoding.py` | Only if the shared builder `build_delta3_config_write_payload` lacks what your frames need | PR #360 gave it `dest` and a float32 path, and `proto_encoding.py` a fixed32 encoder |
| `ecoflow/parsers/<family>_proto.py` | The parser often grows a field the controls read back | 16 lines in `wave3_proto.py` |
| `coordinator/set_commands.py` | One `async_send_<family>_set` that builds the frame, publishes it, and records the write so a rejection can be attributed | `async_send_wave3_set` around 1243 |
| `coordinator/mqtt_ingest.py` | Recognise your device's write acknowledgment (around 223) and its rejection handling (around 418) | Both `DEVICE_TYPE_WAVE3` branches |
| `const.py` | `<FAMILY>_SWITCHES`, `<FAMILY>_NUMBERS`, `<FAMILY>_SELECTS` blocks | Around 6791, 6805, 6876 |
| `switch.py`, `number.py`, `select.py` | A dispatcher branch each (around 467, 783, 281) and a write branch each in `async_turn_on`, `async_set_native_value`, `async_select_option` (around 270, 440, 203) | All six `DEVICE_TYPE_WAVE3` branches |
| `__init__.py` | Only when the controls replace entities the read-only PR created under the same keys: a retired-keys table per serial pattern (around 241 to 263) removes the old registry entries once. `tests/ha/test_powerstream_registry_cleanup.py` is its test | `_AC71_RETIRED_KEYS_BY_DOMAIN` |
| Translations, entity page, `documentation/README.md`, `README.md`, `CHANGELOG.md` | The new keys with state translations for every select option, and the count lines, which change when controls arrive | Same files as the read-only PR |
| `tests/` | Builder test against the recorded app frames, control tests with read-back, the hold test in the entity test, a reply fixture, and the counts in `tests/test_const.py` and `tests/test_<family>_parser.py` | `tests/test_wave3_commands.py`, `tests/ha/test_wave3_controls.py`, `tests/ha/test_wave3_entities.py`, `tests/fixtures/wave3/ac71_set_reply_20260907.json` |

**The wire field and the shared builder.** Decoding the recorded app write with
`decode_header_message` gives `pdata`, the field bytes, and header field 3,
which is the family's routing `dest` (66 for the WAVE 3, measured against
hardware replies). `wave3_commands.py` builds through the shared
`build_delta3_config_write_payload` in `ecoflow/energy_stream.py`, and a new
family usually extends that builder rather than writing a second one. The builder
test is byte-exact against the recording, which is why that recording comes first.

**The acknowledgment.** The ack site in `mqtt_ingest.py` (around 223) reads
`if self.device_type in (DEVICE_TYPE_DELTA3, DEVICE_TYPE_WAVE3): self._check_config_write_ack(payload)`.
If your device's reply decodes like `tests/fixtures/wave3/ac71_set_reply_20260907.json`,
the new type joins that tuple, otherwise it needs a check of its own. The write
record a rejection is attributed against lives in the coordinator
(`_config_writes_sent` in `core.py`, added in PR #360).

## Tests, and what each proves

| Test file | Proves | WAVE 3 |
|:---|:---|:---|
| `tests/test_<family>_parser.py` | Replays every fixture frame through the parser and asserts the keys and values, including that absent fields are absent | `tests/test_wave3_parser.py`, 620 lines in PR #358 |
| `tests/ha/test_<family>_entities.py` | The entity count per platform, and that a full frame fills the sensors with the expected values | `tests/ha/test_wave3_entities.py`, which also holds the prefix and display-name assertions (around 222 and 225) |
| `tests/test_const.py` | `class TestDeviceTypeRouting` (around line 101) holds the `get_device_type` cases, and a new prefix case goes there. The WAVE 3 put its prefix assertions in its entity test instead | `TestWave3Sensors`, 36 lines of list-length asserts |
| `tests/ha/test_config_flow.py` | The device picker offers the device in the right mode | 15 lines changed |
| `tests/fixtures/<family>/*.json` | The recorded frames, serials replaced by a placeholder | `tests/fixtures/wave3/ac71_frames_plan046.json` |

Fixtures never carry a real serial, a real key, or an unedited diagnostics
download. The masked download is the source: its `raw_frames.frames` list
carries `topic`, `format`, `cmds` and `hex` per recorded frame
(`diagnostics.py` around 696-704, written by `_capture_raw_frame` in
`mqtt_ingest.py` around 278). The WAVE 3 fixture is a `note`, `source_topics`,
and a `frames` list whose entries carry `tag`, `t`, `ts_iso`, `topic`, `size`,
`format`, `cmds` (the cmd pair) and `hex`: the download records copied over,
with `{sn}` where the serial was and a `tag` naming the device state the frame
was recorded in. The WAVE 3 one was assembled with a small script of the
maintainer's, by hand is fine for a handful of frames, and the header line says
which. Tests use placeholder serials such as `AC71TEST00000052`, or `"X" * 16`
where only the length matters, the same idea as `HJ31TEST0001` in CONTRIBUTING.md.

Run these gates while you work (they fail fast and name the missing piece), and
`pytest -q` before opening the PR:

```bash
pytest tests/test_entity_definitions.py tests/test_entity_translations.py tests/test_translations.py -q
```

## Checklist

In commit order, with the WAVE 3 file beside it. `manifest.json` is not on it:
both external device PRs so far (#134, #250) left the version alone, and the
maintainer sets it when the change is scheduled for a release.

- [ ] Issue opened with the diagnostics download attached, path confirmed
- [ ] `ecoflow/const.py` - type, prefix, name, docstring (`DEVICE_TYPE_WAVE3`, `AC71`)
- [ ] `ecoflow/parsers/<family>_proto.py` - parser (`wave3_proto.py`)
- [ ] `tests/fixtures/<family>/*.json`, `tests/test_<family>_parser.py` - masked frames with placeholder serials, fixture replay (`ac71_frames_plan046.json`, `test_wave3_parser.py`)
- [ ] `coordinator/mqtt_ingest.py` - parser routing, both sites in `_parse_message`
- [ ] `coordinator/core.py` - energy-from-power table selection
- [ ] `coordinator/state_apply.py` - only if state-derived values exist
- [ ] `coordinator/availability.py` - only if the device pushes less often than every 35 s, with the README table row
- [ ] `const.py` - display name, `ENHANCED_ONLY_DEVICE_TYPES` if `1006`, `<FAMILY>_SENSORS`, `<FAMILY>_BINARY_SENSORS` if the device has any (`WAVE3_SENSORS`, `WAVE3_BINARY_SENSORS`)
- [ ] `sensor.py`, `binary_sensor.py` - one dispatcher branch each, for the platforms the device has
- [ ] `strings.json`, `translations/en.json`, `translations/de.json` - every key, every enum state
- [ ] `tests/ha/test_<family>_entities.py`, `tests/test_const.py`, `tests/ha/test_config_flow.py` - counts and values, prefix case in `TestDeviceTypeRouting`, picker (`test_wave3_entities.py`)
- [ ] `documentation/entities/<device>.md`, `documentation/README.md`, `README.md`, `CHANGELOG.md` - entity page, one line each with counts in step, one entry under Added (`wave-3.md`)
- [ ] Gate commands green, full suite green
- [ ] PR description states raw value and app display for every scaled field, and how the mode was determined

Controls, second PR:

- [ ] Recording of the app writing each setting, attached to the issue
- [ ] `ecoflow/<family>_commands.py`, `tests/test_<family>_commands.py` - builder, byte-exact against the recording (`wave3_commands.py`, `test_wave3_commands.py`)
- [ ] `ecoflow/energy_stream.py`, `ecoflow/proto_encoding.py` - only if the shared builder lacks what the frames need
- [ ] `ecoflow/parsers/<family>_proto.py` - fields the controls read back, if any
- [ ] `coordinator/set_commands.py`, `coordinator/mqtt_ingest.py` - `async_send_<family>_set`, acknowledgment and rejection branches
- [ ] `const.py` - `<FAMILY>_SWITCHES`, `<FAMILY>_NUMBERS`, `<FAMILY>_SELECTS`
- [ ] `switch.py`, `number.py`, `select.py` - dispatcher and write branches
- [ ] `__init__.py` - retired keys, only when replacing entities, with `tests/ha/test_powerstream_registry_cleanup.py`
- [ ] Translations, entity page, `documentation/README.md`, `README.md`, `CHANGELOG.md` - the count lines change
- [ ] `tests/ha/test_<family>_controls.py`, `tests/ha/test_<family>_entities.py` - send, reply, read-back, the hold (`test_wave3_controls.py`, `test_wave3_entities.py`)
- [ ] `tests/test_const.py`, `tests/test_<family>_parser.py` - the counts that change
- [ ] PR description states how each control was confirmed on hardware
