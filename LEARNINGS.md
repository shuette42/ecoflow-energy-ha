# Learnings: EcoFlow Energy HACS Integration

Collected findings from developing the HACS integration, ported from the reference project
`/Users/shuette/Documents/Repo/EcoFlow/`.

---

## 1. EcoFlow IoT API — Three HTTP Methods on One Path

The endpoint `/iot-open/sign/device/quota` is used with **three different HTTP methods**:

| Method | Purpose | Body/Query |
|--------|---------|------------|
| `POST`  | Read specific quotas | `{"sn":"...","params":{"quotas":["mpptPwr","bpSoc"]}}` |
| `PUT`   | Control device (SET) | `{"sn":"...","params":{"moduleType":3,"operateType":"acOutCfg",...}}` |
| `GET`   | Read specific quotas (Delta-style) | Query: `quotas=pd.soc` |

Additionally: `GET /iot-open/sign/device/quota/all?sn=...` returns **all** quotas at once.

**Key finding:** `GET /quota/all` provides the most complete data for **all device types**
(344 keys for PowerOcean, 240 keys for Delta). The `POST /quota` endpoint only returns a
subset (~6 keys for PowerOcean). Therefore Standard Mode uses `GET /quota/all`.

## 2. PowerOcean vs Delta — Fundamentally Different API Formats

### PowerOcean `GET /quota/all` Response
- Flat top-level keys: `mpptPwr`, `sysGridPwr`, `bpSoc`
- **Nested battery packs** as JSON strings(!): `bp_addr.{serial_number}` → string containing JSON object
- EMS data: `ems_change_report.*`, `energy_stream.*`
- Phase data: `pcsAPhase`, `pcsBPhase`, `pcsCPhase` (may also be JSON strings)

### Delta 2 Max `GET /quota/all` Response
- **Dot-notated keys:** `pd.soc`, `inv.outputWatts`, `bms_bmsStatus.vol`
- All values as native types (int/float), no JSON strings
- Module prefixes: `pd.*`, `inv.*`, `mppt.*`, `bms_bmsStatus.*`, `bms_emsStatus.*`

### Delta MQTT `/quota` Reports
- **TypeCode format:** `{"typeCode": "pdStatus", "params": {"soc": 85, ...}}`
- No dot prefix — `soc` instead of `pd.soc`
- Different reports: pdStatus, invStatus, bmsStatus, mpptStatus, emsStatus

**Conclusion:** Each device type needs its own parser. There is no generic approach.

## 3. JSON Strings in API Responses

The `GET /quota/all` response contains nested objects partially as **JSON strings**,
not as native dicts. Example:

```json
{
  "bp_addr.HJ32ZDH5ZG190227": "{\"bpCycles\": 460, \"bpSoh\": 100, ...}"
}
```

The parser must check `isinstance(val, str)` and call `json.loads()`.
This was a bug that caused battery detail sensors (Cycles, SOH, Voltage, Temps)
to show no values.

## 4. Unit Conversions — Three Different Amplifications

The EcoFlow API uses different amplifications for the same physical type:

| Unit    | Factor | Example Keys |
|---------|--------|--------------|
| mV → V  | /1000  | `bms_bmsStatus.vol`, `inv.invOutVol`, `inv.acInVol` |
| dV → V  | /10    | `mppt.inVol`, `mppt.pv2InVol` (solar voltages) |
| mA → A  | /1000  | `bms_bmsStatus.amp`, `inv.invOutAmp`, `mppt.inAmp` |

**Lesson learned:** Solar voltages are in deci-volts (dV), not milli-volts (mV)!
Missing initially → values off by factor 100.

Additionally: `bms_bmsStatus.temp` has a **+15 offset** that must be subtracted.

## 5. Standard Mode Does Not Need MQTT for Data

**Original mistake:** The integration tried to use MQTT as the primary data source in
Standard Mode. However, EcoFlow stops the MQTT data stream after ~60s when no
app/portal session is active.

**Solution:** Standard Mode uses exclusively **HTTP polling** (every 30s) via
`GET /quota/all`. MQTT remains connected in Standard Mode only for SET commands (Switches, Numbers).
MQTT data updates are ignored in Standard Mode.

**Enhanced Mode:** Uses WSS MQTT (port 8084) with Protobuf as the primary source (~3s updates).
Requires email/password login + EnergyStreamSwitch keepalive every 20s.

## 6. HTTP Path Issues During Porting

When porting from the reference project to the HACS integration, several API details were lost:

| What | Ref Project | HACS (wrong) | HACS (corrected) |
|------|-------------|--------------|-------------------|
| HTTP Quota path | `/iot-open/sign/device/quota` | `/iot-open/sign/device/quota/all` | Both — depending on purpose |
| MQTT Keepalive | 120s | 60s | 120s |
| MQTT in Standard | SET only | As data source | SET only |
| Delta JSON Parsing | `parse_delta_report()` | Not called | Correctly called |

## 7. Device Type Detection

The EcoFlow API provides `productName` (e.g. "PowerOcean", "DELTA 2 Max", "Smart Plug"),
but no separate `productType` field. Detection is done via **string matching**
on `productName`:

```python
_POWEROCEAN_KEYWORDS = ("powerocean", "power ocean")
_DELTA_KEYWORDS = ("delta",)
```

**Important:** The `device_type` must be stored in the config entry (`_normalize_devices`),
so it is available on next startup without another API call.

Unknown devices (e.g. "Smart Plug") get `device_type: unknown` and **no entities** —
better than assigning incorrect PowerOcean entities.

## 8. HMAC-SHA256 Signature

The EcoFlow API uses HMAC-SHA256 with a specific flatten logic for nested objects:

- Arrays: `quotas[0]=mpptPwr&quotas[1]=bpSoc`
- Objects: `params.cmdSet=11&params.id=24`
- Sorted alphabetically
- `accessKey`, `nonce`, `timestamp` appended at the end
- Signature as hex string in the HTTP header

**For GET requests:** Query parameters are signed (e.g. `sn=...`), no body.
**For POST requests:** Body is signed, Content-Type must be `application/json;charset=UTF-8`.

## 9. Sensor Definitions Must Match Data Sources

Sensors without a data source permanently show "Unknown" in HA. This confuses users.

**Rule:** Only define sensors for which the active mode has a data source.
Sensors only available in Enhanced Mode (battery cell voltages, detailed temps
from Protobuf) should only be created when Enhanced Mode is configured.

## 10. Docker Deployment Artifacts

`docker cp` copies the entire folder content including `__pycache__` and can
create nested duplicates (`ecoflow_energy/ecoflow_energy/`). These must be
removed before git commit.

## 11. HA Logging

Home Assistant logs at `WARNING` level by default. For EcoFlow debugging:

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.ecoflow_energy: debug
```

Without this, INFO logs like "MQTT started", "HTTP quota OK" etc. are not visible.
