# Design Principles

The fit test every pull request must pass. A change that works but crosses one of these lines is not merged, and the maintainer does not adapt it afterwards. Read this before you write code.

Each principle has a reason. If you think your change needs an exception, say so in the issue first. The decisions behind the principles are in the [decision register](../architecture/decisions.md).

## The principles

### 1. Modes never mix

- Build every config entry for exactly one mode: Standard (developer keys, HTTP polling), Enhanced (EcoFlow account sign-in, MQTT push) or Local (Modbus/TCP, no account). Reason: an entry that holds two sources has two answers for one value (ADR-030).
- Do not add a fallback from one mode to another, and do not add a switch at run time. Reason: a silent fallback hides which source is feeding the entity.
- Do not put developer keys and an account sign-in in one entry.

### 2. Enhanced Mode is push

- Do not add periodic HTTP polling to Enhanced Mode. Reason: the device already pushes every value over MQTT, and a second path for the same value is a second answer.
- The one documented exception is account-side history that the device does not push, read under the five limits that ADR-032 lists. A value that arrives over MQTT is never read over HTTP.

### 3. One value, one source

- Feed an entity from one path. Do not add a second path to the same entity, a second update rate for one reading or a second parser for a field that is already parsed. Reason: two paths drift apart and the user sees the value flip between them.
- When a value from another source looks equal to an existing one, prove they agree before you reuse the key. Until then, give it a key of its own (ADR-030).

### 4. No control without read-back

- Add a switch, number or select only when the device reports back the state it sets. Reason: a frame that was sent proves nothing about what the device did with it.
- State in your pull request how you confirmed the write on a real unit. [Adding Your Device](../add-your-device.md) shows the shape, including the hold between a request and the device's reply.
- Do not set an entity's state after sending and call that confirmation. Reason: state set without a device frame behind it is a guess.

### 5. The device is the contract

- Take field names, scaling and enum values from what the unit sends, never from another project that uses them. Reason: another project is evidence of what exists, not a specification of what this device does.
- Treat scaling as per field. The same message mixes watts with tenths of a watt, and nothing in a field name tells you which. Prove each scaling with the raw value next to what the EcoFlow app shows at the same moment.
- Open an issue and attach the recording before you write the parser. Reason: the maintainer confirms the data path, the device type and the mode, and that answer decides which files you touch.

### 6. One distinction, one registration

- Express a difference once. Do not add a second constant, device type, table or set for a difference the code already expresses. Reason: two places for one fact drift apart, and the next person updates only one.
- Treat an accessory whose data arrives only through a main unit as part of that unit's parser, not as a new device type (ADR-007, with ADR-008 for the one accessory that became its own device). Example: the PowerGlow heating rod (`HF33`) on a PowerOcean.

### 7. Missing data is absent, not zero

- When a field is absent from a frame, emit no key for it. Do not publish `0` to mean "no value yet". Reason: on a `total_increasing` sensor Home Assistant reads a zero as a meter reset, which damages the owner's energy history beyond repair by a later fix.
- Show a value that cannot be read as unavailable or unknown, never as `0` (ADR-032).

### 8. Zero-noise logging

- Log a WARNING or ERROR only when the user must act. Reason: users trust a clean log, and every needless entry teaches them to ignore it.
- Log transient errors at DEBUG: a single timeout, a reconnect, a decode error on an unknown field.
- Log a repeated error once, then stay quiet until it resolves.
- Name a device in a log by its serial prefix and tag, never by more of the serial (ADR-019). Reason: users attach logs to public issues.

### 9. Devices without developer-key access must not break

- Some devices are listed by the Developer API but refuse every request for their serial with error 1006. They work only in Enhanced Mode. Reason: they are a real part of the user base, and Standard Mode must not offer them.
- State in your pull request what your change does for them.

## How a review applies this

A review asks these questions of your change, in this order:

1. Which path carries the data? Name the function. Does that path run for the user who asked for the change?
2. Does the change create a second way to data that already has one? If yes, the answer is which way stays, and the other is removed.
3. Does it soften a mode boundary?
4. What happens on a device that cannot use developer keys?
5. What does it register (a constant, a type, a table) that is not registered already?

When a contribution does not fit, the maintainer says which principle it crosses and what would be merged instead. When a change rests on your recording or hardware test, the changelog names you and what your work showed.
