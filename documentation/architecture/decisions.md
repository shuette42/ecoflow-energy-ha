# Architecture decisions

The register of the decisions behind the EcoFlow Energy integration, one entry
per decision, oldest first. Code comments and tests cite entries by number
(`ADR-NNN`); a number never changes and is never reused. Each entry records the
context at the time, the decision, the trade-offs, the alternatives that were
rejected and why, and what the decision changed in the code. A later decision
that changes an earlier one says so; the earlier entry gets an addendum pointing
forward and is not rewritten.

Numbers not in this register: ADR-001 and ADR-003 concern how the repository is
worked on rather than how the integration behaves and stay internal (ADR-003 was
assigned twice; both records are internal).

ADR-009 was reserved for the wallbox write decision until 2026-09-11 and stands
at the end of this register, after ADR-028, in the order decisions were taken.

Measurements quoted below were taken on captures, downloads and fixtures that
are described by what they are rather than where they sit; see
[README.md](README.md) for why.

---

## ADR-002: Manual varint encoding for SET commands (not protobuf-generated code)

**Status:** Accepted
**Date:** 2026-03-31
**Context:** SET command payloads need to be encoded as protobuf binary for WSS MQTT. Two options: use protoc-generated Python code or manual varint encoding.

**Decision:** Continue using manual varint encoding in `energy_stream.py` for SET command payloads.

**Trade-offs:**
- (+) Zero dependency on protoc toolchain for SET commands
- (+) Full control over which fields are included (critical for oneof semantics)
- (+) Minimal code footprint (3 helper functions + payload builders)
- (+) Proven working (EnergyStreamSwitch + SysBatChgDsgSet both work)
- (-) Manual encoding is error-prone for complex messages
- (-) No automatic validation against proto schema
- (-) Each new SET command needs a new builder function

**Alternatives considered:**
1. protoc-generated Python code for all messages - rejected: adds build dependency, overkill for ~15 simple SET messages
2. Hybrid (generated for complex, manual for simple) - considered for future if TOU scheduling messages prove too complex

**Consequences:** Each new SET command gets a `build_*_set_payload()` function in `energy_stream.py`. Review must verify field numbers against the message definitions on file. The pattern is established and well-tested.

---

## ADR-004: GitHub Rulesets over legacy branch protection

**Status:** Accepted
**Date:** 2026-03-31
**Context:** Need to protect the main branch from accidental force-pushes and unvalidated merges. GitHub offers legacy branch protection rules and the newer Rulesets system.

**Decision:** Use GitHub Rulesets (not legacy branch protection) for all branch and tag protection.

**Trade-offs:**
- (+) Multiple rulesets can stack (most restrictive wins)
- (+) Covers branches AND tags in one system
- (+) Bypass actors configurable (maintainer can bypass PR requirement)
- (+) API-manageable via REST
- (-) Legacy API (`branches/main/protection`) returns 404, confusing any check that reads the legacy endpoint
- (-) Newer, less community documentation available

**Consequences:** A check of the branch protection must read the rulesets API endpoint, not just the legacy branch protection endpoint. Current ruleset `main-protection` requires status checks (Run tests, HACS Validation, Hassfest) and non-fast-forward. Needs extension: tag protection for `v*`.

---

## ADR-005: SemVer beta numbering - only beta counter increments during pre-release

**Status:** Accepted
**Date:** 2026-04-04
**Context:** During the v1.9.0 beta cycle, each bug fix incorrectly bumped the PATCH version (`v1.9.0-beta.4` -> `v1.9.1-beta.1` -> `v1.9.3-beta.1`), creating phantom versions (1.9.0, 1.9.1, 1.9.2) that were never released. The CHANGELOG accumulated separate blocks for versions that never existed. manifest.json showed `1.9.3` even though `v1.9.0` had not been released yet.

**Decision:** During a beta phase, only the beta number increments. The MAJOR.MINOR.PATCH part stays fixed at the target release version. PATCH increments are reserved for hotfixes after a full release exists.

**Trade-offs:**
- (+) No phantom versions in tag history
- (+) CHANGELOG stays clean with one block per actual release
- (+) manifest.json reflects what users will get
- (+) Consistent with SemVer 2.0 spec (pre-release identifiers)
- (-) Requires discipline during fast beta iteration (natural instinct is to bump PATCH)

**Alternatives considered:**
1. Keep bumping PATCH per beta fix - rejected: creates unreleased version numbers, confusing tag history, CHANGELOG bloat
2. Use date-based beta suffixes (`v1.9.0-beta.20260402`) - rejected: harder to track sequence, HACS does not sort these correctly

**Consequences:** During a pre-release only the beta counter is incremented by the release procedure; a commit made during an active beta phase never bumps PATCH; the version check that runs on commit stays informational.

---

## ADR-006: Graduated availability degradation instead of binary cutoff

**Status:** Accepted
**Date:** 2026-04-05
**Context:** Real PowerOcean telemetry has observed gaps up to 613 seconds (captures of message 33 on file). The previous 95-second hard-unavailable cutoff (35s stale + 60s grace) caused unnecessary entity unavailability during normal device behavior. The manufacturer's app uses a multi-stage approach where devices degrade gracefully.

**Decision:** Replace binary available/unavailable with four graduated stages:
- `healthy`: data within stale threshold (< 35s)
- `stale`: reconnect active, entities available (35s - 5min)
- `degraded`: data old but entities available with last-known values (5min - 10min)
- `unavailable`: entities go unavailable in HA (> 10min)

Force-reconnect triggers at stale threshold (unchanged) but is decoupled from entity availability. Device-specific thresholds: PowerOcean/Delta soft=5min hard=10min, SmartPlug soft=6min hard=10min.

**Alternatives considered:**
1. Simply increase the grace period to 600s - rejected: still binary, no intermediate diagnostic state
2. A full per-device profile and transport-policy layer - rejected: over-engineering for 3 devices
3. Keep 95s cutoff and accept the unavailability - rejected: real user impact, devices appear broken during normal operation

**Consequences:** Entities remain available with stale values for up to 10 minutes. HA recorder writes old values during degraded phase (acceptable - better than unavailable gap). The `availability_stage` property enables future diagnostic sensors. At >5 device types, consider per-device threshold profiles.

---

## ADR-007: PowerOcean accessories (PowerGlow, PowerPulse) are EMS subfields, not device types

**Status:** Accepted, superseded in part by ADR-008 (2026-08-24) for the serial prefix `C376`. In force unchanged for `HF33` and `AC31`.
**Date:** 2026-07-30
**Context:** Issue #7 asks for PowerGlow (SN prefix HF33, 9 kW heating rod) and PowerPulse 2 (SN prefix C376, 11 kW wallbox) support, and a community contributor offered a PR. Two integration points were plausible. Both accessories appear as separate entries in the account device list, so they currently hit the unsupported-device skip path (`__init__.py:157-184`), which argues for new device types. But the official EcoFlow Developer Portal PowerOcean profile lists accessory telemetry as PowerOcean quota keys - PowerGlow as `hrEnergyStream[].temp` / `hrEnergyStream[].hrPwr`, PowerPulse as `evPwr` / `chargingStatus` / `errorCode` (the vendor's Developer Portal profile for PowerOcean, repeated in its GetAllQuotaResponse example), and PowerOcean history indices already account accessory energy (`pv_to_powerglow`, `pv_to_powerplus`). Independently, evcc issue 27677 reports that a direct Developer API query against a C376 serial returns error 1006 and that all PowerPulse data is only reachable through the PowerOcean serial acting as gateway.

**Decision:** Treat both accessories as subfields of the PowerOcean data model. Telemetry is parsed in `ecoflow/parsers/powerocean.py` and surfaced as additional `POWEROCEAN_SENSORS` entries under the existing PowerOcean HA device, all `disabled_by_default=True`. No new device types, no new parsers, no new `_SN_PREFIX_MAP` entries. Writes are out of scope until a SET command is confirmed against firmware.

**Trade-offs:**
- (+) Works in Standard mode, where error 1006 makes a dedicated accessory coordinator impossible
- (+) Reuses the existing flattened-container parse pattern (`ems_change_report.*`, `pcs_change_report.*` at `powerocean.py:392-406`) - no new architecture
- (+) Contributor effort stays in two files plus tests, reviewable without owning the hardware
- (-) Accessory entities live under the PowerOcean device, not their own HA device, so the HA device tree does not mirror the physical installation
- (-) Every PowerOcean owner gets accessory entity definitions registered (disabled), because entity creation is unconditional per device type (`sensor.py:57-62`)
- (-) The accessories keep logging one unsupported-device WARNING each at setup until a separate decision suppresses it

**Alternatives considered:**
1. Own device types `powerglow` / `powerpulse` with own parsers (the approach first considered on 2026-03-15) - rejected: error 1006 means a Standard-mode coordinator for these serials can never fetch data. Same constraint that made PowerOcean Plus R371/R374 Enhanced-only (`ecoflow/const.py:64-71`) and WAVE 3 (AC71) Enhanced-only.
2. Route the accessory serials to the PowerOcean parser via `_SN_PREFIX_MAP` - rejected: creates a full duplicate PowerOcean entity set per accessory, and the accessory serial cannot answer a quota request anyway.
3. Wait for Enhanced-mode protobuf coverage before doing anything - rejected: the HTTP quota path is documented by the vendor and needs no protocol analysis, so it delivers read-only value first.

**Consequences:** The PowerOcean sensor count in README and `documentation/` changes, so the device table needs updating in the same PR. The unsupported-device WARNING for HF33/C376 remains until handled separately. Exact quota key spelling is unconfirmed - the vendor doc and the reporter disagree on form (`hrEnergyStream[].hrPwr` vs `ems_heating_rod.heatingPower`), so a raw quota dump from a PowerOcean with both accessories attached gates implementation. Absent accessories must yield no key at all rather than `0.0`, per the phase-container contract (`tests/test_powerocean_bundled_frames.py:270-282`) and the `total_increasing` zero rule.

### Addendum 2026-09-10: the accessory shape shipped, entities are created on a reported reading, and the PowerGlow stays an accessory by ADR-008's own criterion

**What shipped, on two paths.** The four PowerGlow readings (drawn power, water temperature, target power, target temperature) exist as `POWEROCEAN_SENSORS` entries since v1.16.0, read from the polled quota under `ems_heating_rod.<name>`, each name looked up under every spelling the accessory is known to use. Since v1.18.0 the same four are also filled with account sign-in, from the report the PowerOcean forwards on message `(212, 8)`, decoded in `parsers/powerocean_proto.py` and remapped onto the same four keys, roughly every seventeen seconds; that rests on a diagnostics download from @Xygen of just under ten hours. So the decision's "parsed in `ecoflow/parsers/powerocean.py`" reads today as "parsed by the PowerOcean parsers, quota and stream"; there is still no new parser module and no prefix entry, which is what the decision meant.

**Two sentences of this record are retired.** The trade-off "every PowerOcean owner gets accessory entity definitions registered (disabled), because entity creation is unconditional per device type" stopped being true in v1.16.0: a definition marked `accessory=True` is skipped at setup until the device has reported the reading, added without a reload when the first report arrives, and a leftover entry from an earlier release is dropped on the first update that carries device data without the accessory (`sensor.py:80-84`, `_watch_for_accessory`, `_drop_stale_accessory_entity`). A PowerOcean owner without a PowerGlow has no heating-rod entity at all. And the gate in the Consequences, "a raw quota dump from a PowerOcean with both accessories attached gates implementation", is closed: the quota spelling was settled as `ems_heating_rod.*` with candidates per reading, and the stream path was added beside it. The unsupported-device WARNING for `HF33` at setup remains, as the Consequences say.

**The PowerGlow stays an accessory.** On #7 on 2026-09-09 @Xygen asks for the PowerGlow as its own device, as the PowerPulse 2 is becoming under ADR-008. ADR-008's criterion answers it without a new investigation: if the PowerOcean carries the accessory, it stays an accessory. The PowerOcean carries the PowerGlow on `212`, in the captures on file that include one, his own among them, and that report is what fills the four entities today. The PowerPulse 2 is the exception the criterion produces, because the PowerOcean does not carry it, not a precedent for moving every accessory. This addendum is the record of that answer so a further request does not reopen it; what a later capture could change is the criterion's input, not the criterion.

---

## ADR-008: PowerPulse 2 (C376) is its own device; ADR-007 stands for everything else

