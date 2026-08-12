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

- **Over 200 sensors per device** - power, energy, battery packs, temperature, diagnostics
- **Energy Dashboard ready** - local Riemann-sum kWh with gap detection
- **Real-time out of the box** - Enhanced Mode: ~2-4 s updates for all devices
- **Full PowerOcean control** - Backup Reserve, Solar Surplus Threshold, Work Mode (Self-use / AI Schedule)
- **Delta switches & numbers** - AC/DC output, charge speed, backup reserve, screen settings
- **Auto-discovery** - all devices bound to your EcoFlow account
- **4-tier reconnect** - never gives up on the connection
- **Automatic fallback** - MQTT stale? Transparent switch to HTTP polling (Standard Mode)
- **Offline tolerance** - mobile devices offline = expected, not an error

---

## Supported Devices

| Device | Serial prefix | Sensors | Controls | Energy Sensors | Update Rate |
|:---|:---|:---:|:---:|:---:|:---|
| **PowerOcean** - Home Battery | `HJ31` `HJ32` `HJ35` `HJ37` `J32B` `J327`\* `J32D`\* `J32E`\* | 227 + 5 binary | 2 numbers, 1 select (Enhanced only) | 6 (solar, grid import/export, battery charge/discharge, home) | ~30 s standard / ~3 s enhanced |
| **PowerOcean Plus** - 3-phase Hybrid | `R371`\* `R372`\* `R374`\* `HJ3C`\* | 227 + 5 binary | 2 numbers, 1 select (Enhanced only) | 6 (solar, grid import/export, battery charge/discharge, home) | ~3 s enhanced |
| **Delta 2 Max** - Portable Power | `R351` `R331` | 94 + 4 binary | 7 switches, 8 numbers | 4 (solar 1+2, AC in/out) | ~30 s standard (+ MQTT push) |
| **Delta 3** - Portable Power | `D3M1` `D3N1` `P321` `P231` | 47 | 7 switches, 4 numbers, 5 selects (selects and 1 number Enhanced only); `D3M` serials add 3 switches, 3 numbers and 1 binary sensor for port priority | 4 (solar 1+2, AC in, output) | ~30 s standard / ~2 s enhanced |
| **Smart Plug** - Switchable Outlet | `HW52` | 11 + 1 binary | 1 switch, 2 numbers | 1 (total energy) | ~30 s standard / ~3 s enhanced |
| **Stream** - AC-coupled Battery | `BK31` `BK11` `BK41` `BK51` `BK61` | 54 + 2 binary | 1 number; `BK31` adds 2 numbers and 2 switches (Enhanced only) | 2 default (battery charge/discharge), 6 optional diagnostic (solar/home, PV 1-4) | ~30 s standard / ~3 s enhanced |
| **Stream Micro** - Grid-tie Inverter | `BK01`\* | 21 | - | 4 optional diagnostic (PV 1-4) | ~3 s enhanced |
| **STREAM AC 5000** - AC-coupled Battery | `ES22`\* | 56 + 2 binary | 2 switches, 5 numbers, 1 select (Enhanced only) | 4 default (grid import/export, battery charge/discharge), 1 optional diagnostic (home) | ~2 s enhanced |
| **STREAM 5000** - AC-coupled Battery | `ES21`\* | 56 + 2 binary | none yet, see below | 4 default (grid import/export, battery charge/discharge), 1 optional diagnostic (home) | ~2 s enhanced |

