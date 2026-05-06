<div align="center">

# EcoFlow Energy

**Real-time solar, battery, grid & home power monitoring for Home Assistant.**
Energy Dashboard ready. Two modes: official API or real-time app connection.

</div>

## What you get

- **Up to 200 sensors per device** — power, energy, battery packs, temperature, diagnostics
- **Energy Dashboard ready** — local Riemann-sum kWh with gap detection
- **Real-time updates** — Enhanced Mode pushes data every 2-4 seconds
- **Full PowerOcean control** — Backup Reserve, Solar Surplus Threshold, Work Mode, all verified against the official EcoFlow app
- **Delta switches & numbers** — AC/DC output, charge speed, backup reserve, screen settings
- **Smart Plug** — power monitoring, on/off switch, max power limit
- **Auto-discovery** — picks up every device bound to your EcoFlow account

## Supported devices

| Device | Sensors | Controls |
|:---|:---:|:---|
| **PowerOcean** (Home Battery) | 202 | 2 numbers, 1 select (Enhanced only) |
| **Delta 2 Max** (Portable Power) | 94 | 7 switches, 8 numbers |
| **Smart Plug** | 11 | 1 switch, 2 numbers |

Other Delta-series devices (Delta Pro, Delta 2, etc.) typically work automatically with the Delta sensor set.

## Two modes

**Standard Mode** (recommended for stability)

Uses the official EcoFlow IoT Developer API. Apply for free API keys at [developer.ecoflow.com](https://developer.ecoflow.com). HTTP polling at ~30 seconds, plus MQTT push for Delta and Smart Plug. PowerOcean is read-only in this mode.

**Enhanced Mode** (recommended for control + speed)

Connects with your EcoFlow email and password. No Developer API keys needed. Real-time WSS MQTT updates and full PowerOcean controls (Backup Reserve, Solar Surplus, Work Mode). This is an unofficial, community-driven protocol that may change without notice.

## Install

After installing via HACS, restart Home Assistant, then add the integration via **Settings > Devices & Services > Add Integration > EcoFlow Energy** and choose your mode.

For full documentation, configuration details, automation examples, and troubleshooting, see the project README on GitHub.
