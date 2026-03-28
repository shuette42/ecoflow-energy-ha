<div align="center">

# EcoFlow Energy for Home Assistant

### Real-time solar, battery, grid & home power monitoring

[![HACS Default](https://img.shields.io/badge/HACS-Default-30D158?style=for-the-badge&logo=home-assistant&logoColor=white)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/shuette42/ecoflow-energy-ha?style=for-the-badge&color=30D158)](https://github.com/shuette42/ecoflow-energy-ha/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/shuette42/ecoflow-energy-ha/tests.yml?branch=main&label=Tests&style=for-the-badge&logo=pytest&logoColor=white)](https://github.com/shuette42/ecoflow-energy-ha/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/github/license/shuette42/ecoflow-energy-ha?style=for-the-badge&color=30D158)](LICENSE)

<br>

**50+ sensors** &nbsp;·&nbsp; **Energy Dashboard ready** &nbsp;·&nbsp; **~3 s real-time updates** &nbsp;·&nbsp; **No portal login needed**

<br>

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=shuette42&repository=ecoflow-energy-ha&category=integration)

</div>

<br>

## What You Get

<table>
<tr>
<td>

**Sensors & Controls**<br>
50+ sensors per device — power, energy, battery, temperature, diagnostics. Switches and number controls for AC/DC output, charge speed, SoC limits.

</td>
<td>

**Energy Dashboard**<br>
Local Riemann-sum energy tracking with gap detection. All sensors pre-configured for the HA Energy Dashboard — just select and go.

</td>
</tr>
<tr>
<td>

**Real-Time Data**<br>
Delta gets MQTT push out of the box. PowerOcean Enhanced Mode delivers ~3 s Protobuf updates via WSS. No portal session needed.

</td>
<td>

**Resilient Connection**<br>
4-tier reconnect that never gives up. Automatic HTTP fallback when MQTT is stale. Stream health monitoring. Mobile devices offline = expected.

</td>
</tr>
</table>

<br>

## Supported Devices

> **Tip:** Other Delta-series devices (Delta Pro, Delta 2, etc.) should work automatically with the Delta sensor set.

| | Sensors | Controls | Energy | Update Rate |
|:---|:---:|:---:|:---:|:---|
| **PowerOcean** — Home Battery | 63 | — | 6 kWh | Standard ~30 s \| Enhanced ~3 s |
| **Delta 2 Max** — Portable Power | 62 | 3 switches · 4 numbers | 4 kWh | ~30 s + MQTT push |
| **Smart Plug** — Switchable Outlet | 11 | 1 switch | 1 kWh | ~30 s |

<details>
<summary><b>PowerOcean details</b></summary>
<br>

3-phase grid monitoring (voltage, current, power per phase) · MPPT per-string tracking (2 strings) · Battery diagnostics (SoH, cycles, cell temps & voltages, MOSFET temps) · EMS state, work mode, feed mode, grid status, power factor

**Enhanced Mode** upgrades PowerOcean from ~30 s HTTP polling to ~3 s WSS Protobuf push — requires EcoFlow email & password.

</details>

<details>
<summary><b>Delta 2 Max details</b></summary>
<br>

Battery SoC/SoH · All input/output power, temperatures, voltages · **Switches:** AC output, DC output, 12V output · **Numbers:** AC charge speed (200–2400 W), max/min SoC limits, standby timeout · Receives real-time MQTT push in Standard Mode automatically.

</details>

<details>
<summary><b>Smart Plug details</b></summary>
<br>

Power (W), current (A), voltage (V), frequency, temperature · Plug on/off switch · Ideal for automating charging (e.g. charge Delta when solar surplus).

</details>

<br>

## Quick Start

### 1. Install

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=shuette42&repository=ecoflow-energy-ha&category=integration)

Or: **HACS** > **Integrations** > **Explore & Download** > search **EcoFlow Energy** > **Download** > restart HA.

<details>
<summary>Manual installation</summary>
<br>