> **\* Enhanced Mode only.** These serial prefixes cannot currently be linked to an IoT Developer API key, so Standard Mode reports error 1006 and their entities stay unavailable. This is an EcoFlow API limitation, not a configuration problem.
>
> **PowerOcean and PowerOcean Plus share one entity set.** A Plus unit simply reports more of it: per-phase reactive power (var) and apparent power (VA), plus MPPT strings 3 and 4. Those entities exist for every PowerOcean but are disabled by default, because a standard unit never sends them and the entity would sit at "unknown" forever. Enable them under **Settings > Devices & services > Entities** on a Plus device.
>
> **Tip:** Other Delta-series devices (Delta Pro, Delta 2, etc.) should work automatically with the Delta sensor set. Base Delta 3 and Delta 3 Plus use the Delta 3 sensor set. The five AC-coupled Stream models share one sensor set.
>
> **The STREAM AC 5000 is not a Stream.** It shares the name and nothing else: it sends none of the BK-series telemetry messages, so it has its own parser and its own entity set. Its solar and per-phase meter entities are created only once the device reports them, because whether a unit has PV on the EcoFlow and which smart meter is linked are installation choices rather than model differences. Its solar reading is a figure the device derives for itself, so on an installation with separate PV it is the EcoFlow's inference rather than a measurement of that system, which is why there is no lifetime solar counter for it.
>
> **The STREAM 5000 reads but is not driven.** It is the same product as the STREAM AC 5000 on a different model number, and a recording from one shows it sending the same four telemetry messages, so it gets the same readings from the same parser. It does not get the controls. Every write this integration sends to that family is a rebuild of a frame captured from an AC 5000, and a power setpoint writes a scheduled task into the battery rather than flipping a display setting, so reading alike is not enough to assume writing alike. If you own one, a recording from your unit is what turns the controls on. Both models also report three blocks of readings this parser does not map yet, and the solar strings of a STREAM 5000 most likely sit among them; naming them needs a recording taken alongside what the EcoFlow app shows at the same moment.
>
> **The Stream Micro is the exception.** It is a grid-tie inverter with two solar strings and no battery, so it deliberately gets a reduced set: no battery, state of charge, backup reserve or AC outlet entities, because it has none of those and an entity Home Assistant once created stays in the registry forever.
>
> **Note:** Sensor counts are the device-specific entity definitions. Every device additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included in the counts above. Many sensors are diagnostic and disabled by default.

<details>
<summary><b>PowerOcean</b> and <b>PowerOcean Plus</b> - 3-phase grid, MPPT tracking, multi-pack battery, EMS diagnostics, energy strategy controls</summary>

3-phase grid monitoring (voltage, current, power per phase) · MPPT per-string tracking (up to 4 strings, device-dependent) · **Multi-battery-pack support** (up to 5 BP5000 packs - per-pack SoC, power, SoH, cycles, temperatures, lifetime energy) · Battery diagnostics (cell temps & voltages, MOSFET temps) · EMS state, work mode, feed mode, grid status, power factor · System diagnostics (fault codes, connectivity status, capacity limits)

**PowerOcean Plus** (`R371`, `R372`, `R374`, `HJ3C`) are the higher-power 3-phase hybrid units. They use the same entity set as a standard PowerOcean and are supported in Enhanced Mode. Beyond a standard unit they report per-phase **reactive power** (var) and **apparent power** (VA), and drive **MPPT strings 3 and 4**. These entities ship disabled by default so that standard units are not left with permanently empty sensors, so enable the ones you need after adding a Plus device. Field coverage is based on diagnostics from live Plus hardware; if your unit reports a value that no entity picks up, the raw data is available via **Download Diagnostics**.

**Enhanced Mode controls** (verified against the official EcoFlow app, byte-for-byte wire compatible):

- **Backup Reserve** (`number`, 0-100%) - minimum SoC the system keeps in reserve. Same slider as "Backup-Reserve" in the EcoFlow app.
- **Solar Surplus Threshold** (`number`, 0-100%) - SoC above which surplus solar is routed to controllable devices. Same slider as "Prioritize controllable devices (Beta)" in the app.
- **Work Mode** (`select`) - Self-use ("Eigenstromversorgung") or AI Schedule ("Intelligenter Modus"). TOU and Backup modes are deferred (require additional sub-parameters).

The integration enforces the app's `backup_reserve <= solar_surplus_threshold` constraint automatically.

