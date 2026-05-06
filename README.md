<div align="center">

# EcoFlow Energy for Home Assistant

**Real-time solar, battery, grid & home power monitoring.**
**Energy Dashboard ready. Two modes: official API or real-time app connection.**

[![HACS Default](https://img.shields.io/badge/HACS-Default-30D158?style=for-the-badge&logo=home-assistant&logoColor=white)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/shuette42/ecoflow-energy-ha?style=for-the-badge&color=30D158)](https://github.com/shuette42/ecoflow-energy-ha/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/shuette42/ecoflow-energy-ha/tests.yml?branch=main&label=Tests&style=for-the-badge&logo=pytest&logoColor=white)](https://github.com/shuette42/ecoflow-energy-ha/actions/workflows/tests.yml)

<br>

<img src="https://raw.githubusercontent.com/shuette42/ecoflow-energy-ha/main/images/energy-flow.png" alt="Energy Flow" width="280">&nbsp;&nbsp;&nbsp;&nbsp;<img src="https://raw.githubusercontent.com/shuette42/ecoflow-energy-ha/main/images/energy-sources.png" alt="Energy Sources" width="340">

<br>

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=shuette42&repository=ecoflow-energy-ha&category=integration)

</div>

---

## Highlights

- **Up to 200 sensors per device** — power, energy, battery packs, temperature, diagnostics
- **Energy Dashboard ready** — local Riemann-sum kWh with gap detection
- **Real-time out of the box** — Enhanced Mode: ~2-4 s updates for all devices
- **Full PowerOcean control** — Backup Reserve, Solar Surplus Threshold, Work Mode (Self-use / AI Schedule)
- **Delta switches & numbers** — AC/DC output, charge speed, backup reserve, screen settings
- **Auto-discovery** — all devices bound to your EcoFlow account
- **4-tier reconnect** — never gives up on the connection
- **Automatic fallback** — MQTT stale? Transparent switch to HTTP polling (Standard Mode)
- **Offline tolerance** — mobile devices offline = expected, not an error

---

## Supported Devices

| | Sensors | Controls | Energy Sensors | Update Rate |
|:---|:---:|:---:|:---:|:---|
| **PowerOcean** — Home Battery | 202 | 2 numbers, 1 select (Enhanced only) | 6 (solar, grid, battery, home) | ~30 s standard / ~3 s enhanced |
| **Delta 2 Max** — Portable Power | 94 | 7 switches, 8 numbers | 4 (solar 1+2, AC in/out) | ~30 s standard / ~2 s enhanced |
| **Smart Plug** — Switchable Outlet | 11 | 1 switch, 2 numbers | 1 (total energy) | ~30 s standard / ~3 s enhanced |

> **Tip:** Other Delta-series devices (Delta Pro, Delta 2, etc.) should work automatically with the Delta sensor set.

<details>
<summary><b>PowerOcean</b> — 3-phase grid, MPPT tracking, multi-pack battery, EMS diagnostics, energy strategy controls</summary>

3-phase grid monitoring (voltage, current, power per phase) · MPPT per-string tracking (2 strings) · **Multi-battery-pack support** (up to 5 BP5000 packs — per-pack SoC, power, SoH, cycles, temperatures, lifetime energy) · Battery diagnostics (cell temps & voltages, MOSFET temps) · EMS state, work mode, feed mode, grid status, power factor · System diagnostics (fault codes, connectivity status, capacity limits)

**Enhanced Mode controls** (verified against the official EcoFlow app, byte-for-byte wire compatible):

- **Backup Reserve** (`number`, 0-100%) — minimum SoC the system keeps in reserve. Same slider as "Backup-Reserve" in the EcoFlow app.
- **Solar Surplus Threshold** (`number`, 0-100%) — SoC above which surplus solar is routed to controllable devices. Same slider as "Prioritize controllable devices (Beta)" in the app.
- **Work Mode** (`select`) — Self-use ("Eigenstromversorgung") or AI Schedule ("Intelligenter Modus"). TOU and Backup modes are deferred (require additional sub-parameters).

The integration enforces the app's `backup_reserve <= solar_surplus_threshold` constraint automatically.

**Note:** All credentials (API keys or email/password) are stored in Home Assistant's encrypted configuration storage (`.storage/core.config_entries`). This is standard Home Assistant behavior.

</details>

<details>
<summary><b>Delta 2 Max</b> — AC/DC/12V switches, charge speed control, real-time MQTT</summary>

Battery SoC/SoH · All input/output power, temperatures, voltages · **Expansion battery packs** (up to 2, disabled by default) · **Switches:** AC, DC, 12V output, beeper, X-Boost, AC auto restart, backup reserve · **Numbers:** AC charge speed (200-2400 W), max/min SoC, standby timeout, screen brightness/timeout, 12V port timeout, backup reserve level · Real-time MQTT push in Standard Mode · ~2 s updates in Enhanced Mode.

</details>

<details>
<summary><b>Smart Plug</b> — power monitoring, plug switch, automation-ready</summary>

Power (W), current (A), voltage (V), frequency, temperature · Plug on/off switch · **Numbers:** LED brightness (0-100%), max power limit (0-2500 W) · Real-time MQTT push in Standard Mode · ~3 s updates in Enhanced Mode. Ideal for automating charging (e.g. charge Delta on solar surplus).

</details>

---

## Quick Start

### 1. Install

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=shuette42&repository=ecoflow-energy-ha&category=integration)