Download the [latest release](https://github.com/shuette42/ecoflow-energy-ha/releases), copy `custom_components/ecoflow_energy/` to your HA `config/custom_components/`, restart.

</details>

### 2. Configure

You need an **Access Key** and **Secret Key** from the [EcoFlow Developer Portal](https://developer.ecoflow.com).

1. **Settings > Devices & Services > Add Integration** > search **EcoFlow Energy**
2. Enter your Access Key and Secret Key
3. Select devices — done. Entities appear automatically.

### 3. Choose Your Mode

| | Standard | Enhanced |
|:---|:---|:---|
| **For** | All devices | PowerOcean only |
| **Update** | ~30 s HTTP + MQTT push (Delta) | ~3 s WSS Protobuf |
| **Credentials** | Access Key + Secret Key | + Email + Password |
| **Stability** | Official API — stable | Unofficial — may break |
| **Recommended** | Most users | PowerOcean real-time |

<br>

## Energy Dashboard

All energy sensors are pre-configured for the HA Energy Dashboard (`state_class: total_increasing`) — just select and go.

<details>
<summary><b>PowerOcean</b> — Grid, Solar, Battery, Home</summary>
<br>

| Dashboard Section | Sensor |
|:---|:---|
| Grid consumption | **Grid Import Energy** (kWh) |
| Return to grid | **Grid Export Energy** (kWh) |
| Solar production | **Solar Energy** (kWh) |
| Battery charge | **Battery Charge Energy** (kWh) |
| Battery discharge | **Battery Discharge Energy** (kWh) |
| Home consumption | **Home Energy** (kWh) |

> Select **Two sensors** for battery power — charge and discharge separately for higher accuracy.

</details>

<details>
<summary><b>Delta 2 Max</b> — Solar, AC Input, AC Output</summary>
<br>

| Dashboard Section | Sensor |
|:---|:---|
| Solar (MPPT 1) | **Solar Energy** (kWh) |
| Solar (MPPT 2) | **Solar 2 Energy** (kWh) |
| AC input | **AC Input Energy** (kWh) |
| AC output | **AC Output Energy** (kWh) |

</details>

<details>
<summary><b>Smart Plug</b> — Device Energy</summary>
<br>

| Dashboard Section | Sensor |
|:---|:---|
| Individual device | **Energy** (kWh) |

Add under **Energy > Individual Devices**.

</details>

<br>

## Automation Examples

<details>
<summary><b>Charge Delta when PowerOcean is full</b></summary>
<br>

```yaml
automation:
  - alias: "Charge Delta 2 Max when PowerOcean battery is full"
    trigger:
      - platform: numeric_state
        entity_id: sensor.ecoflow_powerocean_battery_soc
        above: 98
    condition:
      - condition: numeric_state
        entity_id: sensor.ecoflow_delta_2_max_soc
        below: 80
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.ecoflow_smart_plug_plug

  - alias: "Stop charging when full or PowerOcean drops"
    trigger:
      - platform: numeric_state
        entity_id: sensor.ecoflow_delta_2_max_soc
        above: 99
      - platform: numeric_state
        entity_id: sensor.ecoflow_powerocean_battery_soc
        below: 50
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.ecoflow_smart_plug_plug
```

</details>

<details>
<summary><b>Delta AC off at night</b></summary>
<br>

```yaml
automation:
  - alias: "Delta AC off at night"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.ecoflow_delta_2_max_ac_output
```

</details>

<details>
<summary><b>Solar surplus alert</b></summary>
<br>

```yaml
automation:
  - alias: "Grid export alert — use surplus"
    trigger:
      - platform: numeric_state
        entity_id: sensor.ecoflow_powerocean_grid_export_power
        above: 1000
        for: "00:05:00"
    action:
      - service: notify.mobile_app
        data:
          title: "Solar surplus"
          message: >
            Exporting {{ states('sensor.ecoflow_powerocean_grid_export_power') }}W
            — consider turning on high-load devices
```

</details>

<br>

## How It Compares

<details>
<summary><b>EcoFlow Energy vs other integrations</b></summary>
<br>

EcoFlow does not offer a local API. The official Developer Portal provides only HTTP polling (~30 s). EcoFlow Energy combines HTTP with MQTT push for real-time data.

| | EcoFlow Energy | Others |
|:---|:---|:---|
| Data source | MQTT push + HTTP fallback | HTTP only or basic MQTT |
| Portal login | Not required | Required |
| Reconnect | 4-tier, never gives up | Simple retry |
| Fallback | Auto HTTP when MQTT stale | None |
| Stream health | 3-state monitoring | Not tracked |
| Energy tracking | Local Riemann-sum | API totals |
| Device types | Heterogeneous in one integration | Single type |
| Control | Optimistic lock, zero-flicker | Read-only or basic |
| Offline handling | Expected, no error spam | Error |

</details>

<br>

## Troubleshooting

<details>
<summary><b>No entities appearing</b></summary>
<br>

- Devices must be online in the EcoFlow app
- Verify Access Key and Secret Key from the Developer Portal
- Check **Settings > System > Logs** for `ecoflow_energy`

</details>

<details>
<summary><b>Data not updating</b></summary>
<br>

- **Standard:** HTTP polls every ~30 s. Delta also gets MQTT push. Check credentials if no data.
- **Enhanced:** WSS auto-reconnects with new ClientID. Check logs for reconnect messages.

</details>

<details>
<summary><b>Enhanced Mode issues</b></summary>
<br>

- Verify EcoFlow email and password
- Requires `cryptography` package (included in HA Core)
- Check logs for "Enhanced login failed" or "decryption failed"

</details>

<details>
<summary><b>Download diagnostics</b></summary>
<br>

**Settings > Devices & Services > EcoFlow Energy > ⋮ > Download Diagnostics** — connection status, data freshness, no credentials exposed.

</details>

---

<div align="center">

**MIT License** — [Contributing](https://github.com/shuette42/ecoflow-energy-ha/issues) welcome

Made by [huette.ai](https://huette.ai) — When it has to work.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-30D158?style=for-the-badge&logo=buy-me-a-coffee&logoColor=white)](https://www.buymeacoffee.com/shuette)

</div>