**Note:** All credentials (API keys or email/password) are stored in Home Assistant's encrypted configuration storage (`.storage/core.config_entries`). This is standard Home Assistant behavior.

</details>

<details>
<summary><b>Delta 2 Max</b> - AC/DC/12V switches, charge speed control, real-time MQTT</summary>

Battery SoC/SoH · All input/output power, temperatures, voltages · **Expansion battery packs** (up to 2, disabled by default) · **Switches:** AC, DC, 12V output, beeper, X-Boost, AC auto restart, backup reserve · **Numbers:** AC charge speed (200-2400 W), max/min SoC, standby timeout, screen brightness/timeout, 12V port timeout, backup reserve level · Real-time MQTT push for faster-than-polling updates.

</details>

<details>
<summary><b>Smart Plug</b> - power monitoring, plug switch, automation-ready</summary>

Power (W), current (A), voltage (V), frequency, temperature · Plug on/off switch · **Numbers:** LED brightness (0-100%), max power limit (0-2500 W) · Real-time MQTT push in Standard Mode · ~3 s updates in Enhanced Mode. Ideal for automating charging (e.g. charge Delta on solar surplus).

</details>

<details>
<summary><b>Stream</b> (AC Pro, Ultra, Max, AC, Ultra X) - AC-coupled battery telemetry, per-string solar, reserve control</summary>

Battery SoC/SoH · signed battery power · battery charge/discharge power · **per-string solar power (PV 1-4)** · signed AC grid connection power ("Netz-Anschluss": negative=input, positive=output/feed-in) · AC outlet states and per-outlet power · AC voltage and frequency · battery temperature, capacity and cell voltage diagnostics · LED brightness diagnostics · **Numbers:** Backup Reserve (3-95%), plus Charge Limit and Discharge Limit on the Stream AC Pro (`BK31`) in Enhanced Mode · **Stream AC Pro switches:** AC outlets 1 and 2 in Enhanced Mode.

The Stream is treated as an AC-coupled battery. House, grid and total solar flow values depend on an EcoFlow-paired meter and are disabled by default as diagnostic entities. LED brightness is exposed read-only, since the app write path is not confirmed for third-party control. The AC Pro limit controls reproduce the app's grouped ConfigWrite containing its timestamp, charge limit, discharge limit and backup reserve. Raising the discharge limit also raises backup reserve to at least three percentage points above it, matching the behavior confirmed on hardware; lowering the discharge limit leaves backup reserve unchanged. The outlet switches reproduce the app's confirmed ConfigWrite fields `380` and `381` plus its required `from="ios"` header; live telemetry reports the relay states through fields `980`/`982` and per-outlet power through `1210`/`1211`.

**Both modes are supported, and they differ in solar detail.** Standard Mode reads the Stream through the official Developer API (~30 s) and reports all four solar strings: PV 1 and PV 2 are enabled by default, PV 3 and PV 4 ship disabled because only larger units drive that many strings. Enhanced Mode updates faster (~3 s) and reports PV 1 and PV 2 plus their input voltage and current, but not strings 3 and 4. Both modes create the same sensor set, while writable numbers require Enhanced Mode.

All five models are recognized by serial prefix and appear under their correct model name: Stream AC Pro (`BK31`), Stream Ultra (`BK11`), Stream Max (`BK41`), Stream AC (`BK51`), Stream Ultra X (`BK61`).

</details>

<details>
<summary><b>Stream Micro</b> (`BK01`) - grid-tie solar inverter, two strings, no battery</summary>

Per-string solar power, voltage and current for both strings · single-phase grid connection with voltage, current, frequency and power · grid connection state · the feed-in limit configured in the EcoFlow app · WiFi signal strength.

The Stream Micro feeds solar directly into the grid and has no battery and no AC outlets, so it gets a smaller entity set than the rest of the Stream family: no state of charge, no battery power or energy, no backup reserve and no outlet entities. Home Assistant keeps an entity in the registry once it has been created, so entities a device can never fill are not created in the first place.