Or: **HACS** > **Integrations** > **Explore & Download** > search **EcoFlow Energy** > **Download** > restart HA.

<details>
<summary>Manual installation</summary>

Download the [latest release](https://github.com/shuette42/ecoflow-energy-ha/releases), copy `custom_components/ecoflow_energy/` to your HA `config/custom_components/`, restart.

</details>

### 2. Configure

**Settings > Devices & Services > Add Integration** > search **EcoFlow Energy** > choose your mode:

| | Standard | Enhanced |
|:---|:---|:---|
| **Credentials** | Access Key + Secret Key ([Developer Portal](https://developer.ecoflow.com)) | EcoFlow email + password (same as mobile app) |
| **Devices** | All supported devices | All supported devices |
| **Update rate** | ~30 s HTTP polling (+ MQTT push for Delta/Smart Plug) | ~2-4 s real-time via WSS MQTT |
| **Delta / Smart Plug controls** | All switches and numbers | All switches and numbers |
| **PowerOcean controls** | Read-only sensors only | Full energy strategy controls (Backup Reserve, Solar Surplus Threshold, Work Mode) |
| **Stability** | Official EcoFlow API - supported and stable | Community-driven - unofficial, use at your own risk |
| **Best for** | Reliable long-term operation | Real-time monitoring, fast automations, PowerOcean control |

**Standard Mode** uses the official EcoFlow IoT Developer API. Apply for free API keys at [developer.ecoflow.com](https://developer.ecoflow.com).

**Enhanced Mode** connects with your EcoFlow email and password. No Developer API keys needed. Faster updates, but this is an unofficial, community-driven protocol that may change without notice.

**Upgrading?** See [CHANGELOG.md](CHANGELOG.md) for migration notes. Most upgrades are seamless. v1.13.0 removes the legacy `min_discharge_soc` PowerOcean entity (replaced by `backup_reserve`); after upgrading you may see it as "unavailable" in HA - safe to delete via Settings > Devices & services > Entities.

---

## Energy Dashboard

All energy sensors are pre-configured (`state_class: total_increasing`) — just select and go.

<details>
<summary><b>PowerOcean</b> — Grid, Solar, Battery, Home</summary>

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

| Dashboard Section | Sensor |
|:---|:---|
| Solar (MPPT 1) | **Solar Energy** (kWh) |
| Solar (MPPT 2) | **Solar 2 Energy** (kWh) |
| AC input | **AC Input Energy** (kWh) |
| AC output | **AC Output Energy** (kWh) |

</details>

<details>
<summary><b>Smart Plug</b> — Device Energy</summary>

| Dashboard Section | Sensor |
|:---|:---|
| Individual device | **Energy** (kWh) |

Add under **Energy > Individual Devices**.

</details>

---

## Automation Examples

<details>
<summary><b>Charge Delta when PowerOcean is full</b></summary>

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

<details>
<summary><b>PowerOcean dynamic backup reserve (Enhanced Mode)</b></summary>

Raise the backup reserve when an EV is plugged in or a storm is forecast, lower it overnight to use the battery for self-consumption.

```yaml
automation:
  - alias: "Backup reserve high before storm"
    trigger:
      - platform: state
        entity_id: weather.home
        attribute: forecast
    condition:
      - condition: template
        value_template: >
          {{ state_attr('weather.home', 'forecast')[0].condition in ['lightning', 'lightning-rainy'] }}
    action:
      - service: number.set_value
        target:
          entity_id: number.ecoflow_powerocean_backup_reserve
        data:
          value: 80

  - alias: "Backup reserve low overnight"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: number.set_value
        target:
          entity_id: number.ecoflow_powerocean_backup_reserve
        data:
          value: 10
```

</details>

<details>
<summary><b>PowerOcean Work Mode switching (Enhanced Mode)</b></summary>

Switch to AI Schedule when dynamic-tariff data is available, fall back to Self-use otherwise.

```yaml
automation:
  - alias: "Work Mode AI Schedule on cheap-tariff days"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tibber_price_total
        below: 0.20
    action:
      - service: select.select_option
        target:
          entity_id: select.ecoflow_powerocean_work_mode
        data:
          option: "ai_schedule"
```

</details>

---

## How It Compares

<details>
<summary><b>EcoFlow Energy vs other integrations</b></summary>

| | EcoFlow Energy | Others |
|:---|:---|:---|
| Data source | MQTT push + HTTP fallback | HTTP only or basic MQTT |
| Portal login | Not required | Required |
| Reconnect | 4-tier, never gives up | Simple retry |
| Fallback | Auto HTTP when MQTT stale | None |
| Stream health | 3-state monitoring | Not tracked |
| Energy tracking | Local Riemann-sum | API totals |
| Device types | Heterogeneous in one integration | Single type |
| PowerOcean control | Backup Reserve, Solar Surplus, Work Mode (verified app-replay) | Read-only or untested |
| Control | Optimistic lock, zero-flicker | Read-only or basic |
| Offline handling | Expected, no error spam | Error |

</details>

---

## Troubleshooting

<details>
<summary><b>No entities appearing</b></summary>

- Devices must be online in the EcoFlow app
- Verify Access Key and Secret Key from the Developer Portal
- Check **Settings > System > Logs** for `ecoflow_energy`

</details>

<details>
<summary><b>Data not updating</b></summary>

- **Standard:** HTTP polls every ~30 s. Delta also gets MQTT push. Check credentials if no data.
- **Enhanced:** WSS auto-reconnects with new ClientID. Check logs for reconnect messages.

</details>

<details>
<summary><b>Update credentials (manual re-auth)</b></summary>

Use the integration menu (not the options dialog):

**Settings > Devices & Services > EcoFlow Energy > 3-dot menu > Reconfigure**

- German UI label: **Neu konfigurieren**
- This opens the manual credential update flow for Access Key / Secret Key (and Enhanced credentials if enabled)

</details>

<details>
<summary><b>"Authentication expired" after restart</b></summary>

This notification can appear when your IoT Developer API key does not have access to the configured devices. The integration uses two credential sets:

- **Access Key / Secret Key** (IoT Developer Portal) — used for HTTP data polling
- **Email / Password** (Enhanced Mode only) — used for MQTT real-time data

If the devices are not linked to the API key, HTTP polling fails with error 1006 ("device not allowed"). In Enhanced Mode, MQTT data still works fine, but the repeated HTTP errors used to trigger a false re-authentication prompt.

**To fix:**

1. Log in at [developer.ecoflow.com](https://developer.ecoflow.com)
2. Go to "Devices" and verify both your API key and your devices are listed
3. Make sure the Developer Portal account uses the **same email** as your EcoFlow App account — devices are linked automatically when the accounts match
4. If the accounts differ, bind the devices manually via their serial numbers

Since v1.8.3, the integration handles this gracefully: error 1006 is logged once with a clear message and does not trigger re-authentication.

</details>

<details>
<summary><b>Enhanced Mode issues</b></summary>

- Verify EcoFlow email and password
- Requires `cryptography` package (included in HA Core)
- Check logs for "Enhanced login failed" or "decryption failed"

</details>

<details>
<summary><b>Download diagnostics</b></summary>

**Settings > Devices & Services > EcoFlow Energy > 3-dot menu > Download Diagnostics** — connection status, data freshness, no credentials exposed.

</details>

---

<div align="center">

**MIT License** — [Contributing](https://github.com/shuette42/ecoflow-energy-ha/issues) welcome

Made by [huette.ai](https://huette.ai) — When it has to work.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-30D158?style=for-the-badge&logo=buy-me-a-coffee&logoColor=white)](https://www.buymeacoffee.com/shuette)

</div>
