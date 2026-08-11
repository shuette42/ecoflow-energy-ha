# Stream AC Pro Entities

Stream AC Pro controls require Enhanced Mode and use the EcoFlow app-auth MQTT protocol.

## Outlet Controls

| Entity | Type | Confirmed wire field |
|:---|:---|:---|
| AC Outlet 1 | switch | `cmd_func=254`, `cmd_id=17`, field `380` |
| AC Outlet 2 | switch | `cmd_func=254`, `cmd_id=17`, field `381` |

The fields were observed in live iOS MQTT captures on `/app/{userId}/{sn}/thing/property/set`. The app's frame includes header field `23` (`from`) set to `ios`; without it the device acknowledges neither outlet action. Replies arrive on `/thing/property/set_reply` and mirror the written field through `cmd_id=18`. Both relay changes were confirmed by subsequent telemetry and on live hardware.

## Primary Sensors

| Entity | Notes |
|:---|:---|
| Battery SOC | Main battery percentage |
| Battery Power | Signed battery path: positive charging, negative discharging |
| Battery Charge Power | Derived from positive Battery Power |
| Battery Discharge Power | Derived from negative Battery Power |
| AC Grid Connection Power | Signed app "Netz-Anschluss" value |
| Battery Charge Energy | Energy Dashboard ready |
| Battery Discharge Energy | Energy Dashboard ready |
| Battery SoH | Battery state of health |
| Battery Voltage | Battery voltage |
| Battery Temp | Battery temperature |
| AC Voltage | AC line voltage |
| AC Frequency | AC line frequency |

## Diagnostic Sensors

House, grid, and solar flow values are meter-dependent and disabled by default unless an EcoFlow-compatible meter is paired in the app.

| Entity | Confirmed / observed field |
|:---|:---|
| Solar Power | `254/21` field `517` |
| Home Power | `254/21` field `516` |
| Grid Power | `254/21` field `515` |
| Home From Battery | `254/21` field `1003` |
| Home From Grid | `254/21` field `1004` |
| AC Outlet 1 Power | `254/21` field `1210` |
| AC Outlet 2 Power | `254/21` field `1211` |
| LED Brightness | `254/21` field `994`, set reply field `384` |
| AC Outlet 1 State | `254/21` field `980` |
| AC Outlet 2 State | `254/21` field `982` |
| Backup Reserve | `254/21` field `461`, set reply field `102` |
| Max Charge SoC | `254/21` field `270` |
| Min Discharge SoC | `254/21` field `271` |

## Capture Notes

The August 2026 capture confirmed:

- AC outlet writes use ConfigWrite fields `380` and `381` with header field `23` set to `ios`.
- Relay state is reported independently through telemetry fields `980` and `982`.
- Per-outlet real power is reported through fields `1210` and `1211`; a known load measured approximately 183-206 W while the grid connection was roughly 10-20 W higher due to device consumption and conversion losses.
- Stream MQTT topics used by the app include `/app/device/property/{sn}`, `/thing/property/get_reply`, `/thing/property/set`, and `/thing/property/set_reply`.