**Enhanced Mode only.** The Stream Micro is not exposed through the EcoFlow Developer API at all, so it needs the EcoFlow account sign-in.

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
| **Devices** | All except the Enhanced-only serials (`J327`, `J32D`, `J32E`, `R371`, `R372`, `R374`, `HJ3C`) | All supported devices |
| **Update rate** | ~30 s HTTP polling (+ MQTT push for Delta/Smart Plug) | ~2-4 s real-time via WSS MQTT |
| **Delta / Smart Plug controls** | All switches and numbers | All switches and numbers |
| **PowerOcean controls** | Read-only sensors only | Full energy strategy controls (Backup Reserve, Solar Surplus Threshold, Work Mode) |
| **Stream AC Pro controls** | Not available | Charge Limit, Discharge Limit, Backup Reserve and AC outlet switches |
| **Stability** | Official EcoFlow API - supported and stable | Community-driven - unofficial, use at your own risk |
| **Best for** | Reliable long-term operation | Real-time monitoring, fast automations, PowerOcean control |

**Standard Mode** uses the official EcoFlow IoT Developer API. Apply for free API keys at [developer.ecoflow.com](https://developer.ecoflow.com). Note: the European PowerOcean variants (`J327`, `J32D`, `J32E`) and the PowerOcean Plus units (`R371`, `R372`, `R374`, `HJ3C`) are currently not exposed through the Developer API and cannot be linked to an API key (error 1006). These devices work in Enhanced Mode only.

**Enhanced Mode** connects with your EcoFlow email and password. No Developer API keys needed. Faster updates, but this is an unofficial, community-driven protocol based on observed behaviour that may change without notice. Stream-family devices report an empty product name, so they are identified by their serial prefix (`BK01`, `BK11`, `BK31`, `BK41`, `BK51`, `BK61`) and appear under the correct model name in Home Assistant in both modes. The Stream Micro (`BK01`) is not exposed through the Developer API at all and therefore needs Enhanced Mode.

**Upgrading?** See [CHANGELOG.md](CHANGELOG.md) for migration notes. Most upgrades are seamless. v1.13.0 removes the legacy `min_discharge_soc` PowerOcean entity (replaced by `backup_reserve`); after upgrading you may see it as "unavailable" in HA - safe to delete via Settings > Devices & services > Entities.

**Ran a Stream on a pre-release build?** Two things changed before the final release:
- Old experimental outlet switches and raw Wh battery-energy entities may remain in the entity registry. They are safe to delete if shown as unavailable or duplicated; use the kWh Battery Charge Energy and Battery Discharge Energy sensors for the Energy Dashboard.
- Solar Power, Home Power, and Grid Power are now meter-dependent diagnostics, disabled by default for new installs. Existing installs keep them enabled, so nothing disappears. If your Stream has no EcoFlow-paired meter these report unreliable values and can be disabled under Settings > Devices & services > Entities.

---

## Energy Dashboard

All energy sensors are pre-configured (`state_class: total_increasing`) - just select and go.

<details>
<summary><b>PowerOcean</b> - Grid, Solar, Battery, Home</summary>

| Dashboard Section | Sensor |
|:---|:---|
| Grid consumption | **Grid Import Energy** (kWh) |
| Return to grid | **Grid Export Energy** (kWh) |
| Solar production | **Solar Energy** (kWh) |
| Battery charge | **Battery Charge Energy** (kWh) |
| Battery discharge | **Battery Discharge Energy** (kWh) |
| Home consumption | **Home Energy** (kWh) |

> Select **Two sensors** for battery power - charge and discharge separately for higher accuracy.

</details>

<details>
<summary><b>Delta 2 Max</b> - Solar, AC Input, AC Output</summary>

| Dashboard Section | Sensor |
|:---|:---|
| Solar (MPPT 1) | **Solar Energy** (kWh) |
| Solar (MPPT 2) | **Solar 2 Energy** (kWh) |
| AC input | **AC Input Energy** (kWh) |
| AC output | **AC Output Energy** (kWh) |

</details>

<details>
<summary><b>Smart Plug</b> - Device Energy</summary>

| Dashboard Section | Sensor |
|:---|:---|
| Individual device | **Energy** (kWh) |

Add under **Energy > Individual Devices**.

</details>

<details>
<summary><b>Stream</b> - AC-coupled Battery</summary>

| Dashboard Section | Sensor |
|:---|:---|
| Battery charge | **Battery Charge Energy** (kWh) |
| Battery discharge | **Battery Discharge Energy** (kWh) |

Solar and home energy sensors exist as diagnostics but are disabled by default because the Stream only reports meaningful home/grid/solar flow values when an EcoFlow-compatible meter is paired in the app. A per-string counter (PV 1 Energy to PV 4 Energy) is available as well, also disabled by default because the solar energy sensor already covers the total. For normal AC-coupled battery use, select the two battery energy sensors above.

On a **Stream Micro** there is no battery, so solar production is the whole picture: enable **PV 1 Energy** and **PV 2 Energy** and add them under **Solar production**.

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
  - alias: "Grid export alert - use surplus"
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
            - consider turning on high-load devices
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
<summary><b>Devices stay unavailable and the log shows repeated reconnects</b></summary>

EcoFlow serves accounts from more than one region, and the server for your account is named in the credentials the integration fetches at sign-in. Versions before 1.17.0 ignored that and always used the European address, so an account served elsewhere was refused with nothing said about why: the connection opens, the server closes it again, and the cycle repeats.

If you see that pattern, update to 1.17.0 or newer. A diagnostics download reports the address in use under `mqtt_status` > `broker`, which is the fastest way to tell this apart from a credential problem.

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

- **Access Key / Secret Key** (IoT Developer Portal) - used for HTTP data polling
- **Email / Password** (Enhanced Mode only) - used for MQTT real-time data

If the devices are not linked to the API key, HTTP polling fails with error 1006 ("device not allowed"). In Enhanced Mode, MQTT data still works fine, but the repeated HTTP errors used to trigger a false re-authentication prompt.

**To fix:**

1. Log in at [developer.ecoflow.com](https://developer.ecoflow.com)
2. Go to "Devices" and verify both your API key and your devices are listed
3. Make sure the Developer Portal account uses the **same email** as your EcoFlow App account - devices are linked automatically when the accounts match
4. If the accounts differ, bind the devices manually via their serial numbers

Since v1.8.3, the integration handles this gracefully: error 1006 is logged once with a clear message and does not trigger re-authentication.

**PowerOcean variants with serial prefix `J327`, `J32D` or `J32E`:** the Developer Portal currently offers no way to link these devices to an API key, so Standard Mode entities stay unavailable (error 1006). This is an EcoFlow API limitation, not a configuration problem. Use Enhanced Mode for these devices - it delivers full real-time data.

</details>

<details>
<summary><b>Enhanced Mode issues</b></summary>

- Verify EcoFlow email and password
- Requires `cryptography` package (included in HA Core)
- Check logs for "Enhanced login failed" or "decryption failed"

</details>

<details>
<summary><b>Download diagnostics</b></summary>

**Settings > Devices & Services > EcoFlow Energy > 3-dot menu > Download Diagnostics** - connection status, data freshness, no credentials exposed.

</details>

---

<div align="center">

**MIT License** - [Contributing](https://github.com/shuette42/ecoflow-energy-ha/issues) welcome

Made by [huette.ai](https://huette.ai) - When it has to work.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-30D158?style=for-the-badge&logo=buy-me-a-coffee&logoColor=white)](https://www.buymeacoffee.com/shuette)

</div>
