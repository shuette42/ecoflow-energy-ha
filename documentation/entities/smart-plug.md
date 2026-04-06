# Smart Plug - Entity Reference

Full list of all entities created for Smart Plug devices (HW52 series).

**Totals:** 11 sensors, 1 binary sensor, 1 switch, 2 numbers

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow Smart Plug > Entities**.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Power | W | - | enabled | Current power consumption |
| Current | A | - | enabled | Current draw |
| Voltage | V | - | enabled | Line voltage |
| Frequency | Hz | diagnostic | enabled | Line frequency |
| Temperature | C | diagnostic | enabled | Internal temperature |
| Max Power Rating | W | diagnostic | disabled | Maximum rated power |
| Max Current Rating | A | diagnostic | disabled | Maximum rated current |
| LED Brightness | % | diagnostic | disabled | Current LED brightness |
| Error Code | - | diagnostic | disabled | Error indicator |
| Warning Code | - | diagnostic | disabled | Warning indicator |

## Sensors - Energy Dashboard

| Entity | Dashboard Section |
|:---|:---|
| Energy | Individual devices (total energy consumed) |

> Add under **Settings > Dashboards > Energy > Individual Devices**.

---

## Binary Sensors

| Entity | Description |
|:---|:---|
| Relay | Plug relay state (on/off) |

---

## Switches

| Entity | Description |
|:---|:---|
| Plug | Turn the plug on/off |

---

## Numbers

| Entity | Unit | Range | Step | Description |
|:---|:---:|:---:|:---:|:---|
| LED Brightness | % | 0 - 100 | 5 | Status LED brightness |
| Max Power Limit | W | 0 - 2500 | 100 | Overload protection threshold |

---

## Notes

- Standard Mode uses HTTP polling (~30s) with MQTT push for near-instant updates
- Enhanced Mode provides ~3s real-time updates via WSS
- The Smart Plug is ideal for automation scenarios (e.g., charge a Delta on solar surplus via the Plug switch)
- The Max Power Limit acts as overload protection - the plug disconnects when exceeded