**Status:** Accepted (decided 2026-08-24; implementation pending, no code on `main` as of 2026-09-10, to ship as a 1.21.0 pre-release; condition 1 met by ADR-024 since PR #380, see the addendum of 2026-09-10)
**Date:** 2026-08-24
**Supersedes:** ADR-007, for the serial prefix `C376` only. ADR-007 remains in force for `HF33` and for the PowerPulse 1 (`AC31`).

**Context:** ADR-007 rested on one premise, taken from the vendor profile and from evcc issue 27677: that all PowerPulse data is reachable through the PowerOcean serial acting as gateway, and that a direct query fails with error 1006. The second half is still true. The first half is not, and our own evidence contradicts it. A diagnostics download from the reporter on #247 (949 s, a capture on file, account sign-in) shows the PowerOcean carrying an accessory family on `212` whose payload is a heating rod (two temperatures, a target, 1500, 100 - matching the `HF33` on the same account) and **no wallbox family at all**. The four values ADR-007 expected to arrive as PowerOcean quota keys (`evPwr`, `chargingStatus`, `errorCode` and the session totals) do not arrive on that path for this device.

The wallbox's own topic, meanwhile, is fully readable. Ten of its eleven message types set `enc_type=1`, the XOR of the payload with `seq & 0xFF` that the envelope declares and that the repo already implements in three places; the schema-free analyzer does not apply it, which is why a first pass showed only noise. With the mask undone all 75 payload-carrying frames walk cleanly and carry both live values and a read-back for every setting the app writes: maximum output current, solar minimum current, custom-mode current, work mode, the settings bitmask and the smart-mode target. The capture's own buffer was not saturated (11 of 12 key slots, zero frames dropped), and all eleven types are device pushes.

Error 1006 is not an obstacle here. Enhanced-only device types are the house pattern, not an exception: `J32D`, `J32E`, `R371`, `R374`, `HJ3C`, `BK01` and `AC71` all exist for the same reason.

**Decision:** The PowerPulse 2 becomes its own Enhanced-only device type with its own parser (`ecoflow/parsers/powerpulse_proto.py`), its own sensor list in `const.py`, and one `_SN_PREFIX_MAP` entry for `C376`. The PowerPulse 1 stays an accessory on the PowerOcean, unchanged, and so does the PowerGlow.

The separation criterion is **not the model**. It is:

> If the PowerOcean carries the accessory, it stays an accessory. If it does not, it is a device of its own.

`AC31` is carried on `209`, `HF33` on `212`, `C376` not at all. The distinction therefore lives in `_SN_PREFIX_MAP` and nowhere else: no model check in the parsers, no second constant, and no family set reuniting what the split just separated. That last failure is on record from the ES21 work.

**Conditions, both binding before implementation:**

1. **The command registry is split per device family.** `_build_cmd_registry()` in `ecoflow/proto/runtime.py` is keyed globally on `(cmd_func, cmd_id)`, and `241/44` and `96/34` occur on the wallbox channel *and* on the PowerOcean channel with different meanings. A wallbox parser writing into that same table loses the separation silently, on the first collision, with no test failing. The resolution is a namespace per family, which is how `stream_proto.py` and `stream_ac5000_proto.py` already resolve their own messages. This is a condition of this ADR, not a cleanup to schedule.
2. **A capture during a real charging session exists.** Without a car connected, every candidate for charging power, session energy, duration and state stood still or empty, so no sensor can be named yet. The same capture also settles whether the PowerOcean carries a wallbox family once a session is running - the only question the settings-only capture could not answer, and one its saturated PowerOcean buffer (20 of 20 key slots, 48 frames discarded without their type) could not have answered either.

**Writes stay out of scope**, and for a structural reason rather than caution. The app writes wallbox settings through the *PowerOcean's* serial: all thirteen `241/102` writes and their thirteen replies sit in the PowerOcean's frame buffer, and the wallbox buffer holds no `/set` message at all. A control built on that would put the command on one channel and its confirmation on the other, which is exactly the mixing this integration refuses. Whether the wallbox accepts writes on its own topic is unevidenced.

**Trade-offs:**
- (+) The HA device tree mirrors the physical installation for the device where it matters, since the wallbox is a device the user bought separately
- (+) The `C376` owner gets values that ADR-007's shape cannot deliver at all, rather than five entities that stay empty forever
- (+) The PowerOcean path is untouched: one new parser, one dispatch line, additive constants
- (+) Nothing breaks for existing users, see below
- (-) A second MQTT device on the app channel per installation, with its own connection lifecycle to keep healthy
- (-) Two shapes now exist for wallboxes, one accessory and one device, and the criterion has to be read to know which applies
- (-) `AC31` owners keep entities on the PowerOcean device while `C376` owners get a separate device, so two installations of nominally the same accessory look different in HA

**Alternatives considered:**
1. Keep ADR-007 unchanged and wait for a firmware or vendor change that puts the wallbox on the PowerOcean path - rejected: it makes the feature depend on something nobody controls, and the data is already reachable today.
2. Move `AC31` to the new device type as well, for one shape instead of two - rejected: its own channel speaks JSON where the `C376` speaks protobuf, its meter values only appeared on a polled `get_reply` topic that the listen-only probe never requests, and the PowerOcean demonstrably carries it. Building that on inference would be exactly the contribution shape this repo turns down.
3. Route `C376` to the PowerOcean parser via `_SN_PREFIX_MAP` - rejected for the same reason ADR-007 rejected it: a full duplicate PowerOcean entity set per accessory.

**Consequences:** The five shipped `ev_*` sensors need no migration and no release note. They carry `accessory=True` and are created only once their key has been reported, so a `C376` owner never received them: the new device is a first fill, not a move, and an `AC31` owner keeps every entity id. Moving the `AC31` sensors later **would** be a break, needing a registry migration - the recommendation is never to do it while the PowerOcean carries the values.

The unsupported-device WARNING for `C376` disappears once the device type exists; the one for `HF33` stays. Work does not begin inside the 1.18.0 beta cycle: a second device is new scope, not the finishing of something already started.

### Addendum 2026-09-08: both conditions revisited, and the no-migration consequence withdrawn

**Condition 2 is met.** A capture during a real charging session now exists: a 7 h
diagnostics download from a second wallbox owner (a capture on file). The live
values sit on `2/33` of the wallbox's own channel, the same message that already
carries the settings read-back, not on `2/40`, `2/41` or `2/42` as had been guessed
before the capture. Six values are readable for an installation with no PowerOcean at
all: charging power, session energy, total energy, status, the configured maximum
current (field 17) and the phase mode (field 21, `phase_ctrl`, 1 in all six
single-phase frames and 0 in all five three-phase ones). Two controls each check
out: `44 - 43 == 42` in 11 of 11 frames, and reported power sits below voltage
times current by an amount a power factor explains. Condition 1, the per-family
command registry, is untouched and still binding.

**The account this ADR did not anticipate.** That owner's account holds a `C376` and
no PowerOcean. Under the accessory design such an installation gets
nothing at all, because there is no PowerOcean to carry the values. That is not an
edge case: the wallbox is sold on its own and is not tied to a PowerOcean. It is the
strongest argument for the split and it is stronger than the protocol argument the
ADR was decided on.

**The "Consequences" paragraph above is wrong as of 2026-08-28 and is withdrawn.** It
reads that the five `ev_*` sensors "need no migration and no release note" because a
`C376` owner never received them, so the new device would be "a first fill, not a
move". That was true on 2026-08-24 and the accessory relay that shipped four days
later changed it: `(241, 3)`
is registered in `ecoflow/proto/runtime.py:169` with `_is_pile_charging_param`, and
`ecoflow/parsers/powerocean_proto.py:675-783` maps it onto the same five keys. Since
`v1.18.0-beta.27`, stable in `v1.18.0`, a `C376` owner does receive them - on the
PowerOcean device, with the PowerOcean serial in the unique id
(`sensor.py:183`, `f"{coordinator.device_sn}_{definition.key}"`).

A device of its own means a coordinator of its own, so the unique id changes and the
five entities are replaced rather than moved: history, dashboards and automations that
name them break. The tree has no unique id migration at all (`async_migrate_entries`
appears nowhere; ADR-018 withdrew ids instead of migrating them for exactly this
reason), so building one is part of the work rather than a detail of it.

**Therefore, added as a third binding condition:** the five `ev_*` entities are carried
over by a registry migration that rewrites `<powerocean_sn>_<key>` to `<c376_sn>_<key>`
on the first start after the upgrade. Without it the split costs an existing owner three
weeks of history to buy a benefit he does not have.

Affected are owners of a PowerOcean **and** a `C376`; on GitHub that is @Xygen (#1, #4,
#247). Unaffected: the PowerPulse 1 (`AC31`, #27, #245), the PowerGlow (`HF33`), and
Standard Mode entirely, where the `C376` is refused with error 1006 and no entity ever
existed.

### Addendum 2026-09-09: condition 3 is narrowed to the keys that survive, and paired with removing `(241, 3)`

The third condition added on 2026-09-08 was written before the wallbox parser existed.
It says the five `ev_*` entities "are carried over by a registry migration that
rewrites `<powerocean_sn>_<key>` to `<c376_sn>_<key>`". Two facts measured on
2026-09-09, once `ecoflow/parsers/powerpulse_proto.py` was on the branch, make that
enumeration wrong in one direction and incomplete in another.

**Only four of the five keys have a counterpart.** The wallbox's own channel carries no
vehicle identity; `ev_vehicle_id` has no field on `2/33` or `2/34` and none is
evidenced anywhere in the 7 h capture. Migrating it would place a permanently
unavailable row on the new device, which is the outcome `_async_remove_withdrawn_entities`
was built to prevent. It is therefore **withdrawn** via
`_WITHDRAWN_ENTITY_SUFFIXES` (`_ev_vehicle_id`), the instrument ADR-018 introduced,
applied to the one key whose model genuinely ends here.

**The status key is one enum under two names.** `powerocean_proto.py` decodes the
`(241, 3)` status through `_PILE_STATUS_BY_NUMBER`, whose own comment records that the
241 family numbers its status as the 209 family does. The wallbox's field `1` carries
the same numbers with the same meanings. The parser's `ev_plug_status` is therefore
renamed to the existing `ev_charge_status` and reuses its definition and its option
strings, rather than registering a synonym for a value the tree already addresses.
`ev_session_status` (field `101`) is a different number on the wire and keeps its own
key.

**A migration alone produces the value twice, so `(241, 3)` leaves the PowerOcean
table in the same release.** If the tuple stays registered, the PowerOcean parser keeps
filling those keys, `sensor.py` sees the reading reported and recreates the very unique
ids the migration renamed away - two devices publishing one wallbox's charging power.
ADR-024's per-device-type registry makes the removal a one-line change that cannot
reach another family. This is not an optimisation of the migration; without it the
migration is incorrect.

**Condition 3 therefore reads:** four `ev_*` entities (`ev_charge_power_w`,
`ev_session_energy_wh`, `ev_session_duration_s`, `ev_charge_status`) are carried over
by a registry migration rewriting `<powerocean_sn>_<key>` to `<c376_sn>_<key>` before
the platforms are set up; `ev_vehicle_id` is withdrawn; and `(241, 3)` is removed from
the PowerOcean command table in the same release. The migration runs only when the
config entry holds exactly one PowerOcean coordinator carrying the old ids and exactly
one `C376` coordinator - any other count does nothing and logs one debug line, because
a guessed target is worse than a stale row. The migration callback checks the target
unique id itself and returns `None` when it is taken: `async_update_entity` raises
`ValueError` on a collision rather than skipping, and a raise there fails the whole
config entry.

Affected owners are unchanged: @Xygen (#1, #4, #247) only. Unaffected: `AC31` (#27,
#245), `HF33`, and Standard Mode entirely.

### Addendum 2026-09-09 (later the same day): no migration, and the four keys stay on both lists

The addendum above was written before two things were known, and both of them
narrow condition 3 further - to almost nothing.

**The `AC31` reports the same four keys through the same PowerOcean, on a path
this change does not touch.** `(209, 8)` stays registered and
`remap_ev_charging_keys` keeps filling `ev_charge_power_w`,
`ev_session_energy_wh`, `ev_session_duration_s`, `ev_charge_status` and
`ev_vehicle_id`. Removing those five definitions from `POWEROCEAN_SENSORS`
alongside the `(241, 3)` relay would have left every PowerPulse 1 owner (#27,
#245) without a single wallbox entity, and nothing in the tree would have gone
red: the keys keep being filled, they would simply have had no definition to
render. So the four keys are defined on **both** lists, deliberately - one
physical quantity, two physical wallboxes, and a unique id carries the serial
of the device it belongs to. `ev_vehicle_id` is **not** withdrawn; it stays a
PowerOcean-side reading of the PowerPulse 1, which is the only wallbox that
reports a vehicle at all.

**The owner the migration was for does not want it.** Asked on #7 on
2026-09-08 which of the five he uses and whether their history matters, @Xygen
answered on 2026-09-09 that he uses none of them in dashboards, automations,
templates or scripts, that preserving their history is not important on his
installation, and that entities coming back under new ids would be completely
fine. He runs his own wallbox integration today and intends to switch once the
PowerPulse 2 is fully supported. He also supports the separate device
explicitly, and asks for the same for the PowerGlow.

**Condition 3 therefore reads, finally:** no registry migration. The four keys
stay defined on both lists. `ev_vehicle_id` stays on the PowerOcean. `(241, 3)`
leaves the PowerOcean table as the addendum above requires, and the registry
entries it used to fill - `<powerocean_sn>_<key>` for the four keys - are
**removed** rather than renamed, by `_async_remove_relayed_wallbox_entities`,
which does nothing at all when the entry also holds an `AC31`, because on such
an entry those ids may be that wallbox's live readings and a unique id does not
say which wallbox wrote it.

This drops `async_migrate_entries` from the plan entirely, and with it the
`ValueError` on a taken unique id that would have failed the whole config entry
from inside `async_setup_entry`. The first migration of that kind in this tree
is not written for a case whose only known owner declined it.

### Addendum 2026-09-10: condition 1 is met; the rest is design until it lands

**Condition 1 is met by ADR-024.** The sentence in the addendum of 2026-09-08, "Condition 1, the per-family command registry, is untouched and still binding", was written before PR #380 merged later that day. Since a7af66d the registry is one table per device type and the device type is passed in, which is the namespace condition 1 asks for. A wallbox parser can now own its own table without touching the PowerOcean's.

**The implementation landed later on 2026-09-10, in v1.21.0-beta.2.** Written earlier that day, this paragraph recorded that nothing of the decision was on `main` yet; that is no longer so. The tree now has the PowerPulse 2 device type, `powerpulse_proto.py` reading `2/33` and `2/34`, and the `C376` entry in the prefix map. `(241, 3)` is gone from the PowerOcean table, the four relayed `ev_*` keys stay defined on both sensor lists for the PowerPulse 1, `ev_vehicle_id` stays a PowerOcean reading of the PowerPulse 1 only, and the removal helper of the addendum of 2026-09-09 (later the same day) runs on every start for an entry that holds a `C376` and no `AC31`. beta.2 removed four rows and left the `ev_vehicle_id` row standing, although the relay had filled it with a placeholder (the owner's diagnostics of 2026-08-29 list the key); beta.3 removes all five. Two things came out of review before it shipped: the wallbox pushes only on change and is silent for up to 50 minutes in a complete seven-hour capture, so it carries its own stale, soft and hard thresholds (20, 40 and 60 minutes) the way the WAVE 3 in standby does; and the session-start meter reading carries no state class, so a snapshot of the lifetime counter is neither summed by statistics nor offered to the Energy Dashboard. Condition 2 is met by the capture the addendum of 2026-09-08 records. Nobody has seen the entities on a running installation yet; the pre-release waits for the two owners on record.

**Prefix scope, open.** On #7 on 2026-09-09 @AlexanderSeggerman reports a PowerPulse 2 under the serial prefix `C374`, listed as not supported and delivering no data. This ADR names `C376` only. Whether `C374` is the same device family, and therefore a second entry for the same type, is not decided here; it needs a question to the reporter and a frame before it needs a decision.

### Addendum 2026-09-11: `C374` is a second prefix of the same type

**Decided on the frame.** The recording asked for above arrived on #7 on 2026-09-11: 30 minutes of a `C374` with several short charging sessions, four of them visible in the kept frames: one drawing 16 A per phase from the grid under a maximum-current reading of 20 A, the others solar-controlled with setpoints of 9.5 to 12.4 A. Its `2/33` HeartBeats carry the same field numbers, the same nested charge readings and the same scaling as the two `C376` recordings: the lifetime identity `44 - 43 == 42` holds on every session frame, the reported power sits just under the sum of the three phase products on every charging frame (11.4 kW at 15.9 A, 8.7 kW at 12.1 A, 6.6 kW at 9.2 A), and the two deci-amp settings read 200 for his 20 A limit and a moving setpoint beside it. `2/34` and `241/44` decode the same way. So `C374` is a second `_SN_PREFIX_MAP` entry for `DEVICE_TYPE_POWERPULSE2` and nothing else: no second type, no model check, no name of its own (the app API names neither prefix, both fall through to the type's display name). Six of his HeartBeats are the fixture `tests/fixtures/powerpulse/c374_frames_20260911.json`.

**Two things the recording showed that the `C376` ones did not.** Plug status `2` appears twice, before the first session and between two sessions, with power 0 and the session status at idle; it is `preparing` in the enum the relay already used for the same key, and the parser now maps it, where before it dropped the value and the sensor kept `finishing`. And the first frame after what looks like a restart carries a session start timestamp of 0 under status `preparing`, which the parser now withholds rather than putting 1970-01-01 on the timestamp sensor. The start and stop gate of ADR-009 is unchanged: both `preparing` frames are followed by a session, but nothing in the recording shows who started it.

### Addendum 2026-09-11: the writes paragraph is changed by ADR-009

**The start and stop command is decided by ADR-009.** The paragraph above that kept writes out of scope described a command on the PowerOcean's topic with its confirmation on the wallbox's own as the mixing this integration refuses. ADR-009 reads that sentence in its context, the accessory era in which the confirmation would have come from the PowerOcean's relay while the values came from the wallbox's own topic, and accepts the shape for the start and stop command under a stated criterion: one transport, one account, one mode, the command where the device accepts it, the confirmation from the value the entity already shows. What this paragraph keeps is unchanged: a write on the wallbox's own topic is unevidenced, and a wallbox without a PowerOcean gets no control. The settings writes (current, mode, phase, target) are not decided by ADR-009 and stay out of scope.

---

## ADR-010: Device energy totals follow the device; a step needs one confirming reading

**Status:** Accepted (decided 2026-09-03; shipped in v1.19.0-beta.4 as 67daf3f, PR #345; amended 2026-09-03 after review, see the addendum at the end of this ADR)
**Date:** 2026-09-03
**Depends on:** ADR-006 is unaffected. The ceiling this ADR keeps came with an earlier change (the 1.15.0 release note names it); the open question of which keys adopt device totals inherits the contract below.

**Context:** Two measured defects on the device-total path, both unchanged on `main` at 2026-09-03:

1. `MAX_TOTAL_KWH` is 10,000,000 kWh (`ecoflow/energy_integrator.py:44`). A device counter is a uint32 in Wh, divided by 1000 in both PowerOcean parsers (`parsers/powerocean_proto.py:579-582`, `parsers/powerocean.py:410-445`), so the largest value the wire can carry is 4,294,967.295 kWh. The ceiling cannot fire on that path.
2. `_apply_data` stores the raw parsed value first (`coordinator/state_apply.py:177`), then `_integrate_energy` hands it to `set_total` and never writes the verdict back (`:459-462`). `set_total` returns `None` both when it rejects (`energy_integrator.py:193-199`) and when it ignores a lower value (`:203-204`). The HTTP path does the same (`coordinator/http_poll.py:156,162`). A rejected or ignored reading is on the sensor regardless.

A third mechanism blocks recovery once a bad value sits in device data: `_enforce_monotonic` (`coordinator/mqtt_ingest.py:120-132`) deletes any lower reading of a `total_increasing` key from `parsed` before the integrator sees it. Poison in `_device_data` therefore keeps the device's own correction from ever reaching `set_total`.

The path is live. In Standard Mode the quota carries `ems_change_report.bpTotalChgEnergy` (3,667,924 Wh on an HJ37, a Standard Mode capture on file), which `parsers/powerocean.py:440` maps onto `batt_charge_energy_kwh`, one of the two `POWEROCEAN_ENERGY_FROM_API` keys. In Enhanced Mode the protobuf counterpart never arrives (verified 2026-07-29), so there the battery totals are accumulated and the `set_total` branch is reached only through `seed_energy_total`.

Two facts shape the decision. Magnitude cannot separate a sustained wrong reading from a genuine one: a fixed ceiling low enough to catch 4,000,000 kWh is within reach of a large installation over decades, and a genuine counter rejected by a ceiling fails silently, which is the worse outcome. Persistence can separate a one-off wrong reading from a genuine one: a decode fault reports one wrong value and the device then returns to the real one; a genuine value is reported again. Home Assistant's recorder reads a decrease of 10 % or more as a meter reset and a smaller decrease as a spurious dip that it warns about (`homeassistant/components/sensor/recorder.py:480-487`). The load path already treats a reset as the honest outcome for a stored value that was never real (`energy_integrator.py:265-270`).

**Decision:**

1. **`set_total` follows the device.** Relative to the stored total, with a tolerance of `max(10 % of stored, 5 kWh)`, a reading falls into one of three bands:
   - a rise within tolerance is accepted at once (steady-state increments; the largest PowerOcean adds under 4 kWh across `MAX_GAP_S` at 30 kW, hence the floor);
   - a dip within tolerance is ignored, which is the glitch the guard was written for (`4408.259 -> 4408.258`, `mqtt_ingest.py:114-116`) and keeps every such dip out of the recorder's warning band;
   - a step beyond tolerance, in either direction, becomes a candidate held in memory. The next device reading within tolerance of the candidate confirms it and it becomes the stored total; a reading that agrees with the stored total drops the candidate; anything else replaces it. A confirmed decrease is published and logged once with the reset consequence spelled out.
   NaN, infinity, negative values and values above `MAX_TOTAL_KWH` stay rejected outright; the ceiling remains the bound for the Riemann and float-JSON paths. Candidates are never persisted. `integrate()` is untouched: candidates are a device-total concept. `seed_energy_total` keeps calling `set_total`, so a restored value beyond tolerance is a candidate that only a device reading can confirm, and a poisoned restored state can no longer re-poison a clean integrator on restart.

2. **The parsers drop an energy counter at the uint32 wire maximum** (4,294,967,295 Wh), the same way `parsers/powerocean_proto.py:579-581` already drops zero. `parsers/powerocean.py:410-445` gets both rules; today it passes zero and the maximum through unchanged. This is the placeholder class found on eleven percentage fields of the same message block (#88), applied to the energy fields by analogy (`LIKELY`: no capture in the repo carries the placeholder on an energy field, or on any field).

3. **`_integrate_energy` owns `_device_data[k]` for every integrator-managed key.** The set is the one `seed_energy_total` already computes (`state_apply.py:314-316`), factored into one property. After `_integrate_energy` runs, `_device_data[k]` equals `round(get_total(k), 2)` when the integrator holds a total and the key is absent otherwise, on both branches. `_enforce_monotonic` skips that set; the integrator is the sole monotonic authority for it, and non-integrator counters (`bp_cycles`, `bms_cycles`, capacities) keep the existing filter.

**Trade-offs:**
- (+) A counter poisoned below the ceiling recovers in two device readings, without touching `.storage`
- (+) No magnitude threshold exists that a genuine total could fail; the worst case for a genuine step is one reading of latency
- (+) One invariant covers display versus integrator in both directions: a rejected reading never reaches the sensor, and an accepted one always does
- (+) A one-off wrong reading in either direction is never published
- (-) A confirmed decrease is a meter reset in long-term statistics. A spike that was published before this change stays in the statistics and is corrected under Developer Tools, as the 1.15.0 release note already says
- (-) A sustained wrong reading is followed; nothing local can tell it from a genuine one. The only sustained class on record, the placeholder, is caught at the parser
- (-) `test_regression_dropped` (`tests/ha/test_coordinator.py:4155-4173`) asserts today that `_enforce_monotonic` drops `solar_energy_kwh`; that assertion flips deliberately
- (-) On the first HTTP poll of a fresh install, a device solar total in the quota no longer shows for one poll before the Riemann value replaces it, because the key stays absent until the integrator holds a total. That is the invariant applied to the Riemann keys; which source those keys should have stays an open question

**Alternatives considered:**
1. Lower `MAX_TOTAL_KWH` below the uint32 maximum - rejected: any value that catches 4,000,000 kWh is within reach of a large installation over decades, and a rejected genuine counter is silent.
2. A rate check, elapsed time times `MAX_POWER_W` - rejected: the load path resets the monotonic clock to now after a reboot (`energy_integrator.py:280-288`), so a genuine catch-up after an outage has no time base and would be rejected.
3. A factor check without confirmation, reject above ten times the previous value - rejected: a counter in its first days grows by more than that between an outage and its end, and a factor cannot tell a sustained value from a spike either.
4. Accept a lower device total at once - rejected: publishes every glitch dip and every one-off wrong-low reading as a reset.

**Consequences:** The device-total adoption question inherits this `set_total` contract and must not add arbitration of its own. It still decides which keys adopt device totals, whether `energy_stream.*TotalEnergy` is device-side or cloud-side, the discontinuity policy on first adoption, and the JSON `get_reply` probe. Not solved here: a Riemann-path total poisoned below the ceiling has no external reference and can only be cleared by the ceiling or by the user; long-term statistics after a reset; Delta 3's `bms_accu_*_energy_kwh`, which reach `_device_data` without any integrator (`DELTA3_ENERGY_FROM_API` is empty) and are outside this decision.

### Addendum 2026-09-03: the restore path, sustained steps, candidate lifetime, and one gap in the commit

The review of the implementation, at the commit it measured (findings F1 to F3), found one case the decision above does not cover and two it covers only by implication. This addendum settles all three and records one gap between the decision and the commit. Line numbers below are those of the commit the review measured.

**A0. Decision 3 is not implemented on the device-total branch.** `coordinator/state_apply.py:486-489` calls `set_total` and discards its return value, so the raw value that `_device_data.update(parsed)` stored at `:177` (and `http_poll.py:156`) stands: the exact defect item 2 of the Context describes. The two tests written for the write-back fail on that commit in the main checkout, `test_apply_data_restores_stored_total_over_a_rejected_reading` (4294967.295 != 2603.0) and `test_http_update_drops_energy_counter_placeholders` (3667.924 != 3667.92), and the full suite has exactly those two failures. The review's mutation table reports the write-back as present and its removal as caught by these same two tests, so the file that was committed is the mutated copy, not the restored one (`LIKELY` on the cause, `VERIFIED` on the state). The fix phase re-applies it: `total = set_total(...)`, then `_device_data[k] = round(total, 2)` for a float and `pop(k)` for `None`, as the two `integrate` branches already do at `:479-483` and `:493-498`. Nothing in A1 holds without it.

**A1. A restored sensor state is not a device reading and does not go through the bands (F1).**

Context: `seed_energy_total` (`state_apply.py:304-316`) hands the value Home Assistant restored for the sensor to `set_total`, and decision 1 treats that value as a device reading, so beyond tolerance it is a candidate. For the four Riemann keys in `POWEROCEAN_POWER_TO_ENERGY`, and for the two `POWEROCEAN_ENERGY_FROM_API` keys in Enhanced Mode, nothing else ever calls `set_total`; `integrate` does not consult candidates (`energy_integrator.py:106-201`). A restored value above the stored one by more than the tolerance therefore stays a candidate for good, and the next `integrate` publishes the lower stored total: a decrease of more than 10 %, which the recorder books as a meter reset and re-counts (`homeassistant/components/sensor/recorder.py:487` and `:786-795`, HA 2026.4.0). Measured in review: restored 200 over stored 100 leaves 100.0, and the next `integrate` publishes 100.0. On `main` the same call accepted 200 at once (`if total_kwh < current: return`). That is the case `seed_energy_total` exists for, a state file lost, rolled back or older than the recorder, and decision 1 named only the poisoned restored value, not the legitimate one. The startup ordering in Enhanced Mode is the same case at its sharpest (`LIKELY`, not measured): a push that reaches `integrate` before the entity is added seeds the metric at 0.0 (`energy_integrator.py:141`), the restore then arrives against a stored 0.0 with a 5 kWh floor, and any restored total above 5 kWh becomes a candidate that the next push publishes over as 0.00.

Decision: the integrator gets a second entry point, `restore_total(metric, total_kwh) -> None` in `ecoflow/energy_integrator.py`, and `seed_energy_total` calls it instead of `set_total`. Its contract:

- the plausibility rejection stays: NaN, infinity, negative and above `MAX_TOTAL_KWH` go through `_reject` and change nothing;
- a metric without state takes the value;
- a restored value above the stored total replaces it, keeping the stored timestamp and last power;
- a restored value at or below the stored total is ignored;
- it never records a candidate, and on accepting it drops any candidate for that metric, because the candidate was measured against the stored total it just replaced;
- it publishes nothing. The seed never wrote `_device_data`; the sensor keeps its restored value until the first `integrate` or `set_total` writes a total, which is now at or above it.

`set_total` becomes a device-only entry point and loses the sentence in its docstring about `seed_energy_total`; the comments at `state_apply.py:310-312` and `sensor.py:216-218` describe this contract instead of the monotonic guard they describe now.

Why an entry point rather than a flag on `set_total`: the difference between the two events is who is speaking, and that is the one thing the value cannot carry. A parameter would make one function hold two contracts, and every future caller would have to know which one it wants. Two names cannot be confused.

What protects against a poisoned value coming back through the recorder, now that the restore path bypasses the confirmation gate:

- For a key that receives device totals (the two `_ENERGY_FROM_API` keys in Standard Mode): the device. A poisoned restored value that `restore_total` accepts is a poisoned stored total, and that is the case decision 1 was written for. Two device readings within tolerance of each other confirm the correction, logged once, and the recorder books the same reset it would have booked one poll earlier under the candidate rule. Worst case: the poisoned value is displayed for two polls (60 s at `HTTP_FALLBACK_INTERVAL_S`) after a restart, during which it was the sensor's restored value anyway. The state file can carry the poison for at most one save interval in between (`_last_save_ts` starts at 0.0, so the first flush writes at once, `energy_integrator.py:68,309-311`); a crash inside that window restarts the same two-reading recovery.
- For a key the integrator alone produces (the four Riemann keys, and the battery keys in Enhanced Mode): the recorder holds nothing the integrator did not publish, so a restored value and a stored value are two snapshots of the same counter and the higher one is the later one. That invariant is decision 3, and at the commit the review measured it has two holes the fix phase closes before or with `restore_total`: A0 above, and F5 of the review. F5: `state_apply.py:476-478` keys the Riemann loop off the power key, while `powerocean.py:431-434` and `:447-451` produce the four Riemann keys directly from the quota, so an energy key arriving without its power key leaves the parser's raw value in `_device_data`, and `_enforce_monotonic` no longer filters it. The rule for that frame is decision 3 applied literally: when the frame carries the energy key, `_integrate_energy` rewrites `_device_data[k]` from `get_total(k)` or pops it, whether or not the power key came along. Which source those keys should ultimately have stays an open question. With A0 and F5 closed, a Riemann restored value above the stored one is by construction the integrator's own later output. Without them, the recorder can hold a raw device figure and `restore_total` would adopt it: a step up, never a reset, and the behaviour `main` has today, but not the invariant this ADR promises.
- For a restored value wrong by orders of magnitude (the #88 shape, 5.45e7 kWh): the ceiling in `restore_total`, unchanged.

Not solved, unchanged from the Consequences: a poisoned total below the ceiling on a key with no device reference, where recorder and state file both carry it, has no external reference and only the user can clear it. `restore_total` neither creates nor removes that case.

Alternatives considered: (a) keep the seed on `set_total` and let it publish nothing, the review's second option - rejected: the seed already publishes nothing, the defect is what the next `integrate` publishes, and that stays the stale stored total. (b) One rule per key class, candidate for device-total keys and accept-higher for integrator-only keys - rejected: the integrator does not know which keys receive device totals, and the coordinator knows the key list but not whether totals arrive in the current mode (the battery keys receive none in Enhanced Mode), so the classification would live in exactly the place that lacks the information. (c) Accept the restored value as provisional, so that a single device reading beyond tolerance replaces it without confirmation - rejected: saves one poll on the poisoned-restore case at the cost of a third state and a third contract; one poll of latency is cheaper.

One test flips deliberately: `test_a_poisoned_restored_state_cannot_repoison_a_clean_integrator` (`tests/test_energy_integrator.py:328-345`) asserts the behaviour F1 identifies as the defect. Under this addendum the restore is accepted and two device readings heal it, so the test becomes "a poisoned restored total is healed by two device readings", the guarantee decision 1 already gives a poisoned state file, and its docstring says why it changed. `test_seed_never_lowers_a_live_total` (`tests/ha/test_state_dedup.py:836-848`) stays green under both.

**A2. A reading that never agrees with its predecessor is not followed (F2).** Measured in review: stored 2.0, then 12, 22, 32, 42 leaves 2.0, because each reading replaces the candidate at `energy_integrator.py:298`, being beyond the band of the candidate as well as of the stored total. The trade-off "a sustained wrong reading is followed" above is too broad and is sharpened here: sustained means two consecutive readings within tolerance of each other. A reading that moves by more than the band on every arrival is not a counter, and the stored total holds.

This is the intended behaviour, and physically it cannot catch a genuine counter on a live path. Consecutive device readings arrive at the poll cadence (30 s, `HTTP_FALLBACK_INTERVAL_S`, `coordinator/core.py:155-157`); at the 30 kW rating that is 0.25 kWh per reading against a 5 kWh floor, a factor of twenty. After an outage the first reading is a catch-up beyond the band and the second, 30 s later, confirms it. That is the two-reading recovery decision 1 promises, and it holds because the second reading is close to the first, not because it is close to the stored total. The only way to stay stuck is a value that changes by more than 5 kWh, or 10 %, on every single reading, and for that shape holding the last good total is correct.

What changes: the stuck case becomes visible. A candidate that is replaced rather than confirmed logs once per metric at warning level, in the style of `_reject` (`energy_integrator.py:79-89`): the device total for this metric keeps moving beyond the tolerance band and the stored total is held. The latch resets when a reading for that metric agrees with the stored total or confirms a candidate, so a second episode logs again and a continuing one does not. No change to any total.

Rejected: confirming on direction alone, a second reading beyond the band on the same side confirms - it would follow two different wrong values in a row, which is the one-off class with one more sample, and gains nothing for a genuine counter that the proximity rule does not already give it.

**A3. A candidate has no expiry, deliberately (F3).** A candidate is dropped by the next reading that agrees with the stored total (`energy_integrator.py:266-269`) and replaced by the next reading that agrees with neither (`:298`). It outlives nothing but silence, and silence carries no evidence in either direction: a second reading that lands within the band of the first after days is two independent readings agreeing, exactly what decision 1 asks for, and the gap makes a coincidental agreement between two wrong values no more likely than it was at 30 s. An expiry would add one cost and no benefit: a genuine step whose confirming reading is delayed by an outage would need two readings after the outage instead of one. Candidates stay memory-only and are cleared by a reload, as decision 1 already says. The pin is `test_one_off_jump_never_moves_the_total` with its `<=` assertions replaced by `==` (F9 of the review).

**Tests the fix phase adds, red before the fix, with the mutation each one discriminates:**

`tests/test_energy_integrator.py`, new class `TestRestoreTotal`:

- restored 200 over stored 100: `get_total == 200.0`, then `integrate` 30 s later at 1000 W returns above 200. Red today (no `restore_total`; with `set_total` in its place, 100.0). Mutation "seed keeps calling `set_total`" fails; mutation "`restore_total` ignores a value when state exists" fails.
- restored 50 over stored 100: `get_total == 100.0` and `_candidates` holds nothing for the metric. Mutation "a lower restore records a candidate" fails the second assertion, and would let one device reading near 50 confirm a reset.
- restored 5.45e7 over stored 100: `get_total == 100.0`, one warning. Mutation "restore skips the plausibility check" fails.
- restored 42 on a metric without state: `get_total == 42.0`. Mutation "restore only replaces, never seeds" fails.
- stored 100, device 300 (candidate), restored 200: `get_total == 200.0`, no candidate; a following device 300.2 is a candidate again, not a confirmation. Mutation "accepting a restore leaves the candidate" fails the last assertion.
- `test_a_poisoned_restored_state_cannot_repoison_a_clean_integrator`, rewritten as above: restored 4,000,000 over stored 2603 is taken; device 2603.3 leaves 4,000,000; device 2603.5 gives 2603.5. Mutation "restore goes through the bands" fails the first assertion; mutation "never accept lower" fails the last.

`tests/ha/test_state_dedup.py::TestEnergyRestoreSeed`:

- integrator holds 100 for `solar_energy_kwh`, `seed_energy_total("solar_energy_kwh", 200.0)`, then `_apply_data({"solar_w": 1000})` with the stored timestamp backdated 30 s: `device_data["solar_energy_kwh"] >= 200.0`. Red today (100.0, the measured F1). The end-to-end pin through the coordinator; mutation "seed calls `set_total`" fails it.

`tests/test_energy_integrator.py::TestSetTotalBands`:

- 2.0, then 12, 22, 32, 42: `get_total == 2.0` after every call and exactly one warning. Mutation "same side confirms" fails after the second call (22.0); mutation "no warning on replacement" fails the count; mutation "warn on every replacement" fails the count too (three replacements, one warning).
- 2.0, then 12, 22, 22.3, then 900, 950: `get_total == 22.3` after the confirmation, and two warnings in total. Mutation "latch never resets" fails the count.

`tests/ha/test_coordinator.py` (A0): the two failing tests are the pin; no new test.

Files and functions for the fix phase, beyond those in decision 1 to 3: `ecoflow/energy_integrator.py` (`restore_total`; the replacement latch beside `_rejected`; `set_total`'s docstring), `coordinator/state_apply.py` (`seed_energy_total` calls `restore_total`; the write-back at `:486-489`; the Riemann branch for an energy key without its power key), `sensor.py:216-218` (comment).

---

## ADR-011: A control's source value that fails validation becomes explicitly unknown, and every value that reaches a control passes a parser

**Status:** Accepted (decided 2026-09-03; shipped in v1.19.0-beta.4 as 67daf3f, PR #345; decision 4 amended 2026-09-03)
**Date:** 2026-09-03

**Context:** `ems_app_surplus_pct` (`dev_soc`, field 10 of the 96/13 report) reaches the coordinator through `merged.update(raw)` and `return raw or None` (`coordinator/mqtt_ingest.py:644-645`, `:553-554`) without a parser and without the 0 to 100 rule given to eleven sibling fields (#88) (`parsers/powerocean.py:107-147`). Three consumers read it: the surplus slider (`const.py:692`), the backup slider's pair write (`number.py:580`) and the auto-sync (`state_apply.py:230-286`).

Evidence, from the repo's own captures decoded with `JTS1EmsParamChangeReport` and a positive control on the known 56-byte frame: 19 payload-carrying 96/13 frames from one HJ31 across seven listen-only captures, 18 with `dev_soc` (90 or 100) and one 33-byte frame without it, a peak-shaving-only report. A second installation's diagnostics show `app=13`. No capture and no fixture in the repo carries the uint32 placeholder on any field; the evidence for that rule was the reporter's screenshot on #88 (PowerOcean Plus, HTTP path, `ems_change_report` block). So: an absent field is proven and already handled (`state_apply.py:232-233` exits, and `:171-172` does not stamp the report time); a placeholder on this field is unobserved and inferred from the sibling class.

What a placeholder would do today, traced through the code rather than observed: `_maybe_schedule_surplus_sync` schedules a write with `solar=4294967295` every 30 s (`APP_SURPLUS_SYNC_MIN_INTERVAL_S`), which `build_powerocean_soc_set_payload` refuses with `ValueError` before any bytes leave (`ecoflow/energy_stream.py:91-93`); the coordinator retrieves the exception (`set_commands.py:206-213`), whether Home Assistant logs it as well is not verified. The backup slider computes `solar = max(4294967295, backup)` (`number.py:580-582`), the flush raises and rolls back (`test_a_raising_write_still_rolls_back`), so the backup slider cannot be moved. The surplus slider publishes 4294967295 as its state; Home Assistant validates the range on the set service only (`homeassistant/components/number/__init__.py:100-121`), never on read. The device is never written with a placeholder. The design's "repeated write commands" is in fact repeated failed attempts plus two broken sliders.

**Decision:**

1. The 96/13 report gets a parser function like every other frame: `remap_ems_param_change_keys(raw)` in `parsers/powerocean_proto.py`, called at both ingest sites. It returns every key unchanged, so the other thirteen names still reach device data and the diagnostics key list (`test_param_change_real_frame_passes_every_declared_field_through` is the guard for that), and it replaces `ems_app_surplus_pct` with `None` when the value is numeric and outside 0 to 100.
2. `None` is the explicit-unknown contract the number platform already has (`number.py:231-240`): the slider shows unknown, not the last value. A sensor's last valid reading is still a reading; a control's last valid value is what the user would write back, and the device has just said it holds no value.
3. `_maybe_schedule_surplus_sync` needs no change: `None` exits at `state_apply.py:232`. No second range check on the write path; `build_powerocean_soc_set_payload` stays the last line.
4. A pair write never guesses the other half. The backup slider reads the surplus mirror with no default (`number.py:587-589` on the branch): a missing key and an explicit `None` are the same fact, the surplus is unknown, and both refuse the write with the set-failed error.
   *Amended 2026-09-03.* The first version of this decision kept the missing-key default of 100 and listed it as a separate open decision. The implementation removed it in the same change, on instruction, and that is right: with the default, a backup write before the first 96/13 report wrote the app-side surplus to a value the owner never chose, which is the fault this ADR exists to stop, one branch over. The same rule covers the other half of the pair. The surplus slider's `int(data.get("ems_discharge_lower_limit_pct", 0))` at `number.py:600` guesses 0 for a missing key and then sends `min(0, solar)`, moving the discharge lower limit to zero in the direction that lets the battery discharge fully; it refuses on a missing key or `None` in the same way (review F6). At the commit the review measured the backup branch is done and the surplus branch is not; this ADR names the one still open so the record and the code disagree in one known place rather than silently.

**Trade-offs:**
- (+) One validation, in one place, on the ingest path; both write consumers are covered by the value they read
- (+) The pass-through of the other fields is untouched by construction, not by care
- (+) The slider is honest: unknown when the device reports no value
- (-) The surplus slider reads unknown until the next valid report; if the device sustained the placeholder, the backup slider would stay refused for as long, with an error that says why
- (-) Sensor keys (drop, keep last) and this control key (`None`, unknown) now treat the same fault differently. Deliberate, and this ADR is where that is written down
- (-) A pair write before the first report that carries the other half is refused, not guessed; the user retries once the report has arrived, which on a live device is seconds

**Alternatives considered:**
1. The same drop rule, delete the key - rejected: the slider would show a value the device no longer holds, and a backup write would send it back.
2. A guard on the write path only - rejected: leaves 4294967295 on the slider and in device data.
3. Narrow the pass-through to the surplus field - rejected for the reason in the comment at `mqtt_ingest.py:545-552`: the other fields' names must reach diagnostics.

**Consequences:** The missing-key default on the backup write is resolved by decision 4 as amended; its mirror on the surplus write (`number.py:600`) is decided there and lands in the fix phase. One pre-existing finding stays open. @Xygen's diagnostics on beta.24 (#247, a capture on file) carry ten `surplus_auto_sync app=13 ems=20` events at 30 s spacing: a valid value the EMS does not reconcile, the same shape as the 0 and 100 exemption at `state_apply.py:249-250`, and outside this decision. Decided as ADR-013.

---

## ADR-012: The HA test harness gets a per-test config directory, and the integrator's state file is the only thing that needed it

**Status:** Accepted; decisions 1 and 2 replaced the same day by the addendum below, after CI run 33858344140 measured the plugin hook absent at 0.13.316 (decided 2026-09-04; first shape implemented on the branch of PR #346, re-implemented per the addendum below)
**Date:** 2026-09-04

**Context:** `hass.config.path()` in this harness resolves to the plugin's own package directory: the `hass` fixture takes a `hass_config_dir` (`pytest_homeassistant_custom_component/plugins.py:622-626`, version 0.13.321 on the machine, floor `>=0.13.316` in `requirements_test.txt:4`), whose default returns `get_test_config_dir()` (`plugins.py:612-618`, `common.py:196-198`), and `async_test_home_assistant` builds `HomeAssistant(config_dir or get_test_config_dir())` (`common.py:226`). The plugin isolates Home Assistant's own storage in memory: `hass_storage` patches `Store` (`plugins.py:475-477`, `common.py:1512`). The energy integrator never goes through `Store`, because `ecoflow/` has no Home Assistant dependency; it writes a raw JSON file at `hass.config.path(".storage/ecoflow_energy_<sn>.json")` (`coordinator/core.py:388-389`, `ecoflow/energy_integrator.py:436-445`, which also creates the directory) and reads it on first use (`:95-104`, `:119`, `:394-434`). That is the only writer in the integration: a search for `config.path`, `config_dir`, `.storage` and `Store(` over `custom_components/ecoflow_energy/` finds `core.py:388` and nothing else, and the positive control is that it found that line. So the harness's storage isolation covers everything except the one file that bypasses it by design.

Measured on 2026-09-04 with a throwaway test file in `tests/ha/`, deleted after the run:

- The plugin directory held four leftover state files, `ecoflow_energy_{D3M1TEST00000001,HW51TEST00000001,HW52ZAB412340001,PLUG1234SN000001}.json`, two written 2026-09-03 22:41 and two 2026-08-18. A coordinator built for the PowerOcean mock with nothing but `load_state()` read `solar_energy_kwh = 200.004 kWh` off disk, the residue of `test_state_dedup.py`'s seed test from an earlier run. That is the leak reproduced from the other side.
- The supported hook exists and works. `hass_tmp_config_dir` (`plugins.py:591-608`) copies `testing_config/` into pytest's `tmp_path`, and its docstring says to override `hass_config_dir` with it. With that override `hass.config.config_dir == str(tmp_path)`, a flushed state file lands under `tmp_path/.storage/`, and the plugin directory's files keep their mtime.
- The copy is `copytree` of the whole directory (`plugins.py:601-606`), so a leftover `.storage/` from a run before this change comes along: with the bare override the leaked `HW52` file was present in the tmp copy. The plugin ships no `.storage/` of its own (`testing_config/` is `__init__.py` plus `custom_components/`, 13 files, 52 kB), so everything under `.storage/` there is ours.
- Integration discovery does not depend on the config directory. Home Assistant's loader imports the `custom_components` package from `sys.path` (`homeassistant/loader.py:298-310`), and `enable_custom_integrations` only clears a cache (`plugins.py:1458-1460`). `hass.config_entries.async_setup` for the Delta mock reached `ConfigEntryState.LOADED` with the tmp directory.

Seven tests hold `_loaded = True` by hand: `tests/ha/test_coordinator.py:1300, 2948, 2975, 3418, 3456`, `tests/ha/test_diagnostics.py:732`, `tests/ha/test_state_dedup.py:876`. Five of them state the leak as the reason. Two also seed `_state` by hand after the flag (`:2948`, `test_state_dedup.py:876`). One (`test_diagnostics.py:732`) gives a different reason, blocking I/O on the event loop. That reason does not hold in this harness: Home Assistant's loop protection lists `open` with `skip_for_tests=True` (`homeassistant/block_async_io.py:136-143`), and the leaked read itself, 2603.41 in the earlier measurement and 200.004 today, happened on the loop and raised nothing. With no file present `_load_state` does one `Path.exists()` and returns (`energy_integrator.py:396`); it replaces `_state` only in its except branch (`:432-434`), so a hand-seeded `_state` survives a `load_state()` against an empty directory.

**Decision:**

1. **`tests/ha/conftest.py` overrides `hass_config_dir` with `hass_tmp_config_dir` and removes `<tmp>/.storage` before returning the path.** This is the plugin's documented hook, not a redirect built around it. It is not autouse and does not need to be: `hass` depends on `hass_config_dir`, so every test that builds a `hass` is covered by the fixture graph, and a test that builds no `hass` has no path to write to.
2. **The `.storage` removal stays after the leftovers are gone.** The copy is the plugin's behaviour, a run of any branch older than this change refills the directory, and the success criterion "deleting `.../testing_config/.storage/` changes no test result" has to hold in both directions. Only our own files can be under that path (see Context), so the removal touches nothing of the plugin's.
3. **All seven `_loaded = True` lines go.** `test_coordinator.py:1300`, `:2975`, `:3418`, `:3456`: leak only, and `:3418`/`:3456` describe "the lost-state-file case", which an empty tmp directory is. `test_coordinator.py:2948` and `test_state_dedup.py:876`: leak plus a hand-seeded `_state`, which survives because `_load_state` reads nothing when there is no file. `test_diagnostics.py:732`: the event-loop reason does not apply in tests (Context), and the seeding it protects does one `exists()`. Their removal is the acceptance test of the fixture: the suite runs green with the plugin's `.storage/` still populated from a pre-fix run. Afterwards the four leftover files are deleted by hand on the maintainer's machine, once; CI installs the plugin fresh and has none.
4. **The production path is untouched.** The integrator keeps writing a raw file under `.storage/`, which is what an HA-free core library has to do.

**Tests**, new file `tests/ha/test_harness_isolation.py`, red before the fix, each with the mutation it discriminates:

- `test_state_file_path_is_under_the_test_tmp_dir`: `hass.config.path(".storage/x.json")` starts with `str(tmp_path)`. Red today (it starts with the plugin's package path). Mutation: remove the override, red.
- An ordered pair in one class. `test_a_flushed_total_lands_in_this_test_dir`: coordinator for `MOCK_POWEROCEAN_DEVICE`, two `integrate("solar_energy_kwh", 1000.0)` calls 36 s apart under a patched `energy_integrator.time.monotonic`, `flush()`, then `Path(hass.config.path(".storage/ecoflow_energy_HW52ZAB412340001.json")).exists()`. `test_a_fresh_coordinator_reads_nothing`: a new coordinator for the same serial, `load_state()`, `state_snapshot() == {}`. Red today when run after its partner, and on a machine with a leftover file even when run alone, which is the property this decision is about. Mutation: remove the override, the second test is red.
- `test_leftovers_in_the_plugin_dir_are_not_copied_in`: a fixture requested before `hass` monkeypatches `pytest_homeassistant_custom_component.plugins.get_test_config_dir` to a tmp "plugin directory" seeded with `.storage/ecoflow_energy_HW52ZAB412340001.json` holding a total; a fresh coordinator reads `{}`. Green after the fix. Mutation: drop the `.storage` removal, red. This is the pin for decision 2; without it the removal is a line nobody tests. The monkeypatch is chosen over writing into `site-packages` during a test, which is the fault being fixed.
- The seven sites get no new test; their removal is the test, and if `_load_state` ever starts clearing `_state` on an empty directory they go red as a group, which is the right alarm.

**Trade-offs:**
- (+) One fixture, on the plugin's own hook, and no test has to know a convention
- (+) The read path runs in every test again, so a regression in `_load_state` is caught by the suite instead of hidden by seven flags
- (+) A `copytree` of 13 files per test; measured 8 tests in 0.28 s including a full entry setup, so no visible cost at 3081 tests
- (-) One `rmtree` per test whose reason is a plugin behaviour, which the comment on the fixture has to carry
- (-) The seven sites lose a defence they did not know they had: a test that seeds `_state` by hand now relies on `_load_state` leaving it alone when no file exists (`:396`), which is true and untested as a contract; the group failure above is the detector

**Alternatives considered:**
1. Make `_loaded = True` a fixture and keep the shared directory - rejected: it stops the read and leaves the write, so the directory keeps filling and the first test that forgets the fixture reads it.
2. An autouse fixture patching `EnergyIntegrator.__init__` or `hass.config.path` - rejected: it works around the plugin instead of using the hook the plugin documents, and it would have to know which paths matter; `hass_config_dir` covers every path.
3. Set `hass.config.config_dir` after the fixture built `hass` - rejected: the harness already used the directory during setup, and the object is not meant to be re-pointed.
4. Persist through `Store` so `hass_storage` covers it - rejected: `Store` is a Home Assistant class and `ecoflow/` stays HA-free (the architecture boundary of this repository: `ecoflow/` has no Home Assistant imports).

**Consequences:** ADR-013's regression tests are written against a harness that reads nothing from earlier runs. `tests/test_energy_integrator.py` already builds every integrator on `tmp_path` and is unaffected. The four leftover files in the plugin directory on the maintainer's machine are deleted in the fix phase, by hand, once. Not solved here: nothing about production persistence, and nothing about `tests/` outside `tests/ha/`, which never build a `hass`.

### Addendum 2026-09-04: the hook was not in CI at the time, so the fixture re-points the directory itself

**What was measured.** The one line marked ASSUMED at decision time ("whether `hass_tmp_config_dir` exists at the `>=0.13.316` floor is not checked; it exists at 0.13.321, CI installs the latest, and a green CI run is the proof") is now measured and false, and "CI installs the latest" was the wrong premise. CI run 33858344140 on PR #346 failed eight tests, four of them in `test_harness_isolation.py`, with `AttributeError: 'module' object at pytest_homeassistant_custom_component.plugins has no attribute 'get_test_config_dir'`. The 0.13.316 wheel, unpacked and read: `plugins.py` has no `hass_config_dir`, no `hass_tmp_config_dir` and no `get_test_config_dir`, and its `hass` fixture (`plugins.py:564-570`) takes `hass_fixture_setup, load_registries, hass_storage, request, mock_recorder_before_hass` and nothing else; `common.py:196` still has `get_test_config_dir` and `async_test_home_assistant(config_dir=...)` at `:219-226`. The hook this ADR built on was added at the fixture level between 0.13.316 and 0.13.321.

The gap is structural, not a stale pin. CI runs Python 3.13 (`.github/workflows/tests.yml:25`) and installs `requirements_test.txt`, whose floor `>=0.13.316` carries the comment that 0.13.317 and later require Python 3.14 (`requirements_test.txt:2-4`, mirrored in `.github/dependabot.yml:19-22`). PyPI confirms it: 0.13.316 requires Python `>=3.13` and pins `homeassistant==2026.2.3`; 0.13.317 requires `>=3.14` and pins `homeassistant==2026.3.1`; every Home Assistant release from 2026.3.0 on requires `>=3.14.2`. On 3.13 the floor is a de-facto pin, because 0.13.316 is the only version pip may choose. The maintainer's machine runs 3.14.2 with 0.13.321 and Home Assistant 2026.4.0, which is where the hook was found and where everything was green.

**Reproduced locally, so the check no longer depends on a CI round trip.** A venv built from the CI recipe (`uv venv --python 3.13`, then `tests.yml:31-32` verbatim) resolves to Python 3.13.12, `pytest-homeassistant-custom-component 0.13.316`, `homeassistant 2026.2.3`, `pytest-asyncio 1.3.0`. The python.org 3.13 on the machine is 3.13.1 and does not qualify (Home Assistant 2026.2.3 requires `>=3.13.2`), which is why `uv`'s managed interpreter is the one to use; CI's `setup-python` "3.13" resolves to the latest patch the same way. At the commit the review measured, this environment reports exactly the eight failures of the CI run, by name. That is the positive control for everything below.

**Options weighed.**

1. *Raise CI to Python 3.14.* Home Assistant 2026.2.3 would install there (`requires_python >=3.13.2`, classifiers 3.13 and 3.14); hassfest and HACS validation run in their own actions (`validate.yml:23`, `:33`) and do not depend on the Python of `tests.yml`; the only other reason on record for 3.13 is historical (3.12 was dropped over a paho thread-leak timing, on record). But on 3.14 the floor `>=0.13.316` resolves to the newest plugin (0.13.363 at the time of the measurement, per pip's own listing) and with it Home Assistant 2026.9.x, floating with every run. So this option needs an exact pin and a decision which Home Assistant line CI covers: today 2026.2.3, the last 3.13 line, against a HACS minimum of 2025.1.0 (`hacs.json`) and a reporter on 2026.8.3. That is a CI platform decision with its own consequences, and it does not belong on a test-isolation branch. Not chosen; recorded as an open question for the maintainer, not decided here.
2. *Re-point the directory without the hook.* `Config.config_dir` is a plain attribute (`homeassistant/core_config.py:587` at 2026.4.0) and `Config.path` joins it at call time (`:626-631`), so an assignment after the plugin has built `hass` moves every later `hass.config.path()`, including the coordinator's at `core.py:388`. Version-independent by construction, and simpler than the hook: no `copytree`, so no leftovers and no `.storage` removal.
3. *Take the isolation change off the branch.* The seven removed `_loaded = True` flags are what make four `test_coordinator.py` tests read another test's state in CI (`test_apply_data_restores_stored_total_over_a_rejected_reading`, `test_device_total_keeps_integration_off_the_seed_path`, `test_first_reading_after_state_loss_publishes_nothing`, `test_http_update_drops_energy_counter_placeholders`, all four among the hunks that removed a flag); the leaked values in the CI output, 2603.4 and 150.0, come from other tests in the same run. Splitting would mean putting the flags back, which is the convention this ADR removes, for a fix that is three lines.
4. *Use the hook where present and fall back otherwise.* Rejected outright: the environment that gates the repository would be the one without the isolation, and a green CI run would then prove nothing.

**Decision, replacing decisions 1 and 2 above.**

1. **`tests/ha/conftest.py` overrides the plugin's `hass` fixture by name, requests the original, sets `hass.config.config_dir = str(tmp_path)` and yields it.** Overriding a fixture and requesting the one it shadows is pytest's documented pattern; the plugin's own fixtures that take `hass` (`enable_custom_integrations`, the client fixtures) resolve to the override too, because pytest resolves fixture names from the requesting test. Nothing is conditional and nothing references a plugin attribute. The `hass_config_dir` override of the first shape goes, so that the tree has one mechanism, not one per plugin version.
2. **What the re-point does not reach, and why that is acceptable.** Anything that resolved `config_dir` while the plugin built `hass`: `hass.config.media_dirs` (`common.py:284`, both versions), unused here. The plugin's teardown reads no `config_dir` (`plugins.py:654-760` at 0.13.321, checked). A test that constructs its own `HomeAssistant(...)` would bypass it; none exists (`grep "HomeAssistant("` over `tests/` finds type hints only). Integration discovery is unaffected either way (`homeassistant/loader.py:298-310` imports `custom_components` from `sys.path`). If a future Home Assistant turned `config_dir` into a read-only property, the path test below fails loudly in both environments, which is the right alarm.
3. **The `.storage` removal of decision 2 is retired with the hook.** `tmp_path` is fresh by pytest's contract and nothing copies into it. The four leftover files in the plugin directory still get deleted by hand, once, as a courtesy to anyone reading that directory; no test depends on it any more.

**Measured with the override in place, on the branch at the commit the review measured, with the conftest swapped for the measurement and restored afterwards (md5 checked):** 3097 passed on 3.13.12 / 0.13.316 / 2026.2.3, and 3097 passed on 3.14.2 / 0.13.321 / 2026.4.0. Mutation, the assignment line replaced by `pass`, on 0.13.316: 7 failed, the three isolation tests below and the same four `test_coordinator.py` tests. The full setup path reaches `ConfigEntryState.LOADED` under the re-pointed directory in both environments.

**Tests, amended.** `tests/ha/test_harness_isolation.py` keeps `test_state_file_path_is_under_the_test_tmp_dir` (with one added assertion: `hass.config.path(".storage")` does not exist at test start, the pin that nothing is copied in) and the ordered pair `test_a_flushed_total_lands_in_this_test_dir` / `test_a_fresh_coordinator_reads_nothing`. It loses `test_leftovers_in_the_plugin_dir_are_not_copied_in`: it pins a `copytree` that no longer happens and reaches for `plugins.get_test_config_dir`, which 0.13.316 does not have. Mutations, both measured above: remove the assignment, the three go red on both plugin versions; a fixture that only works on one version cannot exist, because it references nothing version-specific.

**How the 0.13.316 check runs from now on, without guessing.** The local test runner gains a mode that builds a Python 3.13 environment from the CI recipe, consumed like its other flags. It builds once, and reuses, a venv at `${XDG_CACHE_HOME:-$HOME/.cache}/ecoflow-energy-ha/venv-ci-py313` (outside every checkout, so worktrees share it and `.gitignore` stays untouched) with `uv venv --python 3.13` and the two install lines of `tests.yml:31-32` verbatim. Before running anything it prints the interpreter version and the resolved `pytest-homeassistant-custom-component` and `homeassistant` versions, and refuses unless the plugin is below 0.13.317: a run that does not prove it is on the 3.13 line is not a CI check. Then it runs pytest through that interpreter with the same arguments as the default path. Its own controls, to be recorded with it: at the commit the review measured it must report the eight CI failures; with the fixture in place it must be green; with the assignment line removed it must report the seven above. That mode is the answer to "does this hold on 0.13.316" for every future change to `tests/ha/conftest.py`, and CI remains the gate that runs it for real.

**Trade-offs of the amendment:**
- (+) One fixture that reads identically on every plugin version this repository can meet, verified on the two it does meet
- (+) No copy per test, no leftover to remove
- (-) The isolation now rests on `Config.config_dir` being assignable rather than on a plugin contract; the path test is the detector
- (-) The CI-line question (Python 3.14, exact plugin pin, which Home Assistant CI covers) is now written down and unanswered

### Addendum 2026-09-10: CI moved to Python 3.14 with an exact pin, so the hook is in CI now and the decision stands anyway

**The open question of option 1 is answered.** PR #374 (4ea6080, merged 2026-09-08) moved every CI job to Python 3.14 and replaced the floor in `requirements_test.txt` with an exact pin, `pytest-homeassistant-custom-component==0.13.364`, which carries Home Assistant 2026.9.1. The pin's own comment says why: a floor let the resolver pick, and the type gate had been reading annotations seven months behind the release people actually have. The Home Assistant line CI covers is therefore 2026.9.1, and raising the pin is its own commit with whatever the newer Home Assistant reports fixed in it.

**Consequently the plugin's `hass_config_dir` and `hass_tmp_config_dir` hook is present in CI**, since 0.13.364 is well past the release that added it. That does not reopen the decision. The re-point was chosen because it references nothing version-specific: `tests/ha/conftest.py` still overrides `hass` by name and sets `hass.config.config_dir = str(tmp_path)`, and it works identically on a plugin with the hook and on one without. Switching to the hook now would tie the isolation to one plugin version again, which is what the amendment refused. The three tests of `test_harness_isolation.py` are unchanged.

**The local 3.13 check described above is retired in effect.** That mode still builds a Python 3.13 environment and still refuses unless the plugin is below 0.13.317, but it installs the same pinned file, which now names a release that needs Python 3.14. It can no longer produce a green run. Either it gets its own pin at 0.13.316 as a deliberate "does this still hold on the oldest line" check, or it goes; that is an open item for the maintainer, and CI is the gate either way.

**Two details of the addendum above are dated by this.** "CI runs Python 3.13" and "on 3.13 the floor is a de-facto pin" were true on 2026-09-04 and are not true now; the trade-off "the CI-line question is now written down and unanswered" is closed by PR #374.

---

## ADR-013: The surplus auto-sync writes a divergent pair at most twice, and the boundary list and the backup guess go with it

**Status:** Accepted and implemented (decided and shipped 2026-09-04 in PR #346; builds on 67daf3f, where ADR-010 and ADR-011 are merged)
**Date:** 2026-09-04
**Depends on:** ADR-011 (decision 4, the pair-write rule this ADR applies to its third consumer); ADR-012 (the harness the tests stand on)

**Context:** `_maybe_schedule_surplus_sync` (`coordinator/state_apply.py:215-290`, called from `_apply_data` at `:213`) reads `ems_app_surplus_pct` and `ems_backup_ratio_pct` (`:230-231`), returns when either is missing or non-numeric (`:232-238`) or when they agree (`:239-240`), returns for an app value of 0 or 100 (`:242-250`), then applies three time guards: the report must be newer than the last user SET (`:261-262`), 30 s since the last auto-sync (`:263-264`, `APP_SURPLUS_SYNC_MIN_INTERVAL_S`, `const.py:253`), 5 s since the last user SET (`:265-266`, `const.py:254`). It then writes both fields with `backup = min(ems_discharge_lower_limit_pct, app)` (`:268-279`), stamps `_last_app_surplus_sync_ts` (`:282`), logs at INFO and records a `surplus_auto_sync` event (`:283-290`). The three timestamps live on the coordinator (`core.py:223-224`, `:230`); `mark_user_surplus_set` (`state_apply.py:292-298`) is called by both slider branches (`number.py:592`, `:611`). Nothing in that path counts. The event log is a 50-entry FIFO (`core.py:621-627`).

Evidence. @Xygen's diagnostics download on beta.24 (#247, a capture on file) holds ten `surplus_auto_sync` events with the detail `app=13 ems=20` (`:407` to `:677`), 30 s apart from 1787895351.34 to 1787895636.99, 285 s. Each is followed within 20 ms by `set_powerocean_soc backup=0 solar=13` and within 130 to 230 ms by a `set_reply`: the device acknowledged every write. The FIFO is full (15 `app_write`, 5 `mqtt_data`, 3 x 10), so 285 s is a lower bound on the episode. Diagnostics never carry values, only key names (`diagnostics.py:450` builds `data_keys` from the keys), so the EMS value is known solely from the event detail. Three more downloads exist from the same installation: 2026-08-27 (421 s, 43 `app_write` and 7 `mqtt_data`, no sync) and 2026-08-29 on beta.29 (796 s, 14 `mqtt_data`, 1 `mqtt_connect`, 2 `set_reply`, no sync). Both list all three keys in `data_keys`.

Can the bound harm, i.e. does a device ever adopt the value after more than two writes? Searched: the pattern `"type": "surplus_auto_sync"` over every capture on file matches the beta.24 download only, and the positive control is that it finds exactly ten there; within the file every detail is the same pair, so no capture shows the EMS moving after a repeated write. The 2026-08-29 download proves the loop was not running a day later and cannot say why: the EMS may have adopted 13, the owner may have moved the slider, or the 96/13 report may have stopped carrying `dev_soc` (ADR-011's parser was not in beta.29, so a placeholder would also have been silent there). A diagnostics file cannot separate those, and no capture can, because the value is not in it. So: no evidence for late adoption, none against it beyond 285 s, and the design below does not need the answer. The bound stops writes, not reading. A device that adopts on its own later converges with zero writes, which is the desired end; a device that needs a third identical write is the only harm case, and for it the reset rules give two writes per restart and two per user action, against 2880 per day today. A user write from Home Assistant is never subject to the bound: `number.py` calls `async_set_powerocean_soc_debounced` directly (`:593`, `:612`).

The `(0, 100)` guard (`:242-250`) states the general condition in its own comment, "reissuing a SET would never reconcile the two", and encodes a value list; 100 was measured (the EMS clamps `sys_bat_backup_ratio` to 90), 0 is marked "likely". `test_no_sync_when_app_value_is_100` and `test_no_sync_when_app_value_is_0` (`tests/ha/test_powerocean_number.py:748-784`) pin the list.

The backup half. `:268-273` defaults a missing or non-numeric `ems_discharge_lower_limit_pct` to 0 and then writes `min(0, app) = 0`, moving the discharge lower limit to the value that lets the battery discharge fully. ADR-011 decision 4 as amended forbids that guess on both slider branches (`number.py:587-589`, `:604-606`) and names the auto-sync as the third consumer of the pair in its context; its fix phase did not reach this line. `test_non_numeric_backup_limit_falls_back_to_zero` (`test_powerocean_number.py:715-728`) pins the guess. Whether @Xygen's `backup=0` came from a real 0 or from the default cannot be told from his diagnostics (the key is in `data_keys` at `:260`, the value is not).

**Decision:**

1. **Two writes per divergent pair per process lifetime.** The coordinator keeps one record, `(app, ems, writes, stopped)`, created or advanced at the point where `_last_app_surplus_sync_ts` is stamped (`:282`), so a write that `_schedule_powerocean_soc_write` refused (`:280-281`) does not count. A third evaluation of the same `(app, ems)` returns without a write. Why two: the first write can be judged only against a report that left the device after it, and the 30 s throttle makes the next evaluation land on such a report in the common case but not in every case (the 96/13 echo is event-driven and the EmsChangeReport cadence is the device's, `:253-260`), so a second write is the cheapest way to rule out a lost or raced first one. Why not three: after two acknowledged writes and two reports that still hold the old value, the device has answered, and every further write is the loop this decision describes. Why not one: it would give up on a race, and the cost of the second write is one SET per episode.
2. **What clears the record.** (a) `mark_user_surplus_set()` clears it: a user who corrects the sliders from Home Assistant starts fresh. (b) An app value different from the recorded one clears it, checked before the equality return at `:239`, so a move in the EcoFlow app to a value the EMS already holds clears it too; the next divergence is a new intent. (c) A divergent pair with a different EMS value starts a new record: the device moved, and that is new information. (d) A report where both agree and the app value is the recorded one changes nothing, so an EMS that echoes the written value once and reverts does not re-arm the loop. (e) A reload clears it, because the record is in memory.
3. **The record is not persisted.** Cost: two writes per pair per restart, against one every 30 s today. Persisting it would need a store or a field in the integrator's state file, an expiry rule nobody can derive (a firmware update may change the device's answer), and a coupling between the write path and persistence for a saving of two SETs per restart. Revisit only on a diagnostics download that shows restart-driven traffic mattering.
4. **The `(0, 100)` list goes; the general rule covers the boundaries.** At `app=100, ems=90` the device gets two writes per process lifetime and then silence; before this change it got none. That is two writes per restart the code knows will be clamped, paid for one rule instead of a rule plus a hand-maintained list whose second entry was never measured. The comment's knowledge, that the EMS clamps to 90 at 100, moves into the docstring of `_maybe_schedule_surplus_sync` as the first measured instance of the general case.
5. **The backup guess goes (ADR-011 decision 4 applied).** A missing, `None` or non-numeric `ems_discharge_lower_limit_pct` returns without a write, silently, like the guards at `:232-238`. The auto-sync is a pair write and the rule already covers pair writes.
6. **Visibility.** When the third evaluation of a pair is suppressed for the first time, one INFO line: `PowerOcean surplus auto-sync (HW52): EMS still reports 20 after 2 writes of 13; no further writes until the app value, the EMS value or a user setting changes`, and one event `surplus_auto_sync_stopped` with detail `app=13 ems=20 writes=2`; the record's `stopped` flag makes both once-only. INFO rather than WARNING because at the 100/90 boundary this is documented device behaviour and would otherwise warn on every restart. Diagnostics get a `surplus_auto_sync` key in `_device_diagnostics` (`diagnostics.py:431`, next to `energy_integrator` at `:526`), `{"app": 13, "ems": 20, "writes": 2, "stopped": true}` or `null` while no record exists, read through a coordinator property in the style of `schedule_divergent_bundles` (`core.py:437-440`). Always present, `null` on every device that is not a PowerOcean in Enhanced Mode, following the convention stated at `diagnostics.py:520-525`.

**Tests**, in `tests/ha/test_powerocean_number.py::TestPowerOceanAppSurplusAutoSync` on its `_make_coordinator` (`:525-541`, which seeds `ems_discharge_lower_limit_pct = 0`, `ems_backup_ratio_pct = 90`, `ems_app_surplus_pct = 47` and mocks `async_set_powerocean_soc`), red before the fix, each with the mutation it discriminates. Evaluations are driven by calling `_maybe_schedule_surplus_sync()` under a patched `coordinator.time.monotonic` advanced by 31 s per call, past the throttle.

- `test_a_pair_the_ems_never_adopts_gets_two_writes_then_silence`: app 13, ems 20, six evaluations, `async_set_powerocean_soc` called exactly twice, both with `(0, 13)`. Red today (six calls). Mutations: no bound, six; bound of one, one; bound of three, three. A global counter not keyed on the pair passes this test and fails the third one below.
- `test_the_stop_is_logged_once_and_reported`: after the six evaluations, exactly one `surplus_auto_sync_stopped` event with detail `app=13 ems=20 writes=2`, exactly one matching INFO record in caplog, and `_device_diagnostics(coordinator)["surplus_auto_sync"] == {"app": 13, "ems": 20, "writes": 2, "stopped": True}`. Mutations: log on every suppressed evaluation, four records; `stopped` never set, diagnostics show `False`.
- `test_a_different_ems_value_reopens_the_sync`: after the bound at (13, 20), `ems_backup_ratio_pct = 21`, next evaluation writes (three calls in total). Mutation: record keyed on the app value alone, stays suppressed.
- `test_a_changed_app_value_reopens_the_sync`: after the bound at (13, 20), `ems_app_surplus_pct = 20` and one evaluation (no write, record cleared, diagnostics `null`), then `ems_app_surplus_pct = 13` and one evaluation, which writes. Mutation: clear only on divergent pairs, i.e. the check placed after the equality return, stays suppressed.
- `test_an_equal_reading_does_not_reopen_the_sync`: after the bound at (13, 20), `ems_backup_ratio_pct = 13` and one evaluation, then `= 20` and one evaluation: still two calls. Mutation: clear the record on any equal reading, three calls. This is the pin against an EMS that echoes and reverts.
- `test_a_user_set_reopens_the_sync`: after the bound, `mark_user_surplus_set()` at t, `_last_ems_param_change_ts = t + 1`, evaluation at t + 6 (past the 5 s grace), which writes. Mutation: user set leaves the record, stays suppressed.
- `test_no_sync_when_app_value_is_100` and `test_no_sync_when_app_value_is_0`, rewritten as `test_a_boundary_value_gets_two_writes_then_silence`, parametrised over (100, 90) and (0, 10): two writes then none over six evaluations. Red today (zero writes). Mutation: keep the value list, zero writes.
- `test_non_numeric_backup_limit_falls_back_to_zero`, rewritten as `test_an_unknown_backup_limit_refuses_the_sync`, parametrised over `"junk"`, a missing key and an explicit `None`: no write, no `surplus_auto_sync` event, no record. Red today (writes `(0, 47)`). Mutation: restore the default 0, red.
- `test_a_refused_schedule_does_not_count`: `_schedule_powerocean_soc_write` patched to return `None`, one evaluation, no record and diagnostics `null`. Mutation: advance the record before the `None` check, record shows one write.
- The reload rule needs no test: the record lives on the coordinator, and every test in the class starts with a coordinator that has none.

**Trade-offs:**
- (+) The loop provably ends, in two writes, without knowing the device's rule
- (+) One mechanism where there were two (the value list and nothing), and no guessed half in the pair
- (+) A user is never locked out: a slider write from Home Assistant bypasses the bound and also re-arms it
- (+) The give-up is visible in the log once, in the event log once, and in diagnostics for as long as it holds
- (-) Two writes per restart on an installation parked at a boundary the device clamps, where today there are none
- (-) A device that needs more than two identical writes would be served only by restarts and user actions; no capture shows such a device, and none could
- (-) The set_reply result code is not read for the bound: the capture shows replies, and this repository has already seen other clients' `set_reply` frames on our topic, so a reply is not proof of our write's outcome. The bound is on evaluations of the device's reports, which is the read-back the architecture asks for
- (-) Two tests flip on purpose (the boundary pair) and one changes meaning (the backup guess); each carries its new reason in its docstring

**Alternatives considered:**
1. Learn the device's clamping rule and extend the value list - rejected: needs a capture across the range on hardware we do not have, and a rule for 13 versus 20 would be a third entry in a list this ADR removes.
2. Reset the record on every convergence - rejected: an EMS that echoes the written value once and then reverts would re-arm the loop at half its current rate; decision 2(d) covers it at no cost.
3. Persist the record - rejected in decision 3.
4. Keep the `(0, 100)` list beside the bound - rejected: two mechanisms for one fact, and the tell this repository names for contributions: one distinction, one registration.
5. Bound of one - rejected in decision 1.
6. Count only writes whose task returned `True` - rejected: the completion callback (`set_commands.py:206-213`) only retrieves exceptions today, the failure case without a report is narrow (the reports arrive on the same link the write uses), and counting at the stamp keeps the logic in one function.

**Consequences:** The maintainer's HJ31 reports `dev_soc` of 90 or 100 (ADR-011 context). If its app value sits at a boundary while the EMS holds 90, the Docker gate for this change should show exactly two `surplus auto-sync` INFO lines and one `stopped` line after a restart, and none at all if the two agree; that is the hardware verification the decision needs and it costs nothing extra. Not solved here: why the EMS holds 20 against 13, which stays an open question; the result code of `set_reply`; persistence across restarts. ADR-011's open item on the auto-sync's backup guess is closed by decision 5.

---

## ADR-014: The Solar Tracker is one device type under two prefixes, its entity list is what frames and the vendor schema both carry, and a control-to-be keeps its key from the read path on

**Status:** Accepted (decided 2026-09-04; shipped in v1.19.0)
**Date:** 2026-09-04
**Depends on:** the message definitions on file for this family; ADR-011 decision 2 (an explicit `None` is the unknown contract between parser and entity)

**Context:** #339 reports two active EcoFlow Solar Trackers on one account under two serial prefixes, `S02F` and `HZ31`, both skipped as "no parser available" (the reporter's diagnostics snapshot: four skipped records, all with an empty `product_name`). The reporter attached a 264-frame dataset with test markers and two diagnostics downloads whose listen-only capture holds raw frames from both prefixes. Two public replies (comments 5532023929, 5538238304) committed to a read-only parser for both prefixes with four entities, +10 on the angles, `0xFFFFFFFF` as unknown, an unitless light level, `1.1.24` out, no control before the read path, and a target angle that keeps one name and one identity when it later becomes the control. The reporter's third round (comment 5539173984) adds that `1.1.3` reads back Manual/Auto, that both write commands are decoded and replay-tested on both units, and asks that the read path merge first.

What this decision rests on, by source:

- **Message definitions.** The vendor's definition for this family (`Fd100Sys`) describes `EfFd100ReportPack` with 27 `oneof`-wrapped fields: `mode = 3`, `lux = 4`, `angle = 9`, `angle_manual = 10`, `angle_target = 11`, `battery_percent = 14`, `battery_temperature = 15`, `track_num = 24`, and the set messages `EfFd100SetWorkModePack.work_mode_set` and `EfFd100SetManualAnglePack.angle_set`. The public web app bundle's device registry keys `HZ31` (generalKey `product_st_fd100`, "Single Axis Solar Tracker") and `S02F` (`product_st_sp002`, "Single Axis Solar Tracker 2") to productType 31: same stem, same type, and the stem names the definition.
- **Frames.** Every frame in the dataset and in both diagnostics decodes with `decode_header_message` (`ecoflow/proto/decoder.py:97`) as `cmd_func=32, cmd_id=1`, `product_id=7937`, no `enc_type`, 27 fields present in every frame (n=175 per field on T1, n=118 on T2). One frame per prefix decoded straight from the diagnostics `skipped_devices[].raw_capture` gives the same message, the same product id and the same constants (`f13=1000`, `f18=600000`, `f19=15000`) for `S02F` and for `HZ31`. Field 3 is 0 in all 127 T1 frames before the reporter's TEST_AUTO_START marker, including two app-driven manual moves, and 1 in all 48 from that marker through the scan (AUTO_DETECT) to the end; T2, left in manual, is 0 throughout. Field 11 lands in the first frame after each command (75 while field 9 is still at 58; 0 while field 9 is at 61) and stays at the last manual value through auto mode while field 9 sits at 70-74. Field 10 is `0xFFFFFFFF` in 248 of 293 headers, 48 or 74 only around manual commands, and the sentinel again for the whole auto phase. Field 24 carries 789, 743 and 744 beside the small values. Field 14 is 96..100 on both units over 3.4 hours.
- **Reporter description, not frames.** The +10 offset (app cross-check at raw 0, 10, 75 on both units); the label "calculated optimal angle" for field 10 (app UI); the 25 degree run (field 11 to raw 15); the write command ids `2/24` and `2/19` and their values.
- **Existing code.** `_SN_PREFIX_MAP` and `_SN_PREFIX_DISPLAY_NAMES` (`ecoflow/const.py:71-240`), prefix-first classification in `get_device_type` (`:351-354`); parser dispatch by device type at `coordinator/mqtt_ingest.py:421-422` and `:522-523`; the BK-series helpers `_pdata_candidates` (`parsers/stream_proto.py:352-386`, plain bytes when `enc_type != 1` at `:377-378`), `_iter_fields` (`:220`), `_decode_scalar` (`:195`); the Enhanced-only guard for the meter spelled out as a type equality at `config_flow_setup.py:82`, `:244-250`, `:291`, `config_flow_options.py:174-177`, `:187-190`, `:235`, `__init__.py:297-310` and the key `smart_meter_requires_enhanced` (`strings.json:97`, `:158`); `unique_id = f"{sn}_{key}"` on every platform (`sensor.py:177`, `number.py:146`); two registry-cleanup helpers, one by unique-id suffix across all domains (`__init__.py:183-202`) and one by entity domain (`:205-225`); a never-reporting coordinator keeps `_last_mqtt_ts = 0.0` (`core.py:173`), skips the silent-device escalation (`availability.py:184`) and logs one INFO line at the hard threshold (`:225-236`).

**Decision:**

1. **One device type, `solar_tracker`, for both prefixes.** `HZ31` and `S02F` map to it in `_SN_PREFIX_MAP`; both display as "Solar Tracker". A second type for `S02F` would change no parser, no entity and no name, which makes it a synonym by the test this repository applies to contributions; a change of 2026-08-10 had the same shape. The registry's "Tracker 2" is a kit designation: the unit advertises `EF-HZ31...` over BLE and sends the same message with the same product id. No keyword: the app path returns an empty product name for every device, and the registry name matches no existing keyword list.
2. **Six entities, and the list is closed.** Tilt Angle (9), Target Angle (11), Optimal Angle (10), Light Level (4) as committed; Mode (3) as an enum with exactly the two states the frames show, `manual` (0) and `auto` (1), every other value an explicit `None`, never an exception; Battery (14) as the device's one `battery`-class sensor, added on the message definitions plus frames (the vendor names the field, every frame carries 96..100, a percent has one scale). Not shipped, each with its reason on file: `1.1.15` battery temperature (name known, scale unverified against any app reading), `1.1.24` (`track_num`, a counter: the message definitions close what the counter-measurement opened), and the remaining eighteen fields, none of which is a reading an owner watches. A third mode state for the scan is not invented: no frame carries one, and the scan reads back as `auto` in every scan frame.
3. **Scaling and sentinels.** Raw + 10 on fields 9, 10 and 11, published as integer degrees with unit `°` and no device class. `0xFFFFFFFF` on field 10 becomes an explicit `None` (key present, value `None`), the ADR-011 decision 2 contract, so the entity reads unknown instead of holding the last recommendation for the rest of the day; the frames show that exact sequence (48 for ten minutes, then the sentinel for an hour). Field 11 publishes the wire value in every mode: in auto it is the last manual setpoint, the mode sensor beside it says so, and deriving unknown would leave the round-2 number without a state. Light level is unitless with no device class: the wire maximum (1 439 885) is ten times the brightest sunlight, the vendor's field name `lux` does not change the number, and no factor is verified.
4. **Parser and path.** `ecoflow/parsers/solar_tracker_proto.py`, HA-free, reusing the header decoder and the three BK-series helpers; own to it: the `(32, 1)` map, the offset, the sentinel, the enum. Dispatch by device type at both ingest sites before the runtime-registry fallback; `(32, 1)` is claimed by nothing in `ecoflow/proto/runtime.py`. Enhanced Mode only: the path that carries this device is the app channel, and no Standard-mode evidence exists either way.
5. **The Enhanced-only guard becomes a set.** `ENHANCED_ONLY_DEVICE_TYPES` in `ecoflow/const.py` holds the meter and the tracker; the eight sites test membership; one error key `device_requires_enhanced` with a device-neutral text replaces `smart_meter_requires_enhanced` in `strings.json`, `en.json` and `de.json`. Two types spelled out as `or` at eight sites is the tell the rule names.
6. **Two keys are frozen for the controls.** `target_angle_deg` and `tracking_mode` are the data keys and the definition keys now, and the number and select keys in round 2, so the control shares its unique id with the retired sensor. Round 2 retires the sensor entries by entity domain in the shape of `_async_remove_legacy_hw51_stream_entities`, not through `_WITHDRAWN_ENTITY_SUFFIXES`, which matches the suffix in every domain and would delete the number's own entry on every start. The angle write is confirmed on field 11, the mode write on field 3; a scan has no read-back on this message and is round 2's problem to solve or refuse.
7. **The stale records get nothing.** A selected `HZ31` record that never reports costs one INFO line and unavailable entities, which is what every silent account device costs today; the picker already marks it offline.

**Trade-offs:**
- (+) One type, one parser, one name; the registry, the frames and the vendor's definitions agree on it
- (+) Every shipped value has a frame that sends it and either a vendor name or an app cross-check behind its meaning; the two that rest on description alone (the offset, the field-10 label) are marked on file
- (+) The mode sensor gives the target angle its context without hiding a wire value
- (+) A second Enhanced-only type costs one set and one string, not eight copies
- (+) Round 2 inherits keys, read-back fields and a retirement mechanism, and is blocked on the reporter's frames only
- (-) The optimal angle reads unknown most of the day on the reporter's own units (76 % of frames); that is the device, and the entity reference says so
- (-) The target angle shows a stale setpoint in auto mode; the mode sensor is the mitigation, not a fix
- (-) A sensor cannot become a number: at round 2 the entity id changes domain and automations on the sensor id need the new one. Sequencing round 2 into the same beta cycle keeps that off every full release, and the design on file says so
- (-) The battery is a fifth change to a list that had already changed twice publicly; it is the one addition the message definitions support outright, and leaving it out would reopen the list at the first owner's question
- (-) The guard generalisation widens a read-path PR by eight sites and three translation files

**Alternatives considered:**
1. Two device types, one per prefix - rejected in decision 1.
2. Ship the target angle as a `number` now with the write built from the message definitions and the reporter's description - rejected: no write is encoded onto a described protocol without frames (comment 5538238304), and a number that cannot write is the button that lies.
3. Hold the target angle back until round 2 to avoid the domain change - rejected: it breaks the four-entity commitment and the "same entity in round two" statement; the domain change is named instead.
4. Derive the target angle to unknown in auto mode - rejected in decision 3.
5. A third mode state for the scan - rejected in decision 2.
6. Battery temperature with `°C` on plausibility (34..37 in September sun) - rejected: scaling is verified per field, never inferred from a name or a plausible range; it ships when the reporter reads it off the app.
7. Copy the six meter guard sites for the tracker - rejected in decision 5.
8. Omit the key on the field-10 sentinel - rejected in decision 3: the last value would stand for hours.

**Consequences:** The field evidence, the file map and the test list with a mutation per test are on file. Round 2 is a separate plan that starts from the reporter's five sanitized `set`/`set_reply` frames and must answer three things this ADR leaves open: whether an angle write in auto flips the mode, what range the app offers, and what read-back a scan has. The release target (1.19.0-beta.5 or 1.20.0-beta.1) is the maintainer's call and is put to the maintainer with a recommendation for 1.20.0. Not solved here: the field-10 name conflict between the vendor schema and the app label (recorded, the label stands on the app check), the light level's scale, Standard Mode for this family, and BLE.

### Addendum 2026-09-10: the offset is the client's own and symmetric, the offered range is 10 to 109, the third mode value has a name, and one path is corrected

**What the public app bundle adds, stated on #339 on 2026-09-07.** Four things, each marked by what carries it:

1. **The +10 offset is the client's own and runs both ways.** The app subtracts ten when it writes an angle and adds ten when it reads one. Carried by the public app bundle, and consistent with the reporter's captured 25 degree run, whose write carries raw 15 on field 11. So the offset is not a device quirk the reporter has to defend, and decision 3's "raw + 10" on the read path is the mirror of the app's own write. Confirmed on the read side by the reporter's app cross-check at raw 0, 10 and 75; the write side rests on the bundle and the one captured run, and a maintainer-side replay is still to come.
2. **The offered range is 10 to 109 degrees, as the owner sees it.** The app only sends an angle inside its own range and drops anything outside it without a message. Carried by the public app bundle only. What the device itself accepts, clamps or rejects at either end has not been observed, and a capture of one run to each end of the range is what would settle it; the maintainer asked for it in the same reply. This answers "what range the app offers" from the Consequences above by description, not by measurement.
3. **The third mode value is a re-track.** The bundle's own label for it is a fresh tracking sweep that settles back to `auto`. Decision 2 stands: no frame on file carries a third value on field 3 (the parser's map still holds `manual` and `auto` and reads anything else as an explicit `None`), and every scan frame reads back as `auto`. What shape the re-track takes in Home Assistant, if any, is round 2's question; it is not a state and it is not invented here.
4. **The client waits for no confirmation.** It updates its display at once and relies on the regular status report to show the settled value, which is consistent with the reporter's `set_reply` frames being a reply the client ignores rather than no reply existing. A control built here confirms the same way, from the next status report. That also answers "what read-back a scan has": the report after the sweep, reading `auto`.

Left where they were: the tracking counter (field 24) has a name in the bundle, a plain counter, and how it increments is device behaviour, unconfirmed, so it stays out; whether an angle write in auto flips the mode is still open and still needs the reporter's frames.

**One path in decision 5 is corrected.** `ENHANCED_ONLY_DEVICE_TYPES` lives in the integration's `const.py`, not in `ecoflow/const.py`, and since ADR-020 it holds three members: the smart meter, the solar tracker and the WAVE 3. The eight membership sites and the single error key are as decided.

---

## ADR-015: A STREAM AC 5000 task list that arrives is the whole list, a kind absent from it reads unknown, from get_reply and push alike, with no settling hold

**Status:** Accepted (decided 2026-09-04; shipped in v1.19.0)
**Date:** 2026-09-04
**Depends on:** ADR-011 decision 2 (an explicit `None` is the unknown contract between parser and entity)

**Context:** The STREAM AC 5000 reports its scheduled tasks on `254/39 f40`, one `40.1` block per task, and a task's kind is the power container it carries (`_TASK_BLOCKS`, `ecoflow/parsers/stream_ac5000_proto.py:419-422`). `_finalize_task` (`:669-741`) publishes `scheduled_{kind}_*` for every kind it finds and clears all of `_TASK_KEYS` (`:654-666`) only when the frame carried `f40` empty, which `_EMPTY_GROUP_CLEARS` (`:430-432`) marks at `:955-957`. A non-empty list returns at `:736` without mentioning the kinds it lacks. The coordinator applies frames with `dict.update` (`coordinator/state_apply.py:186`), so a key a frame does not carry survives, which is what protects the setpoints against the majority of `254/39` frames that carry no `f40` at all. A list that shrinks from two tasks to one therefore leaves the deleted kind's keys in the store for good (#234, split out of #233).

Evidence, from @slaapyhoofd's two diagnostics exports of 2026-09-03 on v1.19.0-beta.4, decoded frame by frame with the repo's decoder (times UTC). A charge task was added beside a 0 W discharge task by the app at 22:10:47; the push at 22:10:49.018 carried both. The app removed the discharge task at 22:11:19.543 (operation 3 on slot 2, operation 2 on slot 1 in one `f39`); the `set_reply` at 22:11:19.841 echoed the charge entry alone; the `get_reply` at 22:11:22.075 carried both tasks, and its sequence number belongs to the app's own counter (387881005 to 522471009 through the session), so it was the app refreshing after its own write. No property `f40` was kept after that. After a config-entry reload, our post-connect get-all at 22:15:50.713 (sequence 1766797773-style, the masked millisecond clock of `build_device_get_all_payload`, `ecoflow/energy_stream.py:606-607`) and the push at 22:15:54.070 carried the charge task alone. At 22:16:30 our own write path removed the charge task and added a discharge task (event log), and the push at 22:16:33.712 carried the discharge task alone. His August recording carried both blocks in 16 of 16 frames across four minutes with nothing changing.

What that supports: `f40` is the complete list (a delta would have sent a lone changed task at least once in 16 unchanged frames, and the reload get-all is a full-state answer by construction). What it does not: the ordering between a get-all answer and the push stream near a write. A reply at +2.2 s carried the old list while pushes at +1.3 s and +3.5 s carried the new one, and the push that shrank the list after the app's deletion is not in the file.

`set_reply` never reaches the parser: `_parse_message` returns for every `/set_reply` topic before parsing (`coordinator/mqtt_ingest.py:157-186`) and `(254, 38)` is not in `_ES22_FIELD_MAP` (`:94-298`). Its `f39` is the echo of the write's own entries: one entry at 22:10:24, two at 22:10:47, two minus the removed one at 22:11:19. Our own writes name one task per frame (`set_commands.py:854-873`, `:938-985`). `get_reply` and the property push both go through `parse_stream_ac5000_message` (`mqtt_ingest.py:419-420`, `:517-518`).

The PowerOcean list already retracts a slot that leaves it, through `known_indices` in `remap_timer_task_keys` (`ecoflow/parsers/powerocean_proto.py:996-1001`, fed from `core.py:374` at `mqtt_ingest.py:674-676`), pinned by `tests/test_powerocean_timer_task.py:120` and `tests/ha/test_powerocean_schedule_entities.py:283`.

**Decision:**

1. **A present `f40` is the whole list.** A kind absent from it has no task, and every `_TASK_KEYS` entry with the prefix `scheduled_{kind}_` is published as `None`. Absence of `f40` keeps meaning nothing. The empty-list path is unchanged and the per-kind sets are derived from `_TASK_KEYS`, not kept as a second list, so the two clears cannot drift apart. The change is confined to `_finalize_task`; the merge at `state_apply.py:186` stays as it is.
2. **No settling hold.** Under rule 1 a stale readback harms only when it lands after a fresher one, and then the outcome is cleared, re-added, cleared again by the next `f40` push, which arrives about every 15 s while tasks stand (16 in four minutes, August) and at least one in roughly 37 frames (`:427-428`). Today the same stale frame re-adds the task and nothing clears it, and the re-add comes from the publish loop (`:719-734`), not from the clear, so excluding a topic from clearing would not remove it. A hold cannot be calibrated from one sample; its trigger for app writes does not exist, since the acknowledgement is not decoded (`mqtt_ingest.py:157-186`) and the app's write topic is subscribed only during a capture window (`ecoflow/cloud_mqtt.py:107-111`, `:366-378`); a hold keyed on our own writes in the shape of `_resolve_schedule_armed` (`state_apply.py:116-145`) would guard a case no export shows, the write path already asserts its own truth for the kinds it touched (`set_commands.py:883-896`, `:919-936`) and the number entity hides a bounce for five seconds (`number.py:216-217`, `:500`); and a hold on `get_reply` would discard the reconnect get-all (`cloud_mqtt.py:417`, `:838`), the one readback that is ours and that export B shows answering fresh. Watch item: a reporter seeing a deleted task reappear for seconds is the trigger for a hold on `get_reply`-borne task keys only, at least 3 s after a `254/38 f39` acknowledgement, and it needs a capture that keeps the push between a delete's ack and the app's refresh.
3. **`get_reply` and the property push both clear; `set_reply` never.** The two readbacks are device state through one parser and one grammar, and the reconnect resync is a `get_reply`. The acknowledgement echoes the write's entries, so reading it as the list would clear kinds the write did not name, which is the lost-write case from the issue body: the removal path sends only when the other kind's power is a known number (`set_commands.py:854-856`). Parsing stays off that path.
4. **The cleared value is `None`, shown as unknown.** Zero is a real setpoint on this device, a 0 W discharge task parks the battery while no task leaves the 200 W base output (`set_commands.py:952-954`); a zero would claim a state the device is not in, send a removal naming a dead slot (`:854-873`) and turn the next add into an update (`:973-985`). `None` is the contract of the two existing clear paths (`:737-741`, `:919-936`) and of ADR-011 decision 2; the number entity renders it as unknown and stops falling back to the restored value (`number.py:228-240`). For charge this includes `scheduled_charge_soc_target`, as the empty-list path already does; the write path keeps that key when it removes a charge task itself (`:930-933`) so a later charge write reuses the limit. The read path reports the device and a deleted task has no target; the difference is recorded, not widened.
5. **Absence is evidence only when the list was read in full.** A block that fails to decode (`_decode_task`, `:636-639`) or carries neither container (`:648`) makes the frame inconclusive about the kinds it does not show: it clears nothing and still publishes what it read. Otherwise one corrupt block would clear a live setpoint and, by rule 3's write-side consequence, drop the next removal.
6. **PowerOcean needs no change.** Its parser keeps a set of known indices because its slots are open-ended; the ES22 parser needs no memory because its slots are two fixed kinds. Different shape by necessity, no shared code, no second implementation.

**Tests:** nine tests with the mutation each one discriminates, on a fixture cut from the two exports (`tests/fixtures/stream_ac5000/es22_task_delete_masked.json`, seven frames by role, already masked to `XXXXXXXXXXXXXXXX`). The capture sequence itself is one of them: both-task push, the app's stale refresh (applied as is, decision 2 written down), the reload get-all, the push after it; it is red on today's parser at the third step.

**Trade-offs:**
- (+) A deleted task leaves the entities within one `f40` push, on every path that reports device state, with the same `None` the other clears use
- (+) The parser stays stateless and HA-free; the merge that protects the setpoints against frames without `f40` is untouched
- (+) The lost-write direction is closed twice: the acknowledgement is never read as the list, and a list read in part never clears
- (-) A stale get-all answer after a fresher push re-adds a deleted kind until the next push, seconds on the measured cadence; today it re-adds it for good
- (-) The charge target is cleared by the device's deletion and kept by our own removal; two paths, one named difference
- (-) The delete's own push was never observed; the rule rests on a full-state answer, a push after our own removal and 16 unchanged frames

**Alternatives considered:**
1. A hold on task keys after a write, as `_resolve_schedule_armed` does for the arming flag. Rejected: one sample of staleness, no decoded trigger for app writes outside a capture window, and it would silence the reconnect resync.
2. Clear from the property push only. Rejected: it leaves the reconnect get-all unable to retract, and the stale re-add it was meant to avoid is done by the publish loop, which the restriction does not touch.
3. Read `set_reply f39` as the list, which would retract at +0.3 s. Rejected: every echo on file is the write's own entries, and our writes name one task per frame.
4. Publish the device's zero after a clear. Rejected: 0 W is a setpoint the device acts on, and it would make the write path name a slot that no longer exists.
5. Keep known kinds in the coordinator, as PowerOcean does. Rejected: two fixed kinds need no memory, and the parser can decide alone.

**Consequences:** `_finalize_task` gains a per-kind clear and a completeness guard; `_TASK_KEYS` becomes the single source of both clears. The write path and the coordinator are unchanged. The next capture worth asking a reporter for is the push between a delete's acknowledgement and the app's refresh, which decides whether the watch item in decision 2 ever becomes work.

---

## ADR-016: A lower-case identifier joined to a serial is masked by that join, the length-delimited pass stays upper case, and a widening needs a frame that shows it

**Status:** Accepted (decided 2026-09-04; shipped in v1.19.0)
**Date:** 2026-09-04
**Depends on:** the length-delimited pass and its floor of 12; `_UUID_RUN` from PR #348

**Context:** A 12-character lower-case hex value (`3c39e723136e`) left a reporter's diagnostics download untouched and reached a public issue (#234). The fixture gate `tests/test_fixture_identifiers.py` refused it (`[0-9A-Za-z]{12,}`, any case); the product's passes are `_SERIAL_RUN` (`[A-Z0-9]{15,}`) and a length-delimited whole-field pass over `[A-Z0-9]` at 12 to 32 bytes (`ecoflow/frame_capture.py:35`, `:55-57`, `:78`, `:82-111`). The obvious answer, widen the delimited alphabet to lower case, was measured before it was decided, because an over-broad mask once destroyed 25 of 25 captured frames (`diagnostics.py:52-57`) and lower-case hex is commoner in binary than upper-case alphanumerics.

Measured 2026-09-04 on the two attachments (93 frames), the fixture tree (117 frames, every family) and the maintainer's local downloads (1439 frames). The value is not a whole field: it is the first 12 bytes of a 29-byte string in the header sub-message (`22 1d` then `<12 lower hex>-<16-character serial>`), 8 occurrences in 8 frames, never alone. A delimited alphabet widened to lower hex or to mixed case matches no field in any of the 1649 frames. A free-running `[0-9a-f]{12,}` masks 29 distinct 13-digit millisecond timestamps in 27 local JSON set frames. The joined shape `[0-9a-f]{12,32}(?=-[A-Z0-9]{15,})` matches the 8 occurrences and nothing else. With that pass added behind `_SERIAL_RUN` and `_UUID_RUN`: 0 frames change outside the attachments, 0 length mismatches, 0 non-string header fields change (`decode_header_message`), 0 readings change over 606 frames parsed by three proto parsers, and the gate goes from 13 failing frames to 0 in the attachments. The diagnostics text path (`_redact_serials`, upper-only as well) was measured over 207,829 strings in 41 downloads: no UUID, no lower-hex run, no MAC that shape would miss, and four PowerOcean serials in base64 under `moduleSn` in a skipped-device raw quota, which is a different defect (ADR-017). The log path has no shape pass; it masks topics by name and prints no frame bytes.

**Decision:**

1. **The length-delimited pass does not widen and `_IDENT_MIN` stays 12.** It would not reach the value, and it matches nothing on file, so there is no frame to be its positive control. The floor argument of the original pass stands.
2. **A fourth shape pass masks a lower-case hex run of 12 to 32 characters that a hyphen joins to a serial-shaped run**, `X` for `X`, hyphen and serial kept. Its boundary is the serial: `[A-Z0-9]{15,}` after the hyphen, which a masked serial also satisfies, so the order against `_SERIAL_RUN` is immaterial. It runs after `_UUID_RUN` and before `_TIME_ZONE`.

   **Amended on implementation, 2026-09-04.** The upper bound of 32 is dropped; the run is `[0-9a-f]{12,}` before the anchor. A cap does not decline to mask a longer run, it masks that run's last 32 characters and leaves its front standing, and a front of 12 or more is itself the shape the fixture gate refuses: measured, a 44-character run left `0123456789ab` behind. Since this decision already states that the anchor carries the safety rather than the width, the width was not a second safety and only bought that partial case. No run over 12 characters exists in any of the 1649 frames, so the change moves no byte of any recording; a test pins a 120-character run so the cap cannot return unnoticed. Two couplings the original text left implicit are now in the code comment: the claim that the order against `_SERIAL_RUN` is immaterial holds only because `_MASK_BYTE` is `X`, which is itself inside `[A-Z0-9]`, and the order against `_UUID_RUN` is *not* immaterial but extending, since a masked UUID creates a serial-shaped run that did not exist before.
3. **No hex-only whole-field alternative until a frame presents a bare lower-hex field.** It is the narrowest of the candidates (16 of 256 byte values) and the right shape for that case; it is recorded, not added.
4. **The fixture gate stays stricter than the product.** The gate has no corruption cost, the product has; a digit-only run (a timestamp) is refused by the gate and must be kept by the product; the fixture author masks the rest by hand and says so.
5. **Positive control: the two attachments. Negative control: every frame in the repo, re-parsed per family** - the corpus sweep re-parsed the ES 5000 only and gains a family-agnostic header comparison and every pure parser entry - **plus the 1439 local frames by hand**, numbers in the PR.
6. **The gap is frame capture's, for this shape.** The text path is not widened (no positive control); its base64 finding goes to ADR-017.

**Trade-offs:**
- (+) The one shape on file is masked at the source, with a boundary ordinary binary does not produce (0 hits in 1556 other frames), and the hand edit in the ADR-015 fixture becomes the product's own output
- (+) Nothing on the free-running side moves; the 27-frame timestamp damage stays a number in the PR rather than a regression
- (+) The sweep that guards every mask change stops being an ES 5000-only sweep
- (-) A bare lower-hex identifier, or an upper-case one joined to a serial, still passes; each is one class change away and each waits for a frame
- (-) The local corpus is not in CI; its result is a number in a PR description, re-run by hand on every mask change

**Alternatives considered:**
1. Widen `_IDENT_ALPHABET` to `[A-Za-z0-9]`. Rejected: it does not reach the value (the field has a hyphen), matches nothing on file, and raises the per-position false-positive rate from about 6e-11 to about 4e-8 while covering the tag bytes of fields 12 to 15 in every wire type.
2. A hex-only whole-field alternative now. Rejected for now: zero fields in 1649 frames present one; recorded as the shape for when one does.
3. Free-running `[0-9a-f]{12,}`, or lowering `_SERIAL_RUN` to 12. Rejected: 27 frames lose their timestamps in the local corpus alone; this is the class of change that destroyed 25 of 25 frames once.
4. Lower the fixture gate to what the product masks. Rejected: the gate is what found this; a gate that matches the product finds nothing the product misses.

**Consequences:** `sanitize_frame` gains one pattern and one line; `TestMaskingDoesNotCorruptRealFrames` gains a header comparison and per-family parsing with an asserted checked count; `_UUID_RUN` gets the unit test #348 shipped without. Every future mask change is measured against the same three corpora before it is decided, and the numbers go into the PR. ADR-017 carries the base64 serials.

Every number above was measured over frames as stored, which is over the payloads a device sent in the clear: a payload the device had XOR-masked (`enc_type == 1`, key `seq & 0xFF`) was outside all six passes and outside all of these counts, and ADR-023 carries it.

---

## ADR-017: A serial written in base64 is masked by decoding it, aliased as its plain form, ahead of the plain pass; the text path widens nowhere else

**Status:** Accepted (decided 2026-09-04; shipped in v1.19.0)
**Date:** 2026-09-04
**Depends on:** ADR-016 (whose text-path measurement found this, and whose rule applies: a mask is decided at the wire format of the value, not widened at the pattern); the alias map in `_redact_serials` (built against two battery packs collapsing onto one marker)

**Context:** Four PowerOcean serials in base64 (`SEozN1RFU1RCQU00MFRYNQ==` decodes to `HJ37TESTBAM40TX5`) passed `_redact_serials` under `moduleSn` keys (`error_code.emsErrCode.moduleSn`, `error_code.pcsErrCode.moduleSn`, `error_code.bpErrCode[n].moduleSn`) in the skipped-device raw quota of two of the maintainer's own downloads of 2026-08-12. `_SERIAL_RE` is `[A-Z0-9]{15,}` (`diagnostics.py:47`); base64 is mixed case with `=` padding and carries no such run in any of the four. The plain copies of the same serials in those files were redacted and aliased, so the pass did what it could see.

Measured 2026-09-04 over the maintainer's local corpus (39 JSON files, 221,992 strings outside hex keys) and the fixture tree (18 files, 1,933 strings):

- *Reach.* Every `moduleSn` on file sits under `skipped_devices`: 8 values in 2 files, 0 under `devices`. The five routed PowerOcean downloads on file are all Enhanced Mode, where no quota is polled and `raw_quota.values` is empty, so they cannot show the key. In Standard Mode `coordinator/http_poll.py:115` stores `dict(raw)` of the same `quota/all` response verbatim (the `diagnostic` flag of `get_quota_all` changes only the rate-limit purpose, `cloud_http.py:69-84`) and `diagnostics.py:579-599` exports it under `devices/[n]/raw_quota/values`. The shape therefore reaches a routed Standard Mode PowerOcean by the code path, with no download on file to show it. The single pass on the way out covers both sections.
- *False positives, the negative control for decode-and-test.* 7,196 strings have the base64 shape (standard alphabet, 20 or more characters, length a multiple of 4). 7,188 decode to bytes that are not ASCII: hex strings under `payload_hex` and `pdata_hex` in raw MQTT captures, since a hex string of even length is always valid base64 and always decodes to binary. 0 decode to printable ASCII that is not a serial, 0 to ASCII with a serial among other text, 8 decode to exactly a serial: the eight known values. The ASCII test and the full match do all of the rejecting; the shape pre-check does none.
- *Order.* Base64 of a 16-character `[A-Z0-9]` serial carries a run of 15 or more upper-case letters and digits in 6.5% of cases (13,064 of 200,000 random serials; `HJ31TESTBAM40TX5` encodes to `SEozMVRFU1RCQU00MFRYNQ==`, which carries `MVRFU1RCQU00MFRYNQ`). The plain pass run first turns that into `SEoz**REDACTED**==` (reproduced on today's code): a fragment of the serial in a form no decode recognises. None of the four observed values has such a run, which is why they leaked whole.
- *Named variants.* The serials an entry can name are its devices' own: the routed coordinators' `device_sn` and the skipped devices' `sn`. Of the four values, one is the skipped device's serial; the PCS module and the two battery packs are module serials no entry ever holds. A named pass masks 1 of 4.

**Decision:**

1. **Decode-and-test, in the string branch of `_redact_serials`, ahead of the plain pass.** A string that is standard base64 - alphabet `[A-Za-z0-9+/]`, 20 or more characters, length a multiple of 4, `=` padding at the end only, accepted by `b64decode(validate=True)` - whose decoded bytes are ASCII and fullmatch `_SERIAL_RE` is replaced whole by the alias of the decoded serial. A string that fails any step falls through to the plain pass unchanged; the plain pass itself does not change.
2. **One alias per serial across both forms.** The alias map is keyed by the plain serial and the decoded serial is looked up in the same map, so whichever form the walk meets first takes the marker and the other reads the same one. The marker is written in plain text, not re-encoded: the reader is meant to see `**REDACTED-3**` where the pack was, and a base64 marker would be decoded by hand to find a marker.
3. **By shape, not by key.** The test sits in the string branch, so keys and values are covered alike and a `moduleSn` under a section added later, or a key the vendor names differently, is covered whether or not its author thought about it. That is the rule `_NAME_KEYS` follows for a shape the pass cannot see; here the shape can be seen, so no key list.
4. **Nothing wider.** No URL-safe alphabet, no serial among other decoded bytes (fixed-width padding, a composite), no base64 inside a frame, no change to `_PRE_SANITIZED_KEYS`. None is on file; each is one observation away and is recorded as not solved here.
5. **Controls.** Positive: the raw-quota shape with test serials in both forms, and the synthetic serial `HJ31TESTBAM40TX5`, which comes out as one whole marker only with the decode ahead of the plain pass. Negative: a 24-character hex string under a key outside `_PRE_SANITIZED_KEYS` stays byte-identical - the corpus' 7,188 in one test - and the text-path sweep (a local script) over the downloads before and after, 8 base64 serials to 0, numbers in the PR.

**Trade-offs:**
- (+) Module and pack serials the entry never names, which is the observed case, are masked, with 0 false positives across 223,925 strings
- (+) One marker per pack in either form, so a dump still counts packs correctly, which is what the alias map exists for
- (+) A decode per string that passes the shape check: 7,196 on file, every one rejected at the ASCII step; the cost is not measurable against the download
- (-) The field changes form: a base64 value is replaced by a plain marker. Accepted, and preferable to a marker in base64
- (-) A serial in base64 among other text is not caught; no such value is on file

**Alternatives considered:**
1. Named variants (the base64 of every serial the entry holds, the way `sanitize_frame` tries `.upper()` and `.lower()`). Rejected: 1 of 4 on file; module and pack serials are not entry-known and never will be, the quota is where they come from.
2. A key-name rule (`moduleSn`, any key ending in `Sn`). Rejected: a key rule is for a shape the pass cannot see (`_NAME_KEYS`, four characters); a decodable value is a shape it can see, and the next key the vendor picks is not on the list.
3. A search over the decoded text instead of a full match. Rejected for now: no value on file has a serial among other decoded bytes; recorded as the widening to add with the value that shows it.
4. Plain pass first, decode after. Rejected: 6.5% of serials get a partial mask that leaks the rest and defeats the decode.
5. Widen `_SERIAL_RE` to mixed case. Rejected: the base64 form is not a serial-shaped run, and a mixed-case run of 20 or more would eat camelCase keys and every hex value outside the pre-sanitised keys - the over-broad mask of `diagnostics.py:52-57` in another alphabet.

**Consequences:** `_redact_serials` gains a decode step ahead of the plain pass, and the alias lookup takes a serial string rather than a match. The `_redact_serials` unit class in `tests/ha/test_diagnostics.py` gains the tests listed for this decision; one section-level test covers the skipped-device raw quota and one the routed `raw_quota.values`, since the reach claim for the routed path rests on the code and not on a download. The text-path sweep is the measurement for every future change to `_redact_serials`, as the corpus sweep is for `sanitize_frame`; both run by hand and their numbers go into the PR.

Every number above was measured over the strings as stored, which is over the values a device sent in the clear: the frame bytes under `payload_hex` and `pdata_hex` were counted as base64 false positives and never opened as frames, so a serial inside a payload the device had XOR-masked is outside all of these counts and belongs to the frame path, where ADR-023 carries it.

---

## ADR-018: The BK21 energy record holds six lifetime counters; import and export are `total_increasing`, the four net figures are `total`, the keys name the direction, and the five pre-release entities are withdrawn rather than migrated

**Status:** Accepted (decided 2026-09-05; shipped in v1.19.0-beta.8)
**Date:** 2026-09-05
**Depends on:** the BK21 read path of v1.19.0-beta.1 (this corrects it); `_WITHDRAWN_ENTITY_SUFFIXES` and `_async_remove_withdrawn_entities` in `__init__.py` (the withdrawal mechanism built for the Delta 3 `ac_charge_mode` select of v1.16.0-beta.11/12); the monotonic guard derived from `const.py` (`mqtt_ingest.py:83-121`)

**Context:** The first mapping of the BK21's nested energy record `773` took a 17-minute capture from the day the meter was installed, in which `.4` and `.7` were equal in every frame and `.6` was absent. The vendor schema names `.4` `today_active`, `.7` `total_active_energy`, `.1/.2/.3` `today_active_L1..L3`, and lists `.5`/`.6` as reactive energy; the five entities were built on those names (`grid_energy_today_wh` and three phase `_energy_today_wh` as `total_increasing`, `grid_energy_total_wh` as `total`), with a documented request to the reporter for a capture across midnight and an app reading on a day the house exports.

@wildnet delivered both on #331. On 2026-09-03 he switched his zero-export house to export for a few minutes: `.6` climbed 25 to 28 Wh, phase B read about -277 W and its counter `.2` fell, and the app showed import 26.65 kWh / export 28 Wh / net 26.62 kWh against a record of `.4` 26,614 / `.6` 28 / `.7` 26,586. On 2026-09-05 he attached an 18 h 40 min diagnostics download from `v1.19.0-beta.1` (31,761 frames, one MQTT session, `reconnect_attempts: 0`) across local midnight. Re-read here with the product's own parser: five retained `773` records, one before midnight and four after, `.4` 26,771 to 33,983, `.6` 28 throughout, `.7` 26,743 to 33,955, `.1/.2/.3` 3 / 10,666 to 13,668 / 16,074 to 20,284. In all five, `.1 + .2 + .3 = .7` and `.4 - .6 = .7` exactly, and no subfield falls at midnight. The app two minutes after the last record shows the same six figures at its display precision. `.5` is in no record of either capture.

So: `.4` is cumulative import, `.6` cumulative export, `.7` cumulative net import, `.1/.2/.3` cumulative net per phase. Nothing on the wire is daily. The schema's `today_` prefix and its "reactive" label are both wrong for this device; a definition says what a field is called, the numbers say what it holds. The encoder omits a subfield at zero from the record: `.1` was absent in every frame of the first capture with phase A idle at 0 Wh and is present at 3 Wh in every record of the second; `.6` was absent before the house had ever exported and present at 28 after.

In the tree today, four entities carry "Today" on lifetime counters, one is named "Total" on a net figure, the three phase nets and the import counter share `total_increasing`, the export counter has no entity, and the `const.py` comment argues `total` for `.7` from "the message definition carries no export counter". Falling `total_increasing` sensors do three things: Home Assistant's recorder treats a drop below 90 % of the previous value as a meter reset and re-counts the standing total into the long-term sum (`sensor/recorder.py:480-487`, `:786-795`, 2026.4.0), it logs a WARNING once per entity inviting an issue for a drop inside the band (`warn_dip`), and our own `_enforce_monotonic` drops every decreasing reading so the sensor freezes for the duration of the export. None of it showed on the reporter's install, whose only export is the 28 Wh test.

**Decision:**

1. **Direction decides the class, not period.** `.4` import and `.6` export are `total_increasing`: they only stand still or rise, the class enrols them in the coordinator's monotonic guard automatically, and they are the Energy Dashboard's grid-consumption and return-to-grid entries. `.7` and `.1/.2/.3` are `total`: they fall whenever the house exports, `total` records the negative delta as what it is, permits a negative value (a net exporter's `.7` crosses zero), and keeps them out of the monotonic guard. `measurement` is excluded by Home Assistant itself: `device_class: energy` admits only `total` and `total_increasing` (`sensor/const.py:777-779`).
2. **The key names the direction; six quantities, six entities.** `grid_import_energy_wh` "Grid Import Energy", `grid_export_energy_wh` "Grid Export Energy", `grid_net_energy_wh` "Grid Net Energy", `grid_l1_net_energy_wh` / `grid_l2_net_energy_wh` / `grid_l3_net_energy_wh` "Phase A/B/C Net Energy" - the convention the PowerOcean and Stream families use for the same quantities (`grid_import_energy_kwh`, `grid_export_energy_kwh`), in the meter's own unit. Import icon `-import`, export `-export`, the nets the neutral tower. The net figure keeps an entity: it is the app's headline "Net import", and a statistics sum cannot be derived from two other sensors after the fact. 17 sensors become 18.
3. **Withdrawal, not migration.** The six keys are new unique ids. The five old ones (`_grid_energy_total_wh`, `_grid_energy_today_wh`, `_grid_l1_energy_today_wh`, `_grid_l2_energy_today_wh`, `_grid_l3_energy_today_wh`) go on `_WITHDRAWN_ENTITY_SUFFIXES` and are removed from the registry on setup, exactly as the `ac_charge_mode` select was. Each key is unique to the meter and none is a suffix of another, so the list's existing tests keep their meaning. No `unique_id` migration exists in this tree (`new_unique_id`, `async_update_entity`: zero hits; `async_migrate_entry` moves the config entry schema only) and none is introduced: it would keep entity ids that say `today` on lifetime counters, carry forward statistics the monotonic guard censored during any export, and protect the owners of `v1.19.0-beta.1` to `beta.7` of a device first shipped on 2026-09-02 - a few days of statistics on five sensors, one dashboard re-selection, and any automation naming the old ids, all stated in the CHANGELOG. The deliberate-break precedents are the v1.5.x pack renumbering, the v1.13.0 `min_discharge_soc` removal and the Solar Tracker's round-2 replacement (#339).
4. **Absence of `.6` inside a present record reads as zero.** A house that has never exported never receives `.6`; without a rule its export sensor reads unknown for months beside an import sensor in the tens of kilowatt-hours. The parser publishes `grid_export_energy_wh = 0.0` when a `773` record decodes without `6`. The fill is for `.6` alone: it is the one counter whose zero is a steady state, and its `total_increasing` class protects it - a partial record without `.6` while export stands above zero (none on file; every record carries `.2 .3 .4 .7`) would be a decrease and `_enforce_monotonic` drops it. Net keys carry `total`, where a wrong zero is a real drop, so they are never filled. `_LIFETIME_KEYS` shrinks to the import key: an explicit zero on import is still a glitch, an explicit zero on export is the fill, a zero on a net figure is a reading.
5. **Release: `v1.19.0-beta.8`, inside the running cycle.** The beta-cycle rule excepts bugs against functions the cycle itself introduced; the BK21 exists only in the 1.19.0 pre-releases, the cycle was held open for this capture, and a stable release with these entities would put the wrong name and the wrong class into every stable owner's long-term statistics, where the same rename then costs everyone their history. The export entity is part of the correction, not a feature beside it.

**Trade-offs:**
- (+) Every class matches the direction the device's own arithmetic shows, on both sides of midnight and through an export
- (+) The Energy Dashboard gets its two grid entries from the meter's own counters; no Riemann sum, no derived daily figure
- (+) The monotonic guard covers import and export by derivation and can no longer freeze a falling net figure
- (+) The five wrong entities vanish from the device page instead of sitting there unavailable
- (-) Owners of beta.1 to beta.7 lose a few days of statistics on five sensors and re-select the dashboard entries; accepted, and cheapest now
- (-) The export fill infers a zero from absence; the inference rests on `.1`'s and `.6`'s own absence-then-presence and is guarded by the class
- (-) A negative-going `total` sensor is a shape some dashboard cards handle poorly; it is the truthful shape

**Alternatives considered:**
1. Keep the keys, change names and classes only. Rejected: the code and every diagnostics download would carry `grid_energy_today_wh` holding a lifetime import forever, and the entity ids owners already have would keep saying `today`. Misleading names are what produced this error.
2. Migrate unique ids with `async_update_entity(new_unique_id=...)`. Rejected: no precedent in the tree, a new mechanism for a population of pre-release testers, and it would preserve ids and statistics that are wrong in themselves.
3. `total_increasing` on the nets with `last_reset` or a ceiling. Rejected: the value is not a reset, it is a decrease; the class promises what the device does not.
4. `measurement` on the nets. Rejected by Home Assistant's own device-class rules.
5. No export entity, net only. Rejected: the dashboard would have no return-side entry from a meter that keeps one, and `.7` would have no explanation on the device page.
6. Leave the export sensor unknown until the first export. Rejected: months of unknown beside a live import counter on every zero-export house, for a value the device's absence defines as zero.
7. Defer to 1.20.0. Rejected: it converts a cheap pre-release break into a stable-channel one.

**Consequences:** `_ENERGY_RECORD_MAP` maps six subfields to six new keys and the parser fills `.6`; `_LIFETIME_KEYS` holds the import key; `SMARTMETER_SENSORS` carries six energy definitions and a comment that argues from the record rather than from the schema; `_WITHDRAWN_ENTITY_SUFFIXES` gains five entries; `strings.json`, `en.json`, `de.json` swap five keys for six. `tests/test_smart_meter_parser.py` gains a fixture cut from the five midnight records (the fixture gate must pass on it) and tests for both identities, the fill and its negative, and the narrowed zero guard; `tests/ha/test_smart_meter_entities.py` pins the classes, the monotonic-key derivation and a falling net reading reaching the sensor; `tests/ha/test_withdrawn_entities.py` covers the five suffixes on a `BK21` serial. The entity reference, `documentation/README.md`, two README lines and the 1.19.0 CHANGELOG paragraph are rewritten, the CHANGELOG crediting @wildnet's export test and midnight capture. Still open on #331 and not part of this decision: the maintainer's promise that the power factor reads unknown instead of 0.0 while power flows, which is not in the tree; the recommendation is to carry it in the same beta. `773.5` stays unmapped until a frame carries it.

---

## ADR-019: A device is named in a log by its serial prefix and a one-way tag, never by more of the serial; the pickers show prefix and tail because they are the owner's own screen

**Status:** Accepted (decided 2026-09-05; shipped in v1.19.0)
**Date:** 2026-09-05
**Depends on:** the four-character convention practised at `diagnostics.py:556` and 85 log sites (v1.16.0-beta.16); the #341 fix (v1.19.0-beta.4) that masks the vendor-name tail in diagnostics so a file never carries both ends of a serial

**Context:** Two sites in the tree take eight characters of a serial: the coordinator name `f"EcoFlow {self.device_name} ({self.device_sn[:8]})"` (`coordinator/core.py:164`, from v1.0.0) and the setup picker label (`config_flow_setup.py:77`); the options picker takes twelve (`config_flow_options.py:231-232`). Home Assistant prints the coordinator name from its base class at DEBUG on every push (`Manually updated %s data`, `update_coordinator.py:603`), at INFO on recovery and at ERROR on every fetch failure, and names the refresh task after it, which shutdown warnings print. A log @AndyBowden attached to #347 shows it: 13 lines `Manually updated EcoFlow PowerOcean (J32ETEST) data`, the one eight-character run in a file whose other 160 lines keep to four. A third site prints the full sixteen: `availability.py:235`, `Device %s [%s] became unavailable`, INFO or WARNING, on the event that makes owners attach logs. A fourth is latent: an `OSError` from the state file `ecoflow_energy_{sn}.json` prints its filename through `energy_integrator.py:433, 447`.

The eight characters exist to tell devices apart, and the reporter's installation is the case: two `J32E` inverters, both reporting the product name "PowerOcean", identical at four characters on all 85 of our own lines. Whether characters five to eight differ between two units of one model bought together is not on file (the only same-prefix pair in local captures is an inverter beside a battery module). What is on file is that EcoFlow's own default device names distinguish units by the **last** four characters (`get_device_name`, `ecoflow/const.py:344`), which is the #341 pattern: a file carrying the first four in one place and the last four in another has published both ends.

**Decision:**

1. **The log names a device by prefix and a one-way tag.** `device_log_tag(sn)` in `ecoflow/const.py` returns `f"{sn[:4]}-{sha256(sn.encode()).hexdigest()[:4]}"`, e.g. `J32E-b86f`. The coordinator name becomes `EcoFlow {device_name} ({tag})`. The tag is a pure function of the serial, so it is the same across restarts, reinstalls and machines; it differs for two devices of one model except with probability 1 in 65,536; it reveals nothing about the twelve characters behind it (about 7 x 10^13 serials share any one tag); and the owner can compute it from a serial they hold. The prefix stays because the model is what a reader needs first and it is already on every line.
2. **The tag is written into the diagnostics download**, `device_tag` beside `device_sn: "J32E..."` and beside `sn_prefix` in each `skipped_devices` entry, so a log and a download from one installation correlate by a value that carries no more of the serial than either does alone. It must survive the redaction passes unchanged, proven on the real output rather than by reading the patterns.
3. **Every four-character log, task-name and thread-name site moves to the tag in the same change** (85 sites; the five classification sites and the `device_sn` diagnostics field keep the bare prefix). One tagged line among 85 does not make two same-model devices distinguishable, and the format changes once, before the 1.19.0 full release fixes what reporters quote.
4. **The full serial at `availability.py:235` and the `OSError` filename path are closed.** The first becomes the tag; the second logs the error's class and `strerror`, never a string that carries the path.
5. **The pickers show eight characters, as prefix and tail, through one helper in both flows.** `short_serial(sn)` returns `J32E...0TX5`. The picker is the owner's own screen showing their own device; nobody is asked to publish it, and the serial is the only thing they can match against the app or the sticker. The tail, not the middle, because the tail is what the vendor's default names use to tell units apart. The setup flow's eight and the options flow's twelve become one shape.
6. **No migration.** The coordinator name is read by nothing but the base class (no `self.name` / `coordinator.name` reads, no `Store`, integrator state keyed by the full serial, entities keyed by `unique_id`, picker option keys are serials). Task names are ephemeral.
7. **The convention is written down**: the logging convention states that "Device SN" in a log means the tag and never more than the four-character prefix, including the strings HA prints for us; the bug report template says logs and diagnostics carry at most the first four characters of a serial.

**Trade-offs:**
- (+) Every artifact we ask an owner to attach - log at any level, diagnostics download - keeps to the same four characters, and the two correlate by the tag
- (+) Two devices of one model are distinguishable on every line, not on one
- (+) The tag is HA-free, one function, testable with a fixed vector
- (-) A reader of a log cannot map a tag to a serial without the owner or the download; that is the point, and it costs one question in a thread
- (-) 85 mechanical edits and three test-text updates in a pre-release the cycle wants to close; accepted because a later format change would be paid by every reporter
- (-) Collision 1 in 65,536 per pair; the download still tells them apart by position
- (-) A residual stays: for the families whose vendor name carries the tail (Stream, base DELTA 3), the HA device name and every entity id carry the last four characters, and HA prints entity ids in its own warnings. Entity ids are frozen at first registration, so a rename reaches no existing install; the name is the one the owner sees in the app. Named, accepted

**Alternatives considered:**
1. Cut the name to four characters and stop. Rejected: it makes the reporter's two inverters identical on the one line that could distinguish them, and leaves the 85 others as they are.
2. The last four characters. Rejected: with the first four on every other line, one log carries both ends of the serial - the pattern #341 fixed in diagnostics four days earlier.
3. Characters five to eight alone. Rejected for the same combination, and it is not known to discriminate two units of one model.
4. A running index per config entry. Rejected: it shifts when a device is added or removed, so two logs from one installation disagree, and it correlates with nothing the owner or the download shows.
5. The device name alone. Rejected: identical for the reporter's two units; only owners who renamed devices in the app would benefit.
6. A longer tag (six or eight hex). Rejected: every log line grows for a collision margin one account of a handful of devices does not need.
7. Full serial in the pickers. Rejected: a screenshot of the picker is a thing owners do post, and eight characters in the discriminating positions serve the selection as well.
8. Only the leak closure in beta.9, the sweep later. Rejected: two format changes for reporters, and the tag's purpose is unmet until the sweep lands.

**Consequences:** `ecoflow/const.py` gains `device_log_tag`; the coordinator gains a `device_tag` property and its name uses it; `availability.py:235` and `energy_integrator.py:433, 447` stop printing anything that carries the serial; `diagnostics.py` and the `skipped_devices` builders in `__init__.py` add `device_tag`; `config_flow_setup.py` and `config_flow_options.py` render one `short_serial`; 85 `[:4]` sites become tag sites; `tests/test_cloud_mqtt.py:979`, `tests/test_powerocean_parser.py:1521` update their expected text; new tests pin the HA-printed line via `caplog`, the diagnostics survival of the tag, the `OSError` path and both picker labels; a Docker window in both modes greps the running log for any run longer than the prefix and for the tag as its positive control. The logging convention, the bug report template and the CHANGELOG state the convention. Not decided here: #347's request that the second inverter be captured, which needs a diagnostics download before it can be a plan.

---

## ADR-020: WAVE 3 controls inherit the read-path keys; shared ConfigWrite builder; refusal rules in the core library

**Status:** Accepted (decided 2026-09-07; PR #360, merged 2026-09-07; shipped in v1.20.0, first in the v1.20.0-beta.2 pre-release; amended 2026-09-10, see the addendum). **Date:** 2026-09-07. **Depends on:** ADR-014 dec 6, ADR-011 dec 2, ADR-018. **Decision:** the WAVE 3 setpoint sensors and the running flag become the controls under their read-path keys in the same beta cycle, retired by entity domain through one table-driven helper; the write path is the shared ConfigWrite builder with `dest` and a float32 form, a WAVE 3 control table in the core library that also owns the app's refusal rules, one coordinator method that evaluates them against accumulated state, and the acknowledgement gate widened for logging only; the read-back is the device's own push. **Trade-offs:** (+) one key per fact, (+) rules testable without HA, (+) no second builder, (-) the domain change is visible to the maintainer's beta.1 install, (-) two labels a select offers are refused as writes until captured. **Alternatives:** coexisting sensors and controls (rejected: two names for one fact, and the cost of not coexisting is zero today); a WAVE 3 builder (rejected: same header but one byte); a pending-write registry keyed by seq (deferred: no `config_ok=False` on record).

### Addendum 2026-09-10: one builder, two coordinator entry points

The decision says "one coordinator method" and the trade-offs say "no second builder". Since ADR-021 shipped the constant temperature band, the band has its own trio beside the single-field set: `build_band_write` and `band_write_refusal` in `ecoflow/wave3_commands.py` and `async_send_wave3_band` in `coordinator/set_commands.py`. Both trios end in the same shared ConfigWrite builder and the same transport, so the builder claim holds and the method count does not. Read the decision as: one control table, one shared builder, two coordinator entry points over it (the single-field set and the band pair), and no refusal rule outside `wave3_commands.py`. The band's own record is ADR-021 and its addendum.

---

## ADR-021: The WAVE 3 climate entity is a facade over the existing control path, not a second path

**Status:** Accepted (decided 2026-09-07; shipped in v1.20.0, first in the v1.20.0-beta.4 pre-release; amended 2026-09-10, see the addendum). **Date:** 2026-09-07. **Depends on:** ADR-020.

**Context:** The WAVE 3 (AC71) carries fourteen controls in Home Assistant since ADR-020: four switches, five numbers, five selects, all against real hardware on 2026-09-07, all writing through one path (`coordinator.async_send_wave3_set(key, value)` to `write_refusal()` against accumulated state to `build_write()` against a control table to one ConfigWrite frame). Phase C adds a `climate` entity so the thermostat card, `climate.set_temperature` and scheduler integrations reach the device. The open question is not whether the entity works but whether it is a second way to the same data, which the architecture forbids. A capture of the vendor app on 2026-09-07 (472 frames, 50 ConfigWrite, positive control on the name join) settles the one protocol question the entity depended on: the constant temperature band is written as a single ConfigWrite carrying both limits, pdata `f5 09 <float32 LE upper> fd 09 <float32 LE lower>`, twelve bytes, upper first, in twelve of twelve band writes, never one limit alone. Its acknowledgement echoes field 158 only, so the acknowledgement is not a read-back of the lower limit.

**Decision:**

1. **The climate entity is added and the fourteen controls stay.** ADR-020 retired *sensors* that duplicated *controls*: two entities each holding a copy of one fact, either of which could go stale independently. That is a second implementation. A climate entity is a second UI surface over one implementation, and it holds no fact of its own. The test that separates the two cases, and the constraint this decision imposes: **can the two surfaces disagree?** Two implementations can. Two views cannot, unless one caches. So the climate entity keeps no state: every property reads `coordinator.data` on access, there is no optimistic write, no local copy of a setpoint and no second refusal logic. One control table, one `write_refusal()`, one builder, one coordinator method, one frame on the wire. A change to a rule reaches both surfaces in the same edit, because there is only one place to edit.
2. **Identity:** unique id `{sn}_climate`, `_attr_has_entity_name = True`, `_attr_name = None`. The entity takes the device name because it is the device's main feature, which is the platform convention for exactly one entity per device. Every other platform here also sets `_attr_has_entity_name = True` and additionally sets a name; the climate entity is the one that sets it to `None`.
3. **`preset_modes` is exactly the vocabulary of `select.operating_submode`:** `none, normal, max, sleep, eco`. Two of the five are refused as writes today, and ADR-020 already accepted that cost on the select. Inventing a shorter list for the climate entity would be two vocabularies for one fact, and it would leave the reported state outside the offered list whenever the device reports `normal`, which it does after any write. The refusal surfaces as `raise_set_rejected`. The defect closes when a single-action capture shows how the app clears a preset, at which point both surfaces gain the write together.
4. **`hvac_action` is OFF when the unit is not running**, COOLING / HEATING / FAN / DRYING when it is, and **`None` in constant_temp**, because nothing on the wire says which side of the band the unit is working, and deriving it from ambient temperature or input power would be invention. This decision was drafted with a third state, IDLE for a unit that is on but has reached its setpoint, and the implementation dropped it: the parser publishes no key that separates the two. `running` is the inversion of field 212, the same bit the Power switch renders and the same bit the app's own toggle renders, so "on" and "running" are one boolean here. An IDLE branch would need a key no message has been observed to carry, which is the invention this same decision refuses one sentence earlier. It returns if a capture ever shows a unit reporting on and idle separately.
5. **No `enhanced_only` flag** on the climate definition. `DEVICE_TYPE_WAVE3` is already in `ENHANCED_ONLY_DEVICE_TYPES`, and a flag expressing a distinction the codebase already expresses is a synonym, not an abstraction.
6. **The band ships as a write.** Both fields always travel together, so a `set_temperature` naming only one limit reads the other from accumulated state and sends both; if accumulated state holds no value for the other limit yet, the write is refused rather than guessed. `supported_features` declares `TARGET_TEMPERATURE` and `TARGET_TEMPERATURE_RANGE` together and does not change at runtime; which pair of attributes is non-None follows the mode. The read-back is the device's own push, as it is for every other WAVE 3 control: the per-mode list of the full upload carries both limits (`constant_temp_upper_limit_c`, `constant_temp_lower_limit_c`), so the lower limit is confirmed within 120 s at the latest, against roughly 150 ms for the acknowledgement. The acknowledgement is transport, not read-back, and a duplicate acknowledgement for one sequence number is on record and must be tolerated; ADR-020 already widened that gate for logging only, so no de-duplication is added. The explicit `ConfigRead` for fields 158 and 159 is **not** implemented: its shape is proven for two other field numbers and guessed for these, and it would be a second read path for a fact the push already carries, which is the thing decision 1 forbids.
7. **One vocabulary, no per-mode filtering.** `fan_modes` and `preset_modes` are fixed lists. A setpoint outside COOL and HEAT, a humidity outside DRY, a fan speed in constant_temp and a submode outside COOL and HEAT are refused by the existing rules and surface as rejections. Shrinking a list as the mode changes would give the climate entity a different vocabulary from the select that shares its table.
8. **The integration never issues a write the user did not ask for.** Clearing a preset is not expressed as a rewrite of the current setpoint, even though the device is observed to leave a preset on any write: that sends a temperature nobody asked to change and depends on a side effect no message documents. For the same reason a partial OFF to mode transition, which is two sequential frames (`power` true, then the mode), is not rolled back; the device stays on in its previous mode and the next push corrects what the card shows.

**Trade-offs:**
- (+) One control table, one refusal function, one builder, one coordinator method; the second surface cannot drift from the first because it owns nothing
- (+) The thermostat card, `climate.set_temperature` and scheduler integrations work without a parallel key set
- (+) The band write rests on a captured frame with a worked example, not on a proto reading
- (+) No entity is retired, so the cards built on beta.2 keep working four days after the last registry change
- (-) Two preset labels and several mode-dependent options are offered and refused, and a thermostat card renders presets more prominently than a select does
- (-) The lower band limit is confirmed by the next full upload rather than by the acknowledgement, so the card can show the previous value for up to 120 s
- (-) Fourteen controls and one climate entity address the same device, which is more surface to document than either alone
- (-) A unit that is powered on and has reached its setpoint reads as Off on the card, because the device reports one bit for both

**Alternatives considered:**
1. Replace the four thermostat-shaped controls with the climate entity. Rejected: screen brightness, mood light and pet care have no climate slot, so it leaves a half set; it is a second registry retirement in the cycle whose first one is already visible on the maintainer's install; and it removes controls that scripts and cards can address individually.
2. A separate preset vocabulary for the climate entity, omitting the two labels that are refused. Rejected: two names for one fact, and the device's reported submode would fall outside the offered list after every write.
3. Derive `hvac_action` in constant_temp from ambient temperature against the band, or from input power. Rejected: invention. Nothing on the wire states which side of the band the unit is working.
4. An explicit `ConfigRead` for the band as the read-back. Rejected twice over: the request shape is guessed for these two field numbers, and the push already carries both limits, so it would be a second read path for one fact.
5. Filter `fan_modes` and `preset_modes` by the current mode so no offered option can be refused. Rejected: it gives the climate entity a different vocabulary from the select that shares its table, and the option list would change shape underneath the card as the mode changes.
6. Ship the band read-only, with `TARGET_TEMPERATURE_RANGE` declared but `set_temperature` rejecting in HEAT_COOL. Rejected: this was the correct answer only while the frame was unproven, and the capture proves it.

### Addendum 2026-09-10: what shipped beside the facade, and the wait a bundled gesture makes

Three things are on `main` that the decision does not say, all shipped in v1.20.0.

**The band is a second entry point, not a second path.** Decision 1 counts "one `write_refusal()`, one builder, one coordinator method". The implementation gave the band its own refusal function, its own builder wrapper and its own coordinator method (`band_write_refusal`, `build_band_write`, `async_send_wave3_band`), because the band is one frame carrying two limits and the single-field set cannot express it. Both end in the same shared ConfigWrite builder and both rules live in `ecoflow/wave3_commands.py`. The constraint decision 1 imposes, that the entity owns no state and no rule, holds: the climate entity still reads every value from coordinator data and delegates every refusal. Read decision 1 as: one control table, one shared builder, two coordinator entry points, no rule outside the core library.

**A bundled mode switch waits for the device before it sends the setpoint (beta.5).** Home Assistant lets one `set_temperature` call carry a mode and a setpoint. Measured on hardware on 2026-09-07: the two writes went out 13 ms apart, the device acknowledged both and applied only the first. It applies the mode switch, reports its own stored values for the new mode 1.0 to 2.6 s later, and a setpoint arriving inside that window is dropped; the same setpoint sent on its own, with the mode settled, is applied, which separates a device race from a defect in the frame. So a bundled call now sends the mode, waits for the device's own report of it (`MODE_SETTLE_TIMEOUT_S = 6.0`, polled every `MODE_SETTLE_POLL_S = 0.25` s, in `_await_mode`), and only then sends the setpoint. When the wait expires the setpoint is sent anyway, since a refusal would be worse than a write the device may still take. This is a wait, not a rollback, and decision 8 stands: nothing is written that the user did not ask for.

**In that gesture the refusal is judged against the target mode.** Decision 1 says refusals are evaluated "against accumulated state". For the setpoint half of a bundled call the entity passes the target mode to `async_send_wave3_set`, so `write_refusal` judges the setpoint against the mode the user just chose rather than the mode the device still reports during the settle window. Without that, the rule that accepts a setpoint in cooling and heating only would refuse a bundled COOL-plus-setpoint call from a unit still reporting FAN mode, before the mode switch had been reported. The rule is still the core library's; only the mode it is asked about comes from the gesture.

---

## ADR-022: A mixin declares the state it borrows by inheriting one declaration-only base under `TYPE_CHECKING`; ruff runs the full set minus line length

**Status:** Accepted (decided 2026-09-07; implemented 2026-09-08, PR #365 as 7b74925; decision 7 amended the same day by PR #366, #367 and #374, see the addendum)
**Date:** 2026-09-07
**Depends on:** the inventory measured before this decision (ruff 0.15.18, mypy 2.1.0, `mypy custom_components/ecoflow_energy --ignore-missing-imports` -> 728 errors in 26 of 62 files)

**Context:** `EcoFlowDeviceCoordinator` (`coordinator/core.py:101`) is assembled from eight mixins plus `DataUpdateCoordinator[dict[str, Any]]`, and the state they all read is assigned in one place, `EcoFlowDeviceCoordinator.__init__`. The config flow has the same shape: `EcoFlowEnergyConfigFlow(SetupFlowMixin, ReauthFlowMixin, ReconfigureFlowMixin, ConfigFlow)` and `EcoFlowOptionsFlow(OptionsFlowMixin, OptionsFlow)`. Each mixin is a plain class with no base, so every `self.<x>` inside one is unresolvable to a checker even though it resolves at runtime through the composed MRO. That single fact is 621 of the 728 errors, all of the literal shape `"<X>Mixin" has no attribute "<a>"`, spread over thirteen classes and about 136 distinct (class, attribute) pairs.

The state is not that mypy has no opinion about these attributes. It is that mypy already forms one, from whichever assignment inside a sibling's method body it happens to see first, and that opinion is wrong. `setup.py:148-150` assigns `self._iot_api = None` before assigning the client, so `SetupMixin` is taken to define `_iot_api` as `None`, and `core.py:175` - the real assignment, correctly annotated `IoTApiClient | None` - is then reported as incompatible with its own base class. Ten of the sixteen errors in `core.py` are that inversion, and the thirty-six `has-type` errors ("Cannot determine type of `_consecutive_http_failures`") are the same cause seen from the other side. So the choice is not whether the borrowed state gets declared. It is whether it gets declared deliberately in one place, or guessed from the first assignment in whichever file mypy reaches first.

Three groups fall out of the 621, and they do not have the same answer:

- **(A) Home Assistant framework API** the final class inherits: `hass`, `data`, `config_entry`, `update_interval`, `async_request_refresh`, `async_set_updated_data`, `async_update_listeners`, and for the flows `async_show_form`, `async_create_entry`, `async_abort`, `_get_reauth_entry`, `_get_reconfigure_entry`, `_async_abort_entries_match`. For the four flow mixins this group is *all* of their `attr-defined` errors: 46 errors across `config_flow_options.py`, `config_flow_reauth.py`, `config_flow_reconfigure.py` and `config_flow_setup.py` and not one name of our own among them.
- **(B) coordinator instance state** assigned in `EcoFlowDeviceCoordinator.__init__`: `_device_data`, `_mqtt_client`, `_http_client`, `_entry`, `_shutdown`, `device_sn`, `device_type`, the eleven `_powerocean_soc_*` fields, and the rest.
- **(C) methods owned by a sibling mixin**: `_apply_data` and `_integrate_energy` and `_resolve_soc` and `_derive_battery_state` and `latch_schedule_armed` in `state_apply.py`, `_enforce_monotonic` and `_on_mqtt_message` in `mqtt_ingest.py`, the five `_schedule_*` across `keepalive.py`, `availability.py` and `credentials.py`, plus the core-owned `_log_event`, `set_device_value`, `record_unknown_proto_fields` and the read-only properties.

The binding constraint is the calendar. The 1.20.0 beta cycle is open and the fourteen WAVE 3 controls plus the climate entity of ADR-020 and ADR-021 are mid-verification on real hardware. Any shape that changes what runs is disqualified before its merits are weighed.

**Decision:**

1. **Each mixin inherits a declaration-only base, bound under `if TYPE_CHECKING:` and bound to `object` at runtime.** One idiom, thirteen files:

   ```python
   if TYPE_CHECKING:
       from ._typing import CoordinatorState as _Base
   else:
       _Base = object


   class SetCommandsMixin(_Base):
   ```

   At runtime `_Base is object`, and `class Foo(object)` and `class Foo` produce the identical MRO, so `EcoFlowDeviceCoordinator.__mro__` is unchanged by this ADR. That claim is pinned by a test, not asserted: `tests/test_coordinator_mro.py` compares the tuple of `__mro__` names against a literal list, so a later attempt to make the base real turns CI red at the point of the attempt.

2. **Group (A) is solved by typing `self` against the Home Assistant base class, never by re-declaring its API.** `CoordinatorState` subclasses `DataUpdateCoordinator[dict[str, Any]]`, so `hass`, `data`, `update_interval`, `async_request_refresh`, `async_set_updated_data` and `async_update_listeners` come from Home Assistant's own annotations. The four flow mixins need no declaration file at all - their `_Base` is the framework class itself:

   ```python
   if TYPE_CHECKING:
       from homeassistant.config_entries import ConfigFlow as _Base
   else:
       _Base = object


   class SetupFlowMixin(_Base):
   ```

   with `OptionsFlow` for `OptionsFlowMixin`. Re-declaring `async_show_form` or `async_create_entry` in our tree would be a copy of somebody else's signature, and the failure mode of a stale copy is the bad direction: a signature change in a Home Assistant upgrade would be masked by our declaration instead of reported by the gate that exists to report it. Forty-six errors close with five lines per file and nothing of HA's API written down twice.

3. **Groups (B) and (C) share one mechanism, `coordinator/_typing.py`, because mypy checks them against the same declaration from opposite ends.** An attribute declaration is checked against the assignment in `core.__init__` and a mismatch is an `[assignment]` error at the assignment. A method declaration is checked against the sibling's real `def`, which becomes an override of it, and a mismatch is an `[override]` error at the definition. Both land on the line that drifted. Splitting the two into separate mechanisms would buy nothing and would ask a reader to know which kind a name is before knowing where to look it up.

   Two constraints on that file, both load-bearing:

   - **It declares no `__init__` and no method body.** Every method ends in `...`. `core.py:164` calls `super().__init__(hass, _LOGGER, config_entry=entry, name=..., update_interval=...)`, and with no `__init__` anywhere in the mixin chain that call resolves to `DataUpdateCoordinator.__init__` under mypy exactly as it does at runtime.
   - **A name belongs in `_typing.py` if and only if the mixin reads it and does not assign it.** State a mixin owns stays in that mixin's own class body as a plain annotation. `OptionsFlowMixin._all_devices` (the `has-type` at `config_flow_options.py:114`) is the mixin's own, and is declared where it lives.

4. **The exact shape, `SetCommandsMixin` worked through.** This is the file with the most reads - 196 errors, 24 borrowed names - and Phase 3 copies this rather than improvising it.

   `custom_components/ecoflow_energy/coordinator/_typing.py`, new file:

   ```python
   """Type-check-only declarations of the state the coordinator mixins borrow.

   Never imported at runtime: every mixin imports `CoordinatorState` inside an
   `if TYPE_CHECKING:` block and binds `object` in its place. Nothing here has a
   body and nothing here declares `__init__`, so the runtime MRO of
   `EcoFlowDeviceCoordinator` is exactly what it was before this file existed.

   A name belongs here when a mixin reads it and does not assign it. State a
   mixin owns is annotated in that mixin's own class body.
   """

   from __future__ import annotations

   import asyncio
   from typing import TYPE_CHECKING, Any

   from homeassistant.config_entries import ConfigEntry
   from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

   if TYPE_CHECKING:
       from ..ecoflow.cloud_http import EcoFlowHTTPQuota
       from ..ecoflow.cloud_mqtt import EcoFlowMQTTClient
       from ..ecoflow.iot_api import IoTApiClient


   class CoordinatorState(DataUpdateCoordinator[dict[str, Any]]):
       """Declaration-only view of `EcoFlowDeviceCoordinator`.

       Group (A) - `hass`, `data`, `config_entry`, `update_interval`,
       `async_request_refresh`, `async_set_updated_data`,
       `async_update_listeners` - is inherited from the base above and is
       deliberately absent below.
       """

       # -- group (B): assigned in EcoFlowDeviceCoordinator.__init__ --
       device_sn: str
       device_type: str
       device_name: str
       _entry: ConfigEntry
       _auth_method: str
       _enhanced_mode: bool
       _shutdown: bool
       _device_data: dict[str, Any]
       _mqtt_client: EcoFlowMQTTClient | None
       _http_client: EcoFlowHTTPQuota | None
       _iot_api: IoTApiClient | None
       _config_writes_sent: dict[int, float]
       _device_config_lock: asyncio.Lock
       _powerocean_soc_pending: tuple[int, int] | None
       _powerocean_soc_pending_revision: int
       _powerocean_soc_debounce_unsub: asyncio.TimerHandle | None
       _powerocean_soc_flush_tasks: set[asyncio.Task[None]]
       _powerocean_soc_write_tasks: set[asyncio.Future[Any]]
       _powerocean_soc_before: dict[str, Any]
       _powerocean_soc_generation: int
       _powerocean_soc_request_revision: int
       _powerocean_soc_confirmed_revision: int
       _powerocean_soc_active_revisions: set[int]
       _powerocean_soc_latest_outcome: bool | None
       _powerocean_soc_cycle_open: bool
       _powerocean_soc_rollback_generation: int

       # -- group (C): implemented by core.py or by a sibling mixin --
       @property
       def device_tag(self) -> str: ...

       @property
       def device_data(self) -> dict[str, Any]: ...

       def set_device_value(self, key: str, value: Any) -> None: ...

       def _log_event(self, event_type: str, detail: str) -> None: ...

       def latch_schedule_armed(self, state_key: str, armed: bool) -> None: ...

       def clear_schedule_armed_latch(self, state_key: str) -> None: ...
   ```

   Every type above is read from the assignment or the definition, not chosen: `_device_config_lock` from `core.py` (`asyncio.Lock()`, the one unannotated assignment in that block), `_powerocean_soc_write_tasks` as `set[asyncio.Future[Any]]` rather than `set[asyncio.Task[None]]` because it holds executor futures, `device_tag` and `device_data` as properties because that is what `core.py:437` and `core.py:447` are, `latch_schedule_armed` and `clear_schedule_armed_latch` from `state_apply.py:170` and `:177`. Two of the declarations also close errors that are not `attr-defined`: `_powerocean_soc_pending` as `tuple[int, int] | None` closes `set_commands.py:163`, and `_powerocean_soc_latest_outcome` as `bool | None` closes `set_commands.py:343`.

   `custom_components/ecoflow_energy/coordinator/set_commands.py`, the diff is the import block and the class line:

   ```python
   from __future__ import annotations

   import asyncio
   import json
   import logging
   import time
   from functools import partial
   from typing import TYPE_CHECKING, Any

   ...

   if TYPE_CHECKING:
       from ._typing import CoordinatorState as _Base
   else:
       _Base = object


   class SetCommandsMixin(_Base):
       """Mixin providing SET command dispatch and SoC debounce."""
   ```

   Nothing else in the file changes. The full `CoordinatorState` body is the union of the eight coordinator mixins' rows in that inventory, about sixty names; the block above is the twenty-four `SetCommandsMixin` needs, in the order the inventory lists them.

5. **Anti-drift: mypy itself for three of the four directions, one AST test for the fourth.** Stated plainly, because the fourth is the dangerous one:

   | Drift | Caught by | Where it is reported |
   |---|---|---|
   | A mixin reads a name nobody declares | mypy `[attr-defined]` | the read |
   | A declaration disagrees with `core.__init__` | mypy `[assignment]` | the assignment |
   | A declared method disagrees with its real `def` | mypy `[override]` | the definition |
   | **A declaration outlives its implementation** | **nothing** | - |

   The fourth turns the gate from "catches a missing attribute" into "vouches for a missing attribute": every read of the removed name type-checks and raises `AttributeError` at runtime. It is closed by `tests/test_coordinator_state_declarations.py`, which parses `coordinator/_typing.py` with `ast`, collects every annotated name and every `def` in `CoordinatorState`, parses the eight coordinator modules, and asserts each declared name is either assigned as `self.<name> = ` or defined as a `def` or `@property` in exactly one of them. Pure `ast` on both sides, no Home Assistant import, no coordinator construction, so it runs in milliseconds and cannot be broken by a fixture.

   The test ships with both controls run and recorded: it must fail when a name is deleted from `core.__init__` while its declaration stands, and fail when a declaration is added for a name nothing implements. And it counts what it compared, not the files it visited - `assert checked >= 60` on the number of declarations actually resolved, so an `ast` parse that silently yields an empty class body cannot pass as a clean run.

6. **The protobuf call: exclude the generated module from the crawl *and* skip it on import.** Fifteen of the sixteen findings in `ecoflow/proto/runtime.py` are `Module has no attribute "<Msg>"` on `pb2.JTS1EnergyStreamReport` and its siblings, read out of `ecocharge_pb2.py`, which is generated and regenerated only with the pinned `grpcio-tools==1.73.1`. The sixteenth, the `index` error at `runtime.py:565`, is a genuine finding and stays visible.

   ```toml
   [tool.mypy]
   exclude = "ecocharge_pb2\\.py$"

   [[tool.mypy.overrides]]
   module = "ecoflow_energy.ecoflow.proto.ecocharge_pb2"
   follow_imports = "skip"
   ignore_errors = true
   ```

   Both halves are load-bearing and each was measured alone before the pair was measured together. `follow_imports = "skip"` is what gives `pb2` the type `Any` at the fifteen use sites - `ignore_errors` scoped to the generated file cannot touch them, because they are reported *in* `runtime.py`. But `follow_imports` is ignored for any file mypy has in its own source set, and the generated file is crawled out of the directory we pass on the command line, so without `exclude` the override does nothing at all: measured 16 findings before, 16 after. With both, 16 to 1, and the whole tree 728 to 713.

   **The module path in a per-module section is `ecoflow_energy.*`, never `custom_components.ecoflow_energy.*`.** `custom_components/` carries no `__init__.py`, so that is the module name mypy computes, and a section naming the longer path matches nothing and reports no error for the mismatch. Two of the experiments behind this decision returned an unchanged count for exactly that reason and read as "the switch does not work" until the same override was run under the shorter name as a positive control and moved the count. Every per-module override this repository adds is checked by making it change a number, once, before it is trusted.

   Rejected on measurement: **`disable_error_code = ["attr-defined"]` scoped to `runtime.py`.** It also closes the fifteen (16 to 1, same survivor), and it costs `attr-defined` for the other five hundred lines of that module. The pair above buys the same result while leaving the check live everywhere it is our own code being read.

   Rejected, for now: **`.pyi` stubs.** `protoc` can emit `_pb2.pyi` and it would give real types for every message, which is strictly more checking than `Any`. It is rejected for this plan and not forever, because the stub has to be regenerated in lockstep with the pinned toolchain, and a stub that drifts vouches for a message class that no longer exists - the exact hole decision 5 spends a test closing, reintroduced in the one corner where nobody would notice. It becomes the right answer once the regeneration is a script rather than a remembered command.

   Rejected: **sixteen `# type: ignore[attr-defined]` lines.** One cause, sixteen sites, and a seventeenth message added to the dispatch table needs a seventeenth line that nobody will think of.

7. **The ruff selection.**

   ```toml
   [tool.ruff]
   target-version = "py313"
   extend-exclude = ["custom_components/ecoflow_energy/ecoflow/proto/ecocharge_pb2.py"]

   [tool.ruff.lint]
   select = ["E", "F", "W", "I", "UP", "B", "SIM", "RET", "C4"]
   ignore = ["E501"]
   ```

   No `per-file-ignores`. The one exclusion is the generated `ecocharge_pb2.py`, for both tools, because its findings are not fixable by hand and a hand fix is lost at the next regeneration.

   - **`E501` stays out** (699 findings) and `ruff format` with it (113 of 144 files rewritten): both are pure reflow, and a diff that touches most of the tree while the WAVE 3 controls are being read on hardware buries the changes reporters are looking at. Declared non-goals of this change.
   - **`E402`, three findings, no per-path exception.** Two are `delta_http.py:19` and `:21`, imports sitting below `_LOGGER = logging.getLogger(__name__)` with nothing between them and the top but blank lines - placement, not a guard, so they move up. Where an import genuinely must follow a guard, the exception goes on the line as `# noqa: E402` with the reason beside it, not into a table that nobody reads when moving the import later.
   - **`F841` in tests, thirteen findings, declined as a blanket exception.** Ruff marks its own F841 fix unsafe because it can delete a call kept for its side effect, so these are thirteen read-and-decide edits. That is the right price for a rule set that has already found a real hole in this tree: `F821 Undefined name "Any"` at `tests/ha/test_coordinator.py:7874` and `:8104`, two dict literals annotated against a name the module never imports. It has never raised because a local variable annotation is not evaluated at runtime, which is precisely why nobody found it.
   - The measured total is 179 findings, 139 auto-fixable, against `custom_components/` and `tests/`. Excluding the generated module can only lower it; Phase 3 re-measures once the config file exists and reports the number it actually saw.
   - **`ruff check --fix` must never be run unattended over this tree, and never with `F401` on `const.py`.** Measured 2026-09-08 during the implementation: the unused-import fix removed four names that module re-exports (`DEVICE_TYPE_UNKNOWN`, `device_log_tag`, `get_device_name`, `get_device_type`), and the suite came apart with three collection errors on `cannot import name 'DEVICE_TYPE_UNKNOWN'`. Three of the four have importers that resolve to this module rather than to `ecoflow/const.py`; a relative `..const` inside `ecoflow/parsers/` lands somewhere else entirely, so counting importers needs the package each import resolves in, not the string. They carry `# noqa: F401` per name with a block comment saying why. Run the rule separately and off that file.

8. **Configuration lands in a root `pyproject.toml` carrying only `[tool.ruff]` and `[tool.mypy]`, with no `[project]` table**, so nothing treats the repository as an installable package and hassfest and HACS validation see no change. The repository has no `pyproject.toml`, `ruff.toml`, `mypy.ini` or `setup.cfg` today; `pytest.ini` stays where it is.

9. **Each gate becomes blocking in the same pull request that brings its count to zero, never earlier and never in a soft mode.** A gate that reports without failing is a gate nobody reads, and this plan exists because two tools have been reporting into nobody's terminal. Both gates get their positive control before they are trusted: a deliberately broken file turns CI red once, on the record, so a green result afterwards means the check ran rather than that the check is broken.

**Measured, 2026-09-07, before this ADR was accepted.** The mechanism in decision 1 was prototyped on `SetCommandsMixin` - the `_typing.py` of decision 4 exactly as written, the four-line idiom, nothing else - and the tree restored afterwards:

| | |
|---|---|
| `set_commands.py` before | **196** errors |
| `set_commands.py` with the idiom | **2** errors |
| the 2 survivors | `add_done_callback` variance at `:245` and `:393` - genuine findings, not mechanism failures |
| whole tree | 728 to 536 from one file's four-line change |
| `SetCommandsMixin.__mro__` at runtime, with the idiom in place | `(SetCommandsMixin, object)` |
| `EcoFlowDeviceCoordinator.__mro__` at runtime, with the idiom in place | `EcoFlowDeviceCoordinator, SetupMixin, CredentialsMixin, KeepaliveMixin, MqttIngestMixin, StateApplyMixin, SetCommandsMixin, HttpPollMixin, AvailabilityMixin, DataUpdateCoordinator, BaseDataUpdateCoordinatorProtocol, Protocol, Generic, object` - `CoordinatorState` absent, unchanged |
| tree after restore | 728 errors in 26 files, 62 checked - the number it started at |

The MRO row is the behaviour-neutrality claim, taken from the interpreter rather than from the argument about `class Foo(object)`. The 196-to-2 row is the reason the ADR names one mechanism instead of weighing three: the borrowed-state problem is the whole of it, and what is left over is ordinary work.

**Trade-offs:**
- (+) Provably behaviour-neutral: `if TYPE_CHECKING:` never executes, `_Base is object` at runtime, `class Foo(object)` has the MRO of `class Foo`, and a test pins the MRO tuple so a later change of mind is loud
- (+) One authoritative declaration per name instead of up to twelve copies (`hass` is read by twelve of the thirteen mixins, `device_tag` by nine, `_log_event` by eight)
- (+) Home Assistant's API is never copied into our tree, so an upgrade that changes a signature is reported rather than masked
- (+) Groups (B) and (C) are checked in both directions by the type checker itself, at the line that drifted
- (+) The declaration file doubles as the one place to read what the composed coordinator offers, which the mixin split currently hides
- (-) The type-check-time MRO differs from the runtime MRO by one class. That is the whole trick and it is a thing a reader has to know; the module docstring says it in the first paragraph
- (-) Thirteen files gain five lines of idiom that carry no runtime meaning, which is the recognised cost of the mixin composition, now paid explicitly instead of by mypy guessing
- (-) One hole stays open to the type checker (a declaration outliving its implementation) and is closed by a test that has to be maintained alongside the declarations
- (-) `ecocharge_pb2` becomes `Any`, so a message class renamed by a regeneration is not caught until it raises. Named, accepted, with `.pyi` stubs as the stated follow-up
- (-) About forty ruff findings are not auto-fixable and need reading, and thirteen of them are in tests where the mechanical fix is the unsafe one

**Alternatives considered:**
1. **(i) A per-mixin `if TYPE_CHECKING:` attribute block in each of the thirteen files.** Rejected: `hass` written twelve times, `device_tag` nine, `_log_event` eight, and mypy reports a drift between two copies against the *final class* at `core.py:101` ("Cannot determine type of `_snapshot` in base class `StateApplyMixin`") rather than at the file that drifted - the four `misc` errors already sitting on that line are exactly this shape. It also has no honest answer for group (A) short of copying Home Assistant signatures into our tree, or doing (ii)'s work thirteen times over.
2. **(iii) A real typed base class every mixin inherits.** Rejected: it changes `EcoFlowDeviceCoordinator.__mro__` during a beta cycle whose controls are mid-verification on hardware, and it buys nothing (ii) does not. If it were ever taken the MRO becomes `EcoFlowDeviceCoordinator, SetupMixin, CredentialsMixin, KeepaliveMixin, MqttIngestMixin, StateApplyMixin, SetCommandsMixin, HttpPollMixin, AvailabilityMixin, CoordinatorState, DataUpdateCoordinator, ..., object` - one class inserted between `AvailabilityMixin` and `DataUpdateCoordinator`, which moves where `super()` lands for every method that base defines. Under (ii) that is the MRO mypy computes and nothing else does.
3. **A `Protocol` referenced from each mixin rather than inherited.** Rejected: a Protocol is structural, so referencing it puts nothing on `self`; using it would mean annotating `self: CoordinatorProtocol` on more than two hundred method signatures.
4. **`_Base = EcoFlowDeviceCoordinator` under `TYPE_CHECKING`.** Rejected: the final class inherits the mixins, so this is cyclic inheritance and mypy cannot resolve it. The runtime import cycle would be absent, which is what makes it look plausible.
5. **`disable_error_code = ["attr-defined"]`, or `# type: ignore` at the 621 sites.** Rejected: it switches off the one check that would catch a name removed from `core.__init__`, which is the failure this whole exercise is buying protection against.
6. **Collapse the mixins back into one class.** Rejected: a six-thousand-line file and a rewrite of the subsystem currently under hardware verification.
7. **Nothing at all for anti-drift, on the grounds that a stale declaration is inert.** Rejected: it is not inert, it is the one direction that makes the gate lie.
8. **`E501` and `ruff format` in the same pass.** Rejected: 699 findings and 113 rewritten files of pure reflow, landing on top of a release cycle people are reading.

**Consequences:** a new `coordinator/_typing.py` that nothing imports at runtime; thirteen mixin files gain the four-line `_Base` idiom and lose nothing else; `OptionsFlowMixin` gains one own-state annotation for `_all_devices`; a root `pyproject.toml` with `[tool.ruff]` and `[tool.mypy]` and no `[project]`; two new tests, the AST declaration check with both controls recorded and the MRO pin; `.github/workflows/tests.yml` gains the two gates, each blocking from the pull request that brings its count to zero. The 107 non-mixin mypy errors are not pre-decided here: the thirty-six `has-type` and most of the twenty-six `assignment` errors fall out with the declarations, and the remainder (`cloud_mqtt.py` six, `parsers` four, `decoder.py` three, `diagnostics.py` three, and the singles) are ordinary type errors fixed one at a time, none of them silenced by an ignore without a line beside it naming why. Watch for the failure this design cannot prevent: a mixin added later without the `_Base` idiom re-opens forty errors in one file at once - which the gate catches on that mixin's first pull request, and which is the point of having one.

### Addendum 2026-09-10: line length is a gate, the formatter is checked, the target is the oldest runtime, and four numbers moved

**Decision 7 as it stands on `main`.** The deferral of `E501` and `ruff format` held for one day. PR #366 ran the formatter over the integration and the tests, PR #367 enforced the line length and rewrapped what the formatter leaves alone, both merged 2026-09-08 before v1.20.0. The configuration now reads `line-length = 88`, `ignore = []`, and the Lint job runs `ruff format --check` beside `ruff check`. The check exists for a measured reason: within hours of the reflow landing, three files arrived unformatted on a pull request and nothing said so. The title of this decision, "minus line length", describes the shape at decision time; the full set, line length included, is what gates the repository since PR #367. Rejected alternative 8 was rejected for timing, and the timing changed.

**`target-version` is `py312`, the oldest runtime the integration ships to, not the CI interpreter.** PR #374 set it, with the reason in the file: the setting tells the formatter which syntax it may emit, and a target of 3.14 lets it write unparenthesized `except A, B:` clauses that parse on no older Python, while the HACS minimum of Home Assistant 2025.1.0 runs on 3.12 and 3.13. The type checker's `python_version` is `"3.14"`, the interpreter it runs on, which is a different question with a different answer.

**Four numbers moved on implementation.** The declaration test's floor is `MIN_DECLARATIONS_CHECKED = 75`, not 60. The per-module override for the generated protobuf module lists both module names, `ecoflow_energy.ecoflow.proto.ecocharge_pb2` and `custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2`, deliberately: mypy computes the short name when pointed at `custom_components/` and the long name when it reaches the same file through a test that imports it by that path, and an override naming only one is silently inactive for the other run. So decision 6's "never `custom_components.ecoflow_energy.*`" reads today as "both, because the tests reach the same file under the longer name"; the positive-control discipline it describes is unchanged. The idiom sits in twelve mixin files (eight coordinator mixins, four flow mixins), not thirteen; thirteen counted the final coordinator class in the error inventory. And the pins are `ruff==0.16.6` and `mypy==2.3.1`, with `disallow_untyped_defs = true` on for the integration and an `empty-body` override for the declaration module; the file is the source for the current strictness.

---

## ADR-023: A payload the device XOR-masked is unmasked, masked by the same six passes, and masked back; the key is the header's own `seq`, the raw pass keeps running over the whole frame, and there is no second level

**Status:** Accepted (decided 2026-09-08; shipped in v1.20.0)
**Date:** 2026-09-08
**Depends on:** ADR-016 (the six passes and the corpus sweep that measures every change to them); ADR-017 (the same blind spot on the text path, which stays there); `_inner_payload` in a local analysis script, which has applied this unmask since 2026-08-24

**Context:** `sanitize_frame` runs six passes over the raw bytes of a frame. A header that sets `enc_type == 1` describes a payload XOR-masked with `seq & 0xFF`, and every string inside it is ciphertext to a pattern looking for ASCII, so all six are blind there by construction. The key is not a secret: `seq` is field 14 of the same header, in the clear, in the same frame. The mask is an encoding and anybody holding the frame holds the key.

Measured 2026-09-08 over 1922 frames (the local capture corpus and the tracked fixtures under `tests/fixtures/`), by locating each region, XOR-ing it with its own header's `seq & 0xFF`, and running the module's own patterns over the plaintext. 234 headers set the flag; 224 regions are the header's own `pdata` (field 1), 0 are the frame payload (field 2), 10 have neither and hold nothing. 40 regions in 40 frames carry something a pass would mask: 36 `_SERIAL_RUN` and 4 `_TIME_ZONE`, with `_UUID_RUN`, `_JOINED_HEX_RUN` and `_mask_delimited_identifiers` at zero. The serials are 4 distinct values in two reporter diagnostics downloads - two C376 wallbox serials, one HJ31 PowerOcean serial, and one 16-character run of `Z` that somebody had masked by hand, which is itself the tell that the product could not reach it. The time zones are 5 `Europe/<city>` occurrences in four files, and three of those files are **tracked public fixtures** (`delta3/p231_status_frame.json`, `stream/bk01_capture_masked.json`, `smart_meter/bk21_frames_issue331.json`): three owners' regions are in the repository now, one XOR away from plain text, in fixtures the fixture gate certifies as clean. `_TIME_ZONE` exists precisely so a region cannot narrow a device to a country, and it has been running past these since it was added.

Three structural numbers decide the shape. 535 frames carry more than one header and **260 carry different `seq` values across them**, so a frame-wide key is wrong by measurement rather than by argument. The existing raw pass alters **0 of 224** regions today, so the two passes are disjoint and no double-masking has to be arbitrated. **0 of 224** unmasked regions contain a payload that itself declares `enc_type == 1`, so there is no second level on file. Two headers set the flag and name no `seq`, and none has `seq & 0xFF == 0`. Separately, 3,492 headers carry a `pdata` with `enc_type != 1` - the same protobuf in the clear, already receiving all six passes.

**Decision:**

1. **The region is found by a second byte walk in `frame_capture.py`; `decode_header_message` does not change.** The decoder is on the ingest path, every parser unpacks its two-tuple positionally, and widening that contract for a diagnostics-only consumer makes the hot path pay for the capture path. `frame_capture.py` already owns a walk of exactly this kind for exactly this reason: `_mask_delimited_identifiers` walks rather than parses because a frame is at its most interesting when it does not parse, and a walk degrades to "found no regions" where a parser raises. The walk reuses the decoder's `_read_varint` - varint semantics keep one owner - and is wrapped so no malformed frame can raise, as `decode_cmd_headers` already is.
2. **One key per region, taken from that region's own header.** Not a frame-wide key, and not because of a frame on file - no `enc_type == 1` header appears in a multi-header frame today (0 of 234). Because 260 frames already prove headers disagree about `seq`, and because the failure mode of a wrong key is not a missing mask but a **clean-looking report**: wrong plaintext matches no pattern, so the region certifies itself. It is free to build correctly now and invisible on the day it is not.
3. **`seq & 0xFF == 0` is masked in place; a header with no `seq` is skipped.** A zero key is the identity, so the region is already plaintext and the same code path handles it with `key = 0` - not a special case. A missing key is unknown, and unmasking with a guess would produce garbage that, masked and written back, would **corrupt a region that survives intact today**. Skipping keeps exactly today's behaviour for that frame. The skip is counted rather than silent: the walk returns the region with `key=None` and the sweep reports it.
4. **The six passes keep running over the whole frame; the region pass is additive, and it reads from the original bytes.** The frame that does not parse decides this: where the walk finds nothing, the raw pass has already done its job, and a design in which the region *replaces* the raw pass in its span would make an unparseable frame worse than today. Overlap needs no arbitration - 0 of 224 regions are touched by the raw pass, because ciphertext does not look like a serial - but the code is written against it anyway: the region is sliced from the **original** payload, never from the raw pass's output, since unmasking a run of `X` with the key yields garbage and would leave the region holding neither the original nor a clean mask. A region the pass does not change is not spliced at all, so the change is strictly additive.
5. **One level. Recursion is recorded, not added.** No unmasked region on file contains a frame declaring `enc_type == 1`, and the criterion is that measurement: `is_proto_frame(inner)` plus a header with the flag. Recursion would need a termination argument and a depth cap on the capture path under the caller's lock, for a case with no instance - the pattern of ADR-016 decision 3. The sweep counts nesting candidates so the first one is a number rather than a silence.
6. **All six passes run in the region, from one extracted helper, in the same order.** The body of `sanitize_frame` becomes `_plain_passes(payload, secrets)` and both the whole frame and each region call it, so the rules exist once. `_TIME_ZONE` is 4 of the 40 hits and three of the affected files are public, so no serials-only subset is defensible. `_mask_delimited_identifiers` reads protobuf, which is what a region is, and fires zero times there today - a negative control, not a risk. The symmetry argument closes it: 3,492 plaintext `pdata` regions already get all six, so a subset would mask a value or not depending on whether its device set a flag.
7. **Both the corpus sweep and the fixture gate learn to look under the mask.** The sweep is blocking: it is the instrument every mask change is measured with (ADR-016 decision 5), and blind it reports `0/0` before and `0/0` after, so it can neither show this fix nor catch its regression. It gains regions found, regions skipped, route taken, hits per pass, and nesting candidates. The gate is cheap and its charter demands it: an XOR whose key travels in the same frame does not make an identifier absent, and applying the gate's own run pattern to the unmasked regions of the tracked tree yields **0 failures over 164 blobs and 67 encrypted headers**. Leaving it blind would invert ADR-016 decision 4 - the product would look where the gate cannot, and the gate would certify what it never read. Stated plainly: the gate is not what found this and will not find it after the change either, since a time zone is outside its vocabulary; the three public fixtures come clean because the product starts masking there. The gate's extension buys the next case.
8. **ADR-016 and ADR-017 each gain one sentence naming the scope they measured**, since both are correct and both read wider than they are: every number in them was taken over payloads the device sent in the clear. The sentences are appended on implementation.

**Trade-offs:**
- (+) The four serials and five time zones on file are masked at the source, and three tracked public fixtures stop carrying an owner's region
- (+) The mask survives: the region is XOR-ed back, so a masked frame still decodes exactly as the device sent it and every analyzer keeps working
- (+) Length, header numbers and parser readings are invariant by construction - every pass preserves length and the splice writes at the same offsets
- (+) The sanitizer and the local analyzer finally agree about what a frame is, which matters because the analyzer runs on the sanitized download
- (-) `sanitize_frame` gains a walk over every frame on the capture path; bounded by frame length and run under the caller's lock, and measured at 15,590 region bytes across the whole corpus
- (-) A region whose header names no `seq` stays unread; 2 on file, and the alternative corrupts it
- (-) Three tracked fixtures change bytes, so their parser tests are re-confirmed rather than assumed
- (-) The local corpus is not in CI, so the numbers above are re-run by hand on every mask change, as ADR-016 already requires

**Alternatives considered:**
1. Extend `decode_header_message` to return byte offsets. Rejected: it is the ingest path's function, its two-tuple is unpacked positionally by every caller, and a frame that fails to parse would lose the masking a walk still provides.
2. Let the region replace the raw pass inside its span. Rejected: it makes the frame that does not parse strictly worse than today, which is the one constraint that cannot move.
3. Slice the region out of the raw pass's output rather than the original. Rejected: a raw-pass mask inside a region unmasks to garbage; 0 occurrences today, and the failure is silent and unrecoverable when it happens.
4. A frame-wide key from the first header. Rejected: 260 frames on file already carry headers that disagree about `seq`, and a wrong key reports clean.
5. Skip a region whose key is zero. Rejected: a zero key is the identity, the region is plaintext, and skipping it would be the same blind spot with a different cause.
6. Guess a key for a header with no `seq`. Rejected: it corrupts a region that currently survives intact.
7. Serials only in the region. Rejected: 4 of the 40 hits are time zones and three of those files are public.
8. Recurse into nested masked payloads. Rejected for now: 0 instances, and it needs a termination argument on the capture path. Recorded as the shape to add with the frame that shows it.
9. Leave the fixture gate blind because it adds no failure today. Rejected: it would make the gate weaker than the product exactly where the product just learned to look, which is the inversion ADR-016 decision 4 exists to prevent.

**Consequences:** `frame_capture.py` gains `_plain_passes` (today's body, unmoved), `_EncRegion`, `_encrypted_regions`, `_xor`, and a four-line loop in `sanitize_frame`; the docstring names the mask, the key and where the key lives. `tests/test_frame_capture.py` gains `TestEncryptedRegionMasking` with twenty cases - including the mutation control that the raw pass alone misses the value, the two-headers-two-keys case that fails on a frame-wide key, the original-versus-output slice case, and the truncated frame that must come out no worse - and `TestMaskingDoesNotCorruptRealFrames` gains an under-the-mask sweep with a floor on regions inspected, so a broken walk cannot pass as a clean run. The corpus sweep imports `_encrypted_regions` rather than copying it. Three tracked fixtures are regenerated through the product's own output, never by hand. Watch for the thing this design cannot prevent: a device that masks with something other than `seq & 0xFF`, which would decode to garbage, match nothing, and report clean - the sweep's per-pass counts going to zero on a family that used to have hits is the only signal, and it is a number somebody has to read.

*Addendum 2026-09-09 (ADR-025), corrected 2026-09-10:* the six passes became seven. The anchored-string pass runs second in `_plain_passes`, right after the named secrets and before every free-running pass (ADR-025 amendment), so every string beside a serial is masked under the XOR mask by the same route; the last pass is still the length-delimited one, and nothing else in this decision moves.

---

### Addendum 2026-09-11: a masked region survives a second pass unchanged

The raw pass runs over the whole frame first, and on the wire a masked region reads as `X ^ key`, which for many keys is itself a letter or digit (`0x1e` gives `F`, `0x0a` gives `R`). A masked serial can therefore spell a sixteen-character run on the wire, and the raw pass rewrote it as if it were a serial, which under the mask is no longer `X`. The region pass did not repair it, because the plaintext it read was already clean. Measured on 2026-09-11 when the PowerPulse 2 settings-report fixture, masked by this very function, went through it a second time: two of nine frames came back corrupted. Now, where the plaintext under a rewritten byte is already the mask byte, the byte goes back to its ciphertext; every other rewrite stays, so a header that declares a mask it does not carry is still handled as before. The three properties above are unchanged. Two tests pin it, one with the fixture and one with a synthetic region whose plaintext is not the mask byte.

## ADR-024: The protobuf command registry gets one table per device type; the device type is the namespace and is passed in, not guessed

**Status:** Accepted (decided 2026-09-08; implemented and merged the same day as a7af66d, PR #380; shipped in v1.21.0-beta.1, v1.21.0 not yet released)
**Date:** 2026-09-08
**Satisfies:** ADR-008, condition 1. That condition is binding before the PowerPulse 2 (`C376`) can become its own device type.

**Context:** `_build_cmd_registry()` in `ecoflow/proto/runtime.py:65` returns one flat `dict[tuple[int, int], CmdConfig]`, cached in the module global `_CMD_REGISTRY` (`:217`) and read at three places in that same file: `_typed_runtime_map` (`:424-436`), the per-header loop of `decode_proto_runtime_headers` (`:535-538`) and the legacy last-resort path (`:617-620`). Fifteen tuples are registered, twelve of them PowerOcean and three Delta 3.

The collision ADR-008 names is prospective, not present. Neither `241/44` nor `96/34` is registered today, so no capture on file parses wrongly and nothing is currently mis-decoded. What exists is a shape: a wallbox parser adding a tuple that a PowerOcean entry already owns, or the reverse, produces a silent wrong decode with no test failing. `(241, 3)` shows how close the tree already is, since the PowerOcean's accessory relay owns it and `241` is the family the wallbox reports on when it speaks for itself.

The separation that prevents this already exists, twice, in places nothing can check. The module docstring (`runtime.py:1-13`) states that the registry is device-type agnostic and that callers must route by device type before consuming a result. `coordinator/mqtt_ingest.py:583-612` is that routing: Stream, Stream AC 5000, Smart Meter, Solar Tracker and WAVE 3 each go to their own parser before any lookup, and the comment above `(32, 50)` (`runtime.py:203-208`) records that the Stream family means a different message by that tuple and is saved only by this ordering. A contract carried by a docstring and an if-chain is a contract that holds until somebody adds a call site.

ADR-008 names `stream_proto.py` and `stream_ac5000_proto.py` as the model. Read literally they are a different mechanism: each holds a private `(cmd_func, cmd_id)` map (`stream_ac5000_proto.py:94`, consumed at `:972`) and never touches `runtime.py`. Copying that for the wallbox would mean a private table in a new module, no change here, and the existing hazard left standing.

**Decision:**

1. **The registry is `dict[device_type, dict[tuple[int, int], CmdConfig]]`, and the namespace key is the `DEVICE_TYPE_*` constant `get_device_type()` already produces** from `_SN_PREFIX_MAP` (`ecoflow/const.py:83`, resolved at `:385-388`). No family enum, no family constant, no set. The device type exists, exactly one value applies per device, and it is already the value the caller routes on.

2. **The device type reaches the lookup as a required keyword argument** on `decode_proto_runtime_headers` and `decode_proto_runtime_frame`. There is no default. A default meaning "search every table" is the global registry under another name and would be reachable from any caller that forgets, which is the silent path this ADR removes. Both production call sites (`mqtt_ingest.py:614` and `:686`) have `self.device_type` in scope, so nothing is threaded.

3. **Every family carries a full table. There is no common table with per-family overrides.** Two families that genuinely mean the same message by the same tuple share the `CmdConfig` by reference, as a module-level constant listed explicitly in each table, so reading one family's table still tells you everything that family decodes. No such constant is introduced now; none is shared today.

4. **An unregistered tuple is still skipped without a log line**, unchanged, because that is the "the device says more than the parser reads" contract the `_SN_PREFIX_MAP` comments rest on. **An unknown device type resolves to an empty table** and is skipped the same way, with one debug line per process: the production caller runs on the Paho thread inside a broad `except Exception` that would turn a raise into a lost frame. A bad family is caught at build time instead, by a test asserting every outer key is a device type `get_device_type()` can return.

5. **The alarm is a paired control**, not an assertion about the tables. One representative frame per family is decoded twice: under its own device type it must produce its known `parse_path`, and under a foreign device type it must produce nothing. If the tables are ever merged, flattened or given a union default, the second half starts succeeding and fails loudly.

6. **The change is behaviour-neutral and that is its acceptance criterion.** Every capture parses to the same key set as on `main`, per frame, with a floor on frames compared so a broken replay cannot pass as a clean run.

**Trade-offs:**
- (+) The collision becomes structurally impossible rather than untested: two tables cannot share a key
- (+) The routing contract moves from a docstring and an if-chain into the signature, where the type checker and every test see it
- (+) One mechanism for one distinction: a new family gets a table, not a private map plus a new dispatch line plus a comment explaining why it bypasses the registry
- (+) Each test call site has to name the device it decodes for, which those tests do not currently state and should
- (-) About sixty test call sites across ten files change in one pass, and the pass proves nothing by itself
- (-) Two families that share many tuples list them twice; the duplication is visible, where a shared default would not be
- (-) The registry is one lookup deeper, and a family whose table is missing goes quiet rather than loud at runtime

**Alternatives considered:**
1. Key on `cmd_func` alone - rejected: it looks like a family and is not one. `241` is the PowerOcean's accessory relay and would be the wallbox's own channel, so `241/44` still collides. `32` is shared between the Delta 3 generation and the Stream family, `254` between the Delta 3 generation, the Stream AC Pro, the Smart Meter and the WAVE 3. This moves the collision one level up and calls it solved.
2. An explicit family enum - rejected: each member maps one-to-one onto a device type, which is a second name for one distinction. It drifts when a new prefix gets a device type and no family member, and it produces a family *set* the moment two device types share a table, reuniting what it just split - the failure on record from the ES21 work.
3. Key on the serial prefix - rejected: ten prefixes map to `DEVICE_TYPE_POWEROCEAN`, so it needs a set per family on day one.
4. `device_type: str | None = None` with `None` meaning the union of all tables - rejected: it keeps the exact defect. A caller that forgets gets the global table back, silently, and every test written against the default exercises a namespace no production caller uses.
5. A context object carrying the device type - rejected: over-engineering for one field, and a third name for the device type.
6. A common table plus per-family overrides - rejected: the common table *is* the global registry. Every PowerOcean-only tuple lands in it, a wallbox frame with the same tuple still hits it unless somebody remembers an override, and the collision survives with a precedence rule on top.
7. Follow the cited precedent literally and give the wallbox a private map in its own parser, leaving `runtime.py` alone - rejected: it satisfies nothing. Condition 1 asks for the registry to be split, and this leaves PowerOcean and Delta 3 sharing one table reached by the fallback branch, with a second mechanism now established for the same distinction.

**Consequences:** `runtime.py` gains `_registry_for(device_type)` and loses the global-table lookup at three sites; its docstring stops warning callers to route by device type and states that the argument enforces it. `mqtt_ingest.py` passes `self.device_type` at both call sites. Ten test files change, and `tests/ha/test_coordinator.py` patches the symbol by name at `:5085` and `:5280`, so its replacement must accept the new keyword.

One behaviour is allowed to change and is bounded rather than assumed. The fallback branch at `mqtt_ingest.py:614` is reached by every device type without its own parser, and the `_is_energy_stream` through `_is_bp_heartbeat` branches that follow it (`:633-660`) carry no device type guard, so a non-PowerOcean frame carrying a PowerOcean tuple is mapped as PowerOcean today. Afterwards it decodes to nothing under its own family and falls to `_parse_proto_device_data`. No capture on file exhibits that, so this is theoretical; if the replay finds one, staying neutral means giving that device type a table with exactly those tuples, decided up front rather than discovered in review.

Watch for the thing this design cannot prevent: a family table that is simply wrong. The namespace guarantees that a PowerOcean tuple is never read as a wallbox tuple; it guarantees nothing about whether the `CmdConfig` inside a family names the right message, and a wrong `msg_class` still decodes to whatever the payload happens to fit. That question stays with the capture evidence for each tuple, exactly where it was.

---

## ADR-025: A string beside a serial in the same protobuf message is masked by that neighbourhood; the serial is the anchor, the walk descends six levels over the whole payload, and the fixture gate walks with the product's own helper

**Status:** Accepted (decided 2026-09-09; implemented the same day in PR #382 with the amendment below; shipped in v1.21.0-beta.1, v1.21.0 not yet released)
**Date:** 2026-09-09
**Depends on:** ADR-016 (the serial as the boundary, `_MASK_BYTE` inside `[A-Z0-9]`, a widening needs a frame that shows it); ADR-023 (`_plain_passes` as the one place every pass lives, `_read_varint` as the one owner of varint semantics, the fixture gate importing the product's own walker); the length-delimited pass (`_mask_delimited_identifiers` and its floor of 12)

**Context:** A PowerPulse 2 (C376) diagnostics download carries, in message `2/133`, a repeated record whose two entries on file each hold an eight-character upper-case hex id (`A1B2C3D4`, `B2C3D4E5`) and a name (`Ecoflow_1234`, `Ecoflow_1235`) beside the device serial. The serial is masked; the other two fields are not. Byte layout of one entry (a PowerPulse 2 diagnostics download on file, frame 11 of a skipped device's capture, header `enc_type` absent, so plaintext), as read on 2026-09-09:

```
0a 2c                      field 1, length 44, one record
   08 01                   1: varint 1
   12 08 "A1B2C3D4"        2: eight upper-case hex characters
   1a 10 <serial, 16>      3: the device serial, already 16 x X after _SERIAL_RUN
   2a 0c "Ecoflow_1234"    5: a name, lower case and an underscore
```

The record sits three delimited levels below the frame: frame, header submessage (field 1), `pdata` (header field 1), record list (field 1, repeated), record. Measured 2026-09-09 over 20,324 hex blobs (the local capture corpus and the tracked fixtures): 9 frames carry the record (6 in that download, 3 in an earlier one), 18 values in all, nowhere else in the corpus, not on the text path, not under an XOR mask.

Every existing pass is blind by construction. `_SERIAL_RUN` needs 15 characters (`frame_capture.py:35`). `_mask_delimited_identifiers` tests a whole delimited field, which is exactly the shape here, and requires `_IDENT_MIN = 12` to `_IDENT_MAX = 32` bytes all in `_IDENT_ALPHABET`, upper case and digits (`:55-56`, `:139`, `:164-167`): the hex id is 8 bytes, and the name carries lower case and an underscore. The fixture gate's `_RUN`, `[0-9A-Za-z]{12,}` (`tests/test_fixture_identifiers.py:41`), fails for the same two reasons, the underscore splitting the name into 7 + 4.

Three candidates, measured 2026-09-09 over the same 20,324 blobs:

| Candidate | Hits on the 18 values | Other whole delimited fields it masks |
|---|---:|---|
| A. A whole delimited field of exactly 8 `[0-9A-F]` | 9 of 18 (the hex ids) | 0 |
| B. `_IDENT_ALPHABET` widened to `[A-Za-z0-9_-]`, floor 12 kept | 9 of 18 (the names) | `plug_and_play` (9, a key name in the same device's frames); the placeholder `XXXXXXXXXXXX-XXXXXXXXXXXXXXXX` in a tracked fixture |
| C. Inside any message that carries a whole delimited field matching `[A-Z0-9]{15,}`, every other whole delimited field of 8 or more bytes all in `[A-Za-z0-9_-]` | 18 of 18 | 0 |

The other whole delimited fields that share a message with a serial anywhere on file, same measurement: `ios` (2168), `V0.0.0` (522), `Europe/Berlin` (37), `android` (21), `V1.0.1`, `pd`, `p6`, `653`, and fields that are not printable. Every one is outside C by length (`android` is 7 characters), by a dot, or by a slash. C was measured with a strict protobuf parse of each delimited span, recursive, to depth 6.

**Decision:**

1. **Candidate C, alone: the anchored-string pass.** Within one protobuf message, meaning the fields read from one delimited span, if any whole delimited field fullmatches `_SERIAL_RUN` (`[A-Z0-9]{15,}`, reused rather than declared a second time), every other whole delimited field that fullmatches `[A-Za-z0-9_-]{8,}` is replaced by `X` of equal length. An anchor is never a candidate. A candidate has no upper bound, for the reason ADR-016's amendment gives: a cap does not decline to mask, it masks the tail and leaves the front standing. The rule is ADR-016's own - the boundary is the serial, not the width - and C is the only candidate that reaches a name by its position rather than its spelling. A name an owner typed has no spelling to catch.
2. **Not A and not B, neither alone nor as a belt.** A reaches the 9 hex ids and no name, and on every frame on file its hits are C's hits, so it has no positive control of its own - the situation ADR-016 decision 3 and ADR-023 decision 5 decline to build for. It would also mask any 8-character upper hex whole field anywhere, a checksum or a build hash, with nothing beside it to say the field is an identifier. B costs `plug_and_play` nine times and a byte of a tracked fixture, and cannot reach a name under 12 characters. A + B together reach the 18 values on file and nothing an owner might type. Both are recorded as shapes, with their numbers, for the day a frame presents a bare 8-hex or a long lower-case identifier with no serial beside it.
3. **The walk descends into every delimited span as a message, to `_ANCHOR_DEPTH = 6`**, the payload it receives being depth 0 and the record depth 3. Six is the depth the collateral was measured at; a cap the measurement does not cover would be a guess in either direction. Termination has an argument: each descent is into a strictly shorter span, and the cap bounds the cost at six visits per byte. A span below the cap is not descended into; a record deeper than six is recorded as the widening to make with the frame that shows it, and the sweep reports the deepest level at which it saw an anchor so that frame is a number rather than a silence.
4. **The walk runs over the whole payload `_plain_passes` receives, from byte 0, not only over the spans the header walk identifies.** The pass is additive, so a payload the walk cannot read is masked exactly as today. Within a message the walk is tolerant the way `_header_fields` is (`frame_capture.py:240-280`): it reads fields until the first one it cannot read and keeps what it read, because a frame is at its most interesting when it does not parse (ADR-023 decision 1) and a truncated later header must not cost an intact earlier one its record. The safety is the anchor, not the parse: masking anything needs a serial-shaped whole field in the same message, and ordinary binary does not produce a `[A-Z0-9]{15,}` run (0 hits in 1556 frames, ADR-016). The measurement used a strict parse; a tolerant walk examines a superset of spans, so the sweep is re-run with the walk as implemented before the PR opens. If the tolerant walk finds collateral the strict one did not, the parse strictness is the knob and it goes back to the design, not into the review. The walk is wrapped so no malformed frame can raise, as `_encrypted_regions` is, and reuses the decoder's `_read_varint`.
5. **The pass is the seventh and last in `_plain_passes`, after `_mask_delimited_identifiers`.** Running inside `_plain_passes` puts it under the XOR mask for free (ADR-023 decision 6). Two couplings it depends on: the anchor matches a serial `_SERIAL_RUN` has already turned to `X` only because `_MASK_BYTE` is inside `[A-Z0-9]` (ADR-016 decision 2, amendment), and a 12-character identifier `_mask_delimited_identifiers` has already masked fullmatches the candidate shape and is re-masked `X` for `X`, which is idempotent and needs no arbitration.
6. **Floor 8, alphabet `[A-Za-z0-9_-]`.** `android` (7 characters, 21 occurrences beside a serial) is the reason the floor is 8 and not lower; `V0.0.0` and `Europe/Berlin` are the reason the dot and the slash stay out; the hyphen is in because names carry it. A floor of 4 would cost `android` and nothing else on file, and would reach a short typed name; it is recorded, not taken, because no frame on file shows such a name and a widening needs one (ADR-016).
7. **The fixture gate walks with the product's own helper.** `tests/test_fixture_identifiers.py` imports `_anchored_string_fields` the way it imports `_encrypted_regions` and `_xor` (`:22`), applies it to every frame and to every region it unmasks, and requires every span returned to be all `X`, with `_PLACEHOLDERS` (`:45`) still exempt. A free-running `[0-9A-F]{8}` in gate text is rejected: eight hex characters occur in ordinary binary, and the gate would go red on the tracked tree for the wrong reason. The gate's per-frame checks move into one function, `_leaks(raw) -> list[str]`, so a positive control can run the exact code the parametrized test runs; the gate has never been run against a known-bad frame, and this closes that. The gate stays green on the tracked tree by the measurement: C touches 0 fields there.
8. **Controls.** Positive: the two downloads (9 frames, 18 values, 0 surviving after the pass) and a hand-built record in the unit tests. Negative: the full corpus through the corpus sweep, which gains a per-pass count for the anchored pass and the deepest anchor level seen, with 0 other fields changed, 0 length changes, 0 header fields changed, 0 parser readings changed, numbers in the PR.

**Trade-offs:**
- (+) The 18 values on file are masked at the source, by position beside the serial, with 0 collateral in 20,324 blobs
- (+) A name an owner typed is reached whatever its spelling, which no shape pass can do
- (+) One rule, one pass, one walker shared by product, gate and sweep; no second constant for the serial shape
- (+) Length, offsets, header fields and readings are invariant by construction, as for every pass
- (-) `_plain_passes` gains a recursive walk over every frame and every region, bounded at six visits per byte, under the caller's lock
- (-) A name shorter than 8 characters, a name in a message without a serial, and a name whose serial sits in the parent message all pass; each is one measured change away and each waits for a frame
- (-) The tolerant walk is not the strict parse the numbers were taken with; the re-run before the PR is what makes the numbers apply
- (-) The local corpus is not in CI; the sweep is re-run by hand on every mask change, as ADR-016 already requires

**Alternatives considered:**
1. A alone. Rejected: 9 of 18, both names stay.
2. A + B. Rejected: `plug_and_play` nine times, a tracked fixture byte, and no reach to a name under 12 characters or outside the alphabet.
3. C + A as a belt. Rejected: A has no frame of its own on file, so it would be a pass without a positive control, and a second path to the same value (one value, one source).
4. Unbounded recursion. Rejected: a walk without a cap has no cost bound on the capture path; ADR-023 decision 5 asked for a termination argument and a cap before any recursion there.
5. Walk only the `pdata` spans the header walk identifies. Rejected: a frame the header walk cannot read comes out strictly worse than under a whole-payload walk, and `_plain_passes` receives an unmasked region that has no header of its own.
6. Strict parse per span, as measured. Not rejected as a rule, held as the knob: it is what the numbers were taken with, and it is the change to make if the tolerant walk shows collateral.
7. A free-running `[0-9A-F]{8}` in the gate. Rejected: fires on ordinary binary.
8. Floor 4. Rejected for now: costs `android` 21 times, and no frame on file needs it.
9. Lower `_IDENT_MIN` to 8 with the upper-case alphabet. Rejected: it still leaves the names, and an 8-byte upper-alphanumeric whole field is accepted at (36/256)^8, about 1.5e-7 per position against 4e-11 at 12, some four thousand times the rate `_mask_delimited_identifiers` was accepted at (`frame_capture.py:147-151`); that is the class of change that destroyed 25 of 25 frames once.

**Consequences:** `frame_capture.py` gains `_ANCHORED_STRING = re.compile(rb"[A-Za-z0-9_-]{8,}")`, `_ANCHOR_DEPTH = 6`, `_delimited_spans_by_message(payload)` (the walk: yields the whole delimited spans of each message it can read, to the cap, reusing `_read_varint`, never raising), `_anchored_string_fields(payload) -> list[tuple[int, int]]` (the rule: candidate spans in messages that carry an anchor), `_mask_anchored_strings(payload) -> bytes`, and one line at the end of `_plain_passes`; the `_plain_passes` and `sanitize_frame` docstrings count seven passes. `tests/test_frame_capture.py` gains `TestAnchoredStringMasking` with the cases listed for it, one case in `TestEncryptedRegionMasking`, and a floor test in `TestMaskingDoesNotCorruptRealFrames` that counts anchored messages visited, not frames. `tests/test_fixture_identifiers.py` gains `_leaks`, the anchored check inside it, and a positive and a negative control. The corpus sweep counts the pass and the deepest anchor level. ADR-023's "six passes" becomes seven on implementation, one sentence appended there. Watch for the thing this design cannot see: a device that puts the serial in a parent message and the name in a child, or a name in a message with no serial at all. The pass is blind there by design, and so is the gate, until a frame shows it.

### Amendment 2026-09-09 (review of the implementation): the pass runs second, not seventh

Decision 5 placed the anchored pass last. The review measured a record `{1: 1, 3: <serial>, 8: "Ecoflow_1234"}` through `sanitize_frame` and the name survived, while the same record with the name at field 5 (the C376 layout) was masked. Cause: `_SERIAL_RUN` is free-running and greedy, and when the byte after a serial is itself in `[A-Z0-9]` it is swallowed; for a length-delimited field that byte is the tag, and fields 6, 8, 9, 10 and 11 carry the tags `2`, `B`, `J`, `R`, `Z`. The masked tag reads as a varint and the walk loses the rest of the message. Nothing on file is affected (18 of 18 still reached), so this was a silent gap for the next device rather than a regression.

The pass now runs right after the named secrets and before every free-running pass. The anchor accepts a real serial and a run of `X` alike, every pass is `X` for `X` idempotent, and the sweep after the move reports the same numbers as before it: the four values reached, 0 other fields, 0 length, header or reading changes. Pinned by `test_a_name_after_the_serial_with_an_alphanumeric_tag_is_masked`, which is red with the pass in its original position.

Two more things the review pinned that the design had left to prose: the alphabet boundary (`V1.0.1.2` and `Asia/Tokyo` beside a serial survive, red if `.` or `/` joins the alphabet) and `_ANCHOR_DEPTH == 6` itself, since the cap test moved with the constant. Recorded as a known limit, not closed: a field holding a whole sub-message whose every byte falls in the alphabet is a candidate and would be masked whole; 0 on file, and a guard would have to parse a value to tell a message from a name. The fixture gate now runs the anchored check under the XOR mask as well, with its own positive control, which decision 7 asked for and the first implementation omitted.

---

## ADR-026: The decision register is public and single; numbers are stable and gaps are stated; evidence lives in an internal annex; new decisions are written public first

**Status:** Accepted
**Date:** 2026-09-10

**Context:** Code comments and tests cite decisions by number. A hundred and eleven references of the form `ADR-NNN` sit under `custom_components/` and `tests/`, forty-nine of them in the integration itself, and the description of PR #380 cites the decision it implements. The register those references pointed at was never part of the repository: it lived beside the tree as a private file, so every citation resolved for the maintainer and for nobody else. A contributor reading PR #380 asked whether the decisions could be read.

Three forces shape the answer. First, most of what a decision records is exactly what a contributor needs: what was measured, what was chosen, what was rejected and why, what changed in the code. Second, some of the material a decision rests on cannot be public: downloads from owners who report by mail, raw device captures, and a few decisions about how the repository itself is worked on rather than about how the integration behaves. Third, a number that has been cited from code cannot move. Renumbering would break a hundred and eleven citations at once, and the numbers are the only thing tying a line of code to the measurement behind it.

**Decision:**

1. **One register.** `documentation/architecture/decisions.md` is the register of architecture decisions, newest last. There is no second copy. A decision is recorded there or it is not recorded.
2. **Numbers are stable and the gaps are stated.** Every decision keeps the number the code already cites. The register names in one sentence the numbers it does not carry, so a citation of such a number resolves to that sentence rather than to silence. Numbers not in this register: ADR-001 and ADR-003 record how the repository is worked on rather than how the integration behaves, and stay internal; ADR-003 was assigned twice and both records are internal. ADR-009 was reserved for the wallbox write decision when this entry was written; it was recorded on 2026-09-11 and stands after ADR-028.
3. **A public decision is complete for a reader who has this repository and nothing else.** Number, title, status, date and the section shape are the ones the record always had. Measurements, trade-offs, alternatives and the reasoning stay in full. Material the reader cannot open is described by what it is, not where it is: a device capture is "a capture on file" with its device family, mode and duration; a review is "the review of the implementation"; a local measurement script is what it measured. A reporter is named only by a GitHub handle in the context of a public issue. An owner who reports by mail is not named and not identifiable by device set, file name or date.
4. **Evidence lives in an internal annex.** The material a decision rests on and cannot show (captures, downloads, local scripts, the internal decisions themselves) is listed per decision in an annex that is not part of the repository. The annex is not a register: it decides nothing, it carries no decision heading beyond the internal numbers above, and it exists only so that the register's "on file" can be followed by the maintainer.
5. **New decisions are written public first.** A new decision is written into the register in the shape of item 3 from the start, and its evidence goes to the annex in the same step. Nothing is written privately and translated later.
6. **A test guards the boundary.** `tests/test_public_docs_boundary.py` walks `documentation/`, `README.md` and `CHANGELOG.md` for the classes item 3 excludes (terms that describe how protocol knowledge was obtained beyond observed traffic and the public app bundle, paths into the private part of the working tree, internal plan numbers, names of internal tooling, device serials that are not obvious dummies, em dashes), resolves every `ADR-NNN` cited in the code, the tests, the CHANGELOG and the documentation to a heading or to the sentence of item 2, and runs each of its patterns against a sample it must catch, so that an empty scan cannot pass as a clean one.

**Trade-offs:**
- (+) Every citation in the code resolves to text the reader can open, including the three numbers that resolve to a sentence rather than a decision
- (+) The reasoning behind a change is reviewable by the contributor who is asked to fit it, which is the point of recording it
- (+) One file to keep current instead of a private original and a public derivative that drift apart
- (+) The boundary is a test, not a habit: a leak in the register turns CI red
- (-) A public decision says "a capture on file" where the private record named the file; a reader outside the repository cannot follow that pointer, and must take the numbers on trust or ask
- (-) Three numbers are gaps forever, and every reader who meets them reads the same sentence
- (-) Writing public first costs care on every new decision, at the moment the decision is being made; the test catches the mechanical part, not a reporter identified by context

**Alternatives considered:**
1. A second, public copy beside the private register. Rejected: two files with one number series drift on the first amendment that lands in only one of them, and the private one would stay the one that gets written first.
2. Renumber, so the public series has no gaps. Rejected: a hundred and eleven citations in the code and the tests would break at once, and the numbers are the only thing tying a line of code to the measurement behind it.
3. Publish everything, including the decisions that stay internal. Rejected: two of them are not about the integration at all, and one records a source of protocol knowledge that the repository does not rely on and does not describe.
4. Keep the register private and remove the citations from the code. Rejected: the citations are the most useful comments in the tree, because each one points at the measurement behind a line that would otherwise look arbitrary. Removing them keeps the register consistent by making the code poorer.

**Consequences:** `documentation/architecture/decisions.md` is created with every public decision, and `documentation/architecture/README.md` says how to read it and what the gaps mean; `documentation/README.md` links both. The internal annex replaces the former private register and carries the internal decisions and the per-decision evidence lists; the private register is removed so that nothing can be written there by habit. `tests/test_public_docs_boundary.py` runs with the suite. Watch for the two things the test cannot see: an owner identified by the combination of device, date and account shape, and a measurement whose number is right but whose "on file" no longer has a file behind it. Both are caught only by reading.

---

## ADR-027: PowerOcean scheduled charge tasks are read and operated as the device reports them; creating and deleting stay in the app; the bounds are the app's rule, not a measured device limit

**Status:** Accepted (decided in public on #328 on 2026-08-30 and restated at the stable release on 2026-08-31; shipped in v1.18.0, first in the v1.18.0-beta.30 pre-release; three of the four entities withheld in v1.18.1 and restored with the bounds in v1.19.0, first in the v1.19.0-beta.2 pre-release; recorded here 2026-09-10; see the addendum of 2026-09-10 for the feed-to-grid family asked for on #381)
**Date:** 2026-08-30
**Depends on:** ADR-011 decision 2 (an explicit unknown is the contract between parser and entity). **Related:** ADR-024 (the list message sits in the PowerOcean's own table since PR #380)

**Context:** Owners on a dynamic tariff schedule grid charging into the cheap hours from the EcoFlow app, and Home Assistant had no view of it. @stuartglewis31 on #328 recorded a session with a timed log: creating a schedule, switching it off, changing its power, switching it back on, off again, deleting it. Every action is in the file with the device acknowledging it, and the power change shows as the number going from 1000 to 1500 at the minute he made it. The recording also covered what had been thought missing: five reads before the creation show no schedule at all, so it holds a schedule being created from nothing.

What the messages carry. The device sends its whole schedule list in one message, `(96, 10)`, and that message rides inside the get-all reply the integration already receives, twice under the same sequence number, with the two copies able to disagree; the first copy is the one read. The list arrives both when asked for and unasked, so a write can be confirmed from the device's own list rather than from an acknowledgement. The time window is carried as minutes since midnight packed into one number, 1200 and 1230 for 20:00 to 20:30; the 20:00 half is anchored twice, by the log and by the device's own running flag flipping at that minute. The repeat kind and its parameter are two separate fields whose meaning depends on the kind: weekly carries the chosen weekdays as a bitmask, a one-off carries its date. That was settled on the maintainer's own PowerOcean by predicting the number for two day sets and two time windows before reading them back, and the reporter's single sample fits the same rule. A target state of charge is not on this path: the message has no such field, which is what the app screen says from the other side.

What is not on record. Whether the device rejects, clamps or accepts a charge power beyond the app's range has never been observed; only the app's rule is known. The 20:30 end time was in the field, but the schedule was deleted before the window closed, so a run to the natural end is the one thing that capture left open.

**Decision:**

1. **Each schedule the device reports gets four entities:** a switch that arms and disarms it, a number for its charge power, a sensor with the time window written as `20:00-20:30`, and a binary sensor for whether it is charging right now, taken from the device's own running flag rather than inferred from the clock. Reading and operating an existing schedule is the supported shape.
2. **Creating and deleting a schedule stay in the app.** Without a create there is no safe way to undo a delete, so neither is exposed. Creating was first ruled out for a second reason, that it would have meant inventing a repeat pattern on hardware that charges from the grid; since the repeat fields were decoded on 2026-08-30 that reason is gone, and creation is not built, has no date, and is only no longer off the table.
3. **A write is confirmed from the schedule list the device sends back**, not from the acknowledgement, so a control shows what took rather than what was asked for. A schedule deleted in the app has its readings go to unknown, and a further write to it fails with a message saying why instead of doing nothing.
4. **The charge-power number enforces the app's own bounds and nothing more:** steps of 100 W, a floor of 100 W per online battery pack, and a ceiling per model taken from the serial prefix, 6000 W on the reporter's single-phase 6 kW unit. A value outside them is refused rather than rounded or sent. The bounds are the app's rule; the device's own limit is unobserved and is not claimed.
5. **The list is read from the reply the integration already receives.** No timer polls it.

**Trade-offs:**
- (+) The device is the source of truth for every state shown, and a write that did not take cannot look as if it did
- (+) Nothing is sent that the app would refuse to send, so the integration cannot put the hardware somewhere the app cannot
- (+) A schedule the owner created in the app is fully operable from Home Assistant on the first list that carries it
- (-) An owner cannot create a schedule from Home Assistant, so an automation that needs one must start from a schedule that already exists in the app
- (-) The ceiling is the app's; if the device would take more, that headroom is unreachable from here, and if it would take less, a value inside the app's range could still be refused by the device without the integration knowing
- (-) The end of the window rests on one field position anchored once; a run to a natural end would confirm it and none is on file

**Alternatives considered:**
1. Create and delete schedules through the integration. Rejected in decision 2: a delete without a create is a one-way door, and a create needs a repeat pattern the integration would have to compose; possible since the fields were decoded, not built, no date.
2. No bounds on the charge-power number, letting the device answer. Rejected: v1.18.1 withheld the switch, the number and the window for a day precisely because the evidence did not establish writable bounds, and a value the app would never send is not a value to try first on hardware that charges from the grid.
3. Poll the schedule list on a timer. Rejected in decision 5: the device sends the list asked and unasked, inside a reply already received.
4. A target state of charge per schedule. Not possible: the message has no such field.

**Consequences:** The schedule entities shipped in v1.18.0 and were confirmed on hardware other than the maintainer's on 2026-09-03 (#328). The optional check @stuartglewis31 offered on 2026-09-02, whether the device rejects a value above 6000 W, stays optional; nothing waits on it. #381 (opened 2026-09-09 by @paddy2k) asks for a schedule type that feeds power to the grid, with enable, disable and an export rate from Home Assistant: enabling, disabling and modifying a reported schedule is the shape decision 1 supports, and a schedule that has to be created is the case decision 2 leaves in the app. Whether the device carries a feed-to-grid kind on the same list, and what its fields are, is the open question this decision leaves and does not promise; the diagnostics download attached to #381, taken while such a schedule was created, enabled, disabled, modified and deleted, is the evidence to read first.

### Addendum 2026-09-10: the feed-to-grid schedule is a second family with the same shape, and the device pushes its list unasked

The download attached to #381 has been read. @paddy2k recorded, in one timed session on 2026-09-09, a feed-to-grid schedule being created, disabled, enabled, changed in power, changed in window, changed in repeat kind three ways and deleted, on a `J327` PowerOcean with two such schedules already on the unit. What it answers:

- **It is not on the same list.** The feed-to-grid schedule is written with `(96, 143)` and reported in `(96, 145)` on request and in `(96, 14)` unasked, a message that rides in the same bundle as the main status report. The grid-charge schedule of decision 1 is written with `(96, 125)` and reported in `(96, 10)`. The integration reads `(96, 10)` and nothing else of this kind, so a feed-to-grid schedule is invisible in Home Assistant today, on every installation, and nothing is broken on the reporter's unit.
- **It has the same shape.** Each entry carries an index, an enable flag, a kind (`2` on every feed-to-grid entry; the earlier #328 unit showed a separate list of kind `3` whose meaning is not measured), a power in watts, a device-computed running flag, and a sub-record with the repeat kind, its parameter and one or two time windows. The window, the weekday mask and the one-off date encode exactly as in decision 1's family, disable is the flag omitted rather than sent as false, and every write was acknowledged within the same second with the change visible in the next list, asked and unasked. Decision 3's read-back therefore exists for this family too, and decision 5's no-polling holds, because `(96, 14)` arrives without being asked.
- **What decision 1 would give it needs no new evidence:** a switch, a power number, a window sensor and a running binary sensor per reported feed-to-grid schedule, which are the reporter's three asks (enable, disable, export rate) exactly. Creating and deleting stay in the app under decision 2, unchanged.
- **What is still not on record:** whether two feed-to-grid schedules may overlap. The app's ceiling for the export power, which decision 4 would take as the bound, was answered by the reporter later the same day: 5000 W on his unit, the same value as his Feed Power Limit, which is the inverter's own feed-in limit and a reading the integration already takes. One unit's observation, so a bound taken from that reading is the obvious rule rather than a proven one; it means no per-model table is needed for this family. Neither point blocks reading the list.

This addendum records the answer, not a commitment: nothing is scheduled for it, and its scope is decided after the running release cycle. The full field-level reading is kept with the maintainer's working notes.

---

## ADR-028: The Ocean 2 under `RE11` and `RE17` is one device type; a field position inferred from a name is marked as such until a frame confirms it

**Status:** Accepted (decided in public on #145 on 2026-09-07; not implemented: as of 2026-09-10 neither prefix is in the prefix map and no Ocean 2 parser or entity list exists on `main`; the read path is expected from a contributor's pull request)
**Date:** 2026-09-07
**Depends on:** ADR-014 decision 1 (one type for two prefixes that send one message); ADR-011 decision 2

**Context:** #145 tracks Ocean 2 support. @jensfr1 is preparing the parser and supplied a field mapping with three battery-block corrections; the maintainer's check on 2026-09-07 found all three hold, and one is stronger than stated: the value read as capacity is remaining energy in watt hours, the one read as state of charge is the state of health, and the cell voltage is a maximum cell voltage that already arrives in volts, so both the quantity and the scale of that reading were wrong. The energy-stream block carries seven readings, not four: load, grid, solar and battery power, plus a solar-charger power, an inverter-side solar power and a battery state of charge, and the client keeps two of those blocks side by side, which is the likely reason for the two block numbers in the mapping.

Two prefixes are in play. The vendor's own device list carries `RE11` and `RE17` with different power ratings, 10 kW against 12 kW, and the public app bundle has no separate handling for `RE17` anywhere: same message families, same payload shape, same screens. An `RE17` owner (@Fvdzandt, from #343) has been pointed at the thread.

What is on file and what is not. A 16 h diagnostics download from an `RE11` has already shaped the tree twice: it holds nine bundles of 12 to 14 messages each, which is why the frame buffer is sized by what the Ocean 2 sends rather than by a fixed count, and an Ocean 2 get-all reply of 53 KiB in one frame forced the last raise of the capture limit (v1.17.0). What is not on file is a raw capture that pins the field numbers of the mapping: names and types are visible in the bundle, field numbers are not, and neither is whether the energy-stream block arrives once or twice per message; the maintainer stated on 2026-09-07 that no raw Ocean 2 capture usable for that is on his side. No `RE17` frame is on file at all. One value is known not to be a reading: the feed-in ceiling, which the app resolves per serial to bound a slider, so a sensor built on it would report the app's own setting back to the owner.

**Decision:**

1. **One device type for `RE11` and `RE17`.** Both prefixes map to it in the prefix map with one display name. A second type for `RE17` would change no parser, no entity and no name, which is the synonym test ADR-014 decision 1 applied to the tracker's two prefixes. The 12 kW rating is a rating, not a message.
2. **No keyword, no rating check.** The app path returns an empty product name for every device, so the prefix is the whole classification, and nothing in the parser or the entity list branches on which of the two it is.
3. **A field position inferred from a name is marked as inferred until a frame confirms it.** The ask on #145 is a few minutes of listen-only capture from either prefix, taken on v1.17.0 or later so it arrives whole. The ask does not block the pull request: the maintainer said the capture may arrive after the contributor's own work lands. Until it does, the mapping's positions are recorded as inferred in the parser, and the first frame is the check that turns each one into a measured fact or a correction.
4. **The first `RE17` frame is the check on decision 1.** The identity rests on the bundle for `RE17`, where ADR-014 had frames for both prefixes. If an `RE17` frame shows a different message family, that is the moment for a second parser table, not a second type; if it shows the same, nothing changes.
5. **The feed-in ceiling is not a sensor.**

**Trade-offs:**
- (+) One type, one parser, one name; an `RE17` owner gets the same entities on the day the read path lands
- (+) The two corrections of the wrong quantity and wrong scale go in before anything ships, not as a migration after
- (-) `RE17` is mapped on the bundle's evidence alone; decision 4 is the safeguard, and until an `RE17` frame arrives it is a stated risk
- (-) Decision 3 is looser than ADR-014, which had a frame for every shipped value before the parser merged; a position that is wrong ships as unknown or as the wrong reading until the first capture, and the marking is what keeps that visible

**Alternatives considered:**
1. Two device types by power rating. Rejected in decision 1.
2. Map `RE11` now and `RE17` when a frame shows it. Rejected: an `RE17` owner would be refused as unsupported by the same integration whose author has said in public the two are one read path, and the cost of being wrong is one parser table, caught by decision 4.
3. Block the read path until a frame is on file. Not chosen: the maintainer said on #145 that the capture may follow the contributor's work; decision 3 keeps the inference visible instead of hiding it behind a merged parser.
4. A feed-in ceiling sensor. Rejected in decision 5.

**Consequences:** The implementation lands with the contributor's pull request, and the register gets that PR and its release in this status line then. Nothing is owed to @jensfr1 before he is ready, as stated on #145 on 2026-08-23 and 2026-09-07. The one ask stands: a few minutes of listen-only capture from an `RE11` or `RE17`, on v1.17.0 or later, attached to #145. The unsupported-device notice for both prefixes stays until the read path lands.

## ADR-009: The PowerPulse 2 start and stop command leaves on the PowerOcean's set topic and is confirmed on the wallbox's own topic; the device's addressing decides the route, the entity's source decides the confirmation

**Status:** Accepted (decided 2026-09-11; implemented the same day, Docker-verified in Enhanced Mode; ships as v1.22.0-beta.1, the first hardware run on #7 decides the next pre-release or the stable release)
**Date:** 2026-09-11
**Depends on:** ADR-008 (the wallbox is its own device with its own coordinator and its own connection), ADR-024 (the wallbox owns its own message table). **Changes:** the paragraph of ADR-008 that kept writes out of scope; ADR-008 carries an addendum pointing here.

**Context:** This number was reserved when the wallbox was still an accessory of the PowerOcean, for exactly one question: whether a start or stop command may leave on the PowerOcean's set topic for a device that has its own. ADR-008 answered no for its time, "for a structural reason rather than caution": the app writes the wallbox through the PowerOcean's serial, the wallbox's own buffer holds no set message at all, and a control built on that "would put the command on one channel and its confirmation on the other, which is exactly the mixing this integration refuses". The paragraph ends with the fact that whether the wallbox accepts writes on its own topic is unevidenced. That last fact still holds. The rest has moved.

What is on file since. A recording of 2026-08-28 from the wallbox owner on #7 (@Xygen), taken while the app started and stopped a charging session with a vehicle attached, holds the app's command four times: one message of the wallbox's own message family, on the PowerOcean's set topic, addressed to the accessory by a bus address (215) and the wallbox serial, with one selector field, 1 for stop and 2 for start. The four frames are decoded byte for byte. A diagnostics download from the same owner of 2026-08-29 holds two acknowledgements to that same message carrying a plain sequence counter where the app puts its signature, each followed by the state change the command asked for: a client that is not the app is accepted on this route. The acknowledgement never carries a result field, in two of two cases here and in seven of seven for the settings message, so an acknowledgement proves delivery, not effect. The owner then published an integration of his own, on GitHub, that sends exactly this frame through its PowerOcean connection, requires exactly one PowerOcean in the account, takes the accessory's address from the wallbox's own reporting, and confirms only on a fresh wallbox heartbeat, 30 s for start and 15 s for stop; he reports a reversible start and stop test with a vehicle attached. The latencies observed on the relayed path after a write were 7 s from stop to the finishing state and 10 s from start to charging. No latency on the wallbox's own channel after a write is on file.

The address the command needs is on the wallbox's own topic. Its periodic settings report carries the accessory descriptor, the bus address 215 and the sixteen-byte serial, about once per second in a sixteen-minute recording on file; the app's commands carry those two fields and nothing else of it. The wallbox's own state comes from its heartbeat as `ev_charge_status`: 1 available, 3 charging, 6 finishing, and the parser withholds every session-scoped value while the plug status is 1.

A wallbox without a PowerOcean in the account (a capture on file, 2026-09-07, account sign-in) shows none of that message family apart from one report type; the public app bundle's path for that case is a different message family on the wallbox's own topic, and it appears in no recording.

The integration since ADR-008 has one coordinator and one connection per device serial, all in the same config entry. A connection publishes to its own serial's set topic and to nothing else; the acknowledgements come back on that serial's reply topic, which the same connection subscribes to and logs.

**The sentence in ADR-008, read in its context.** It refused a control in the accessory era, when the confirmation would have come from the PowerOcean's relay of the wallbox's state while the values had just been decided to come from the wallbox's own topic: two sources for one status, with neither the descriptor nor a non-app write on the route evidenced. The rule the integration actually keeps is threefold: two modes are never mixed, one value has one source, and no control exists without read-back from the device. Two topics on one connection family are not a second mode, not a second source and not a missing read-back. They are routing, and the device defines the route: its descriptor names a bus address behind the PowerOcean, and the PowerOcean is the only endpoint observed accepting the command.

**Decision:**

1. **The criterion.** A command goes where the device accepts it; a confirmation comes from where the device reports; both on one transport, one account and one mode; and the confirmation source is the value the entity already shows. For the wallbox that means the command leaves on the PowerOcean's set topic and the confirmation is read from the wallbox's own heartbeat. That is the shape ADR-008's sentence described, and it is accepted here because every term of the threefold rule holds.

2. **The wallbox coordinator publishes through its sibling.** The wallbox coordinator resolves the PowerOcean coordinator of the same config entry at the moment of the press and calls that coordinator's own protobuf publish path, the one every PowerOcean control uses. It does not touch the sibling's connection object, and it does not gain a publish path of its own to another serial: the acknowledgement lands on the PowerOcean's reply topic, which only the PowerOcean's connection subscribes to and logs, and a connection that could publish to any serial would open the same door to every future caller. The PowerOcean's configuration lock is not taken; it guards read-modify-write of PowerOcean settings, which this frame does not touch. The wallbox coordinator holds its own lock around the check and the publish.

3. **Exactly one PowerOcean, or no control.** The descriptor names the bus address and the wallbox serial, not the parent. With zero PowerOcean coordinators in the entry there is no route; with two or more there is no way to tell which one carries the wallbox, and a command sent to the wrong one is acknowledged and does nothing. In both cases the buttons are not created and one debug line says why. The count is checked when the platform sets up and again at the press.

4. **Confirmation is the wallbox's own heartbeat, in the arriving frame, inside a window.** A press publishes once and then waits for a frame on the wallbox's own topic whose `ev_charge_status` is the state the action asked for: `charging` for start, `finishing` or `available` for stop. The check runs on the frame being applied, never on the stored value, so a stale reading cannot confirm anything. The window is 15 s for stop and 30 s for start: both above twice the relayed latency on file (7 s stop, 10 s start), and the values the owner's own integration tested live, so the first hardware run compares like for like. When the window passes without that frame the press raises with a message that says so. No retry, no optimistic state, no seeded value; the sensor keeps showing what the device says. A second press while one is pending is refused with zero publishes.

5. **Start is offered on `finishing` only; stop on `charging` only.** The only start transition on file is from finishing to charging: the owner's stop from Home Assistant left the wallbox in finishing, his start restored charging. Plug status 1, `available`, is the state on which the parser withholds every session-scoped value, so the parser itself reads it as no session in progress; whether a cable is attached in that state is readable from no field on file. A start with no vehicle would be an acknowledgement, no state change, a thirty-second wait and an error, every time. If the hardware run shows a state in which the wallbox reports available with a cable attached, offering start there is one entry in a set.

6. **Two buttons on the wallbox device, account sign-in only, created once the descriptor has been reported.** "Wallbox Start Charging" and "Wallbox Stop Charging" are created for a wallbox coordinator when the entry holds exactly one PowerOcean, in the way the PowerOcean's accessory controls already appear: if the descriptor has not been reported yet, the buttons are added on the first frame that carries it. Their availability is structural: the wallbox coordinator's own availability and the sibling's connection being up. The state precondition is not part of availability: an unavailable button says nothing about why, and a status that changes with every heartbeat would flap the entity in the logbook and in every automation watching it. A press in the wrong state raises with the state the wallbox is in.

7. **The descriptor is echoed, never assumed.** The bus address and the serial in the command are the values the wallbox reported, not constants. A settings report without the address marker yields no descriptor, and without a descriptor there is no button.

8. **Nothing for a wallbox without a PowerOcean.** The write path on the wallbox's own topic is unevidenced; ADR-008 stands there unchanged. The release note states the condition. A recording of the app starting and stopping such a wallbox is the one thing that can open that path.

**Trade-offs:**
- (+) The owner who asked on #7 gets the two controls the app has, on the device that is the wallbox in Home Assistant, confirmed by the same value the sensor shows
- (+) Nothing new is opened in the core: the sibling's existing publish path, the existing accessory-control pattern, one lock, one pending record
- (+) An acknowledgement that proves only delivery cannot mislead, because delivery is not what the press reports; the state is
- (-) Two coordinators of one entry now depend on each other for one action; the dependency is resolved at the press and refused cleanly when the sibling is absent or down, and it is the only such dependency in the tree
- (-) The windows rest on latencies measured on the relayed path, not on the wallbox's own channel after a write; the first hardware run records the direct latency, and the windows are widened from data, never narrowed without it
- (-) Households with two PowerOceans and a wallbox get no control until an owner with that setup appears

**Alternatives considered:**
1. A publish path on the wallbox coordinator's own connection addressed to the PowerOcean's serial. Rejected in decision 2: the acknowledgement would land on a topic that connection never subscribes to, the per-device event log would lose it, the invariant that a connection publishes only to its own serial would be gone for every future caller, and the acceptance of a non-app client on file was observed from a connection subscribed to the PowerOcean's topics.
2. The buttons on the PowerOcean device, with the PowerOcean coordinator owning the action. Rejected: ADR-008 made the wallbox its own device and its state lives on the wallbox coordinator; the PowerOcean would then read another coordinator's state to confirm, which is the same coupling in the worse direction.
3. Choosing the parent among two PowerOceans by the one that relays the wallbox's state. Rejected in decision 3: that relay was removed from the parser when the wallbox became its own device, and it appears only while a session runs, which is exactly when the buttons are not being created.
4. Offering start on `available` as well. Rejected in decision 5.
5. The state precondition inside availability. Rejected in decision 6.
6. Copying the PowerOcean's debounced pending record for the state of charge. Rejected: that record coalesces a value write so the last value wins; an action must not coalesce, a start followed by a stop is two commands. What is reused is its claim-and-clear shape, not the debounce.
7. Holding the wallbox lock for the whole wait so a second press queues behind the first. Rejected: the tree has no queue of writes for any control, and a clear refusal is better than a stop that fires thirty seconds later.

**Consequences:** A new button platform, two entities on the wallbox device, one core builder for the command with the message family as a parameter of the existing envelope so that every PowerOcean write stays byte-identical, two descriptor keys from the settings report with no entity of their own, a pending record resolved in the wallbox coordinator's state-apply step, and five error messages. The tests reproduce the four recorded frames byte for byte, prove that no button exists without exactly one PowerOcean or in the developer-key mode, that a press publishes exactly once on the sibling's connection, resolves on the confirming frame and raises after the window, and that every precondition refuses with zero publishes. The first release is a pre-release, and the hardware run on #7 is the first place the route shows from this integration; its result decides the next pre-release or the stable one. The addendum of ADR-008 points here.
