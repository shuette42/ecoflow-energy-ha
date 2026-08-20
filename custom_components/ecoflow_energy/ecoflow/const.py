"""EcoFlow API constants and default configuration."""

from __future__ import annotations

# MQTT Broker
MQTT_HOST = "mqtt-e.ecoflow.com"
MQTT_PORT_TCP = 8883
MQTT_PORT_WSS = 8084
MQTT_WSS_PATH = "/mqtt"

# Default MQTT settings
DEFAULT_MQTT_KEEPALIVE = 60
DEFAULT_WSS_KEEPALIVE = 30
DEFAULT_MQTT_CLEAN_SESSION = True
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_RECONNECT_DELAY = 5
DEFAULT_MAX_RECONNECT_DELAY = 60
DEFAULT_COUNTER_RESET_INTERVAL = 300  # 5 min

# IoT Developer API
IOT_API_BASE = "https://api-e.ecoflow.com"
IOT_CERT_PATH = "/iot-open/sign/certification"
IOT_DEVICE_LIST_PATH = "/iot-open/sign/device/list"
IOT_QUOTA_PATH = "/iot-open/sign/device/quota"
IOT_QUOTA_ALL_PATH = "/iot-open/sign/device/quota/all"

# Rate limits
IOT_MIN_FETCH_INTERVAL_S = 60.0
QUOTA_HTTP_MIN_INTERVAL_S = 10.0
HTTP_RETRIES = 3
HTTP_RETRY_BACKOFF_S = 2.0

# Device types
DEVICE_TYPE_POWEROCEAN = "powerocean"
DEVICE_TYPE_DELTA = "delta"
DEVICE_TYPE_DELTA3 = "delta3"
DEVICE_TYPE_SMARTPLUG = "smartplug"
DEVICE_TYPE_STREAM = "stream"
# Carries the Stream name but not the Stream protocol: no 254/21 frame, and
# nested submessages where the BK series uses flat scalars.
DEVICE_TYPE_STREAM_AC5000 = "stream_ac5000"
# Microinverter, not a battery. Shares nothing with the Stream line but the
# five letters in the middle of its name (#230).
DEVICE_TYPE_POWERSTREAM = "powerstream"
DEVICE_TYPE_UNKNOWN = "unknown"

# Keywords used to classify devices from productName strings.
# Delta 3 keywords must be checked BEFORE the generic "delta" keyword:
# "DELTA 3 Max Plus" contains "delta" and would otherwise be routed to
# the Delta 2 Max parser, whose field map matches none of its quota keys.
_POWEROCEAN_KEYWORDS = ("powerocean", "power ocean")
# Family net: intentionally matches the whole Delta 3 line (base Delta 3,
# Delta 3 Plus, Delta 3 Max Plus). Their quota key sets are identical; the
# base model just omits PV2, so one parser and one device type serve all.
_DELTA3_KEYWORDS = ("delta 3", "delta3")
_DELTA_KEYWORDS = ("delta",)
_SMARTPLUG_KEYWORDS = ("smart plug", "smartplug")
_STREAM_KEYWORDS = ("stream",)
# Checked before the Stream list, and the order is the whole point. The match
# is by substring with no word boundary, so "PowerStream" contains "stream":
# in the other order a microinverter is classified as a Stream battery and
# given its full entity set, which then stays empty for good, which is exactly
# what #188 reported. Until this parser existed the same ordering was used to
# route the name to DEVICE_TYPE_UNKNOWN instead.
_POWERSTREAM_KEYWORDS = ("powerstream", "power stream")

_SN_PREFIX_MAP = {
    "HJ31": DEVICE_TYPE_POWEROCEAN,
    "HJ32": DEVICE_TYPE_POWEROCEAN,
    # PowerOcean gateway variant (#165). Unlike every other prefix here this
    # one rests on a third-party report rather than on a capture from a user
    # of this integration, so the field layout is assumed rather than shown.
    # Routing it costs nothing if the assumption holds and produces a
    # diagnosable device if it does not - which beats "unsupported device"
    # either way, because that state carries no information at all.
    "HJ35": DEVICE_TYPE_POWEROCEAN,
    # Three-phase PowerOcean variant (#245). Two raw captures from the same
    # reporter unit carry `96/1`, `96/7`, `96/8`, `96/33` and `96/39` - the EMS
    # heartbeat, the battery pack heartbeat, the EMS change report, the energy
    # stream report that holds the core power readings, and the PV inverter
    # stream. All five are decoded today, so this unit reports live data into
    # the existing parser rather than merely being recognised. The captures
    # also hold `96/34`, `53/14`, `53/113`, `241/5`, `224/38` and the `209`
    # family, none of which is registered - as with every other PowerOcean
    # variant here, the device says more than the parser reads.
    "HJ37": DEVICE_TYPE_POWEROCEAN,
    # European PowerOcean variant (#89): verified against live hardware in
    # Enhanced mode - telemetry matches the EcoFlow app (grid, battery, MPPT).
    "J32D": DEVICE_TYPE_POWEROCEAN,
    # Single-phase European PowerOcean variant (#89): verified via reporter
    # diagnostics in Enhanced mode - live data across grid/battery/MPPT;
    # single-phase unit, so only grid phases A and B carry values.
    "J32E": DEVICE_TYPE_POWEROCEAN,
    # PowerOcean variant (#194): shown by a raw capture from a reporter unit,
    # which carries a cmd_func 96 / cmd_id 1 frame - the EMS heartbeat this
    # integration already decodes. That command family is PowerOcean only;
    # the Delta 3 generation uses 254. The capture also holds frames on
    # 254/32, 53/14 and 241/36, none of which is registered, so the unit
    # reports more than the PowerOcean parser reads. Routing it delivers
    # what the parser understands today rather than nothing at all.
    "J32B": DEVICE_TYPE_POWEROCEAN,
    # Single-phase 5 kW hybrid inverter, European (#225). Routed on the same
    # evidence as J32B and rather more of it: a reporter capture holds 44
    # command frames, of which the EMS heartbeat `96/8` alone accounts for 17
    # and `96/1`, `96/7` and `96/13` for six more. All four are already
    # decoded, so this unit reports live data into the existing parser rather
    # than merely being recognised. The capture also carries `96/11`, `96/26`,
    # `96/34`, `254/32`, `53/14`, `241/5` and `241/36`, none registered, so it
    # says more than the parser reads - the same situation as every other
    # PowerOcean variant here.
    "J327": DEVICE_TYPE_POWEROCEAN,
    # PowerOcean Plus variants (#88): higher-power 3-phase hybrid units
    # (e.g. P3-S1, ~25-30 kW). Not exposed through the Developer API, so
    # Enhanced mode only - same situation as J32D/J32E. Routed to the
    # PowerOcean parser so entities are created; field layout to be
    # confirmed via reporter diagnostics on a Plus unit.
    "R371": DEVICE_TYPE_POWEROCEAN,
    # PowerOcean Plus 20kW. It sits between the 15kW `R371` and the 30kW
    # `R374` in the app's device registry, on the same product type and the
    # same `product_smart_re_307` family stem as both. (#205)
    "R372": DEVICE_TYPE_POWEROCEAN,
    "R374": DEVICE_TYPE_POWEROCEAN,
    "HJ3C": DEVICE_TYPE_POWEROCEAN,
    "R351": DEVICE_TYPE_DELTA,
    "R331": DEVICE_TYPE_DELTA,
    "D3M1": DEVICE_TYPE_DELTA3,
    # DELTA 3 Max, the variant below the Max Plus. The app's own device
    # registry puts it in the same product family as the three prefixes
    # above and below: `product_ps_delta_delta_3_m` against `_m_p` for the
    # Max Plus, `_c` for the Classic and the bare stem for the base unit.
    # It has no port priority, which is why it is listed in
    # `_SN_PREFIX_EXCLUDED_KEYS`. (#216)
    "D3N1": DEVICE_TYPE_DELTA3,
    "P321": DEVICE_TYPE_DELTA3,
    # Base DELTA 3 (#182): confirmed from a reporter capture in Enhanced mode.
    # The unit sends the same three frames as a Max Plus (32/2 battery, 32/50
    # BMS, 254/21 status), and the status frame decodes through the existing
    # Delta 3 binding with 22 mapped keys, so it needs routing and nothing else.
    "P231": DEVICE_TYPE_DELTA3,
    "HW52": DEVICE_TYPE_SMARTPLUG,
    # BK-series Stream devices:
    #  - BK01: Stream Micro
    #  - BK11: Stream Ultra
    #  - BK31: Stream AC Pro
    #  - BK41: Stream Max
    #  - BK51: Stream AC
    #  - BK61: Stream Ultra X
    # The Stream Micro (#141) is a grid-tie PV inverter without a battery, so
    # it shares the Stream parser but gets a reduced entity set (see
    # STREAM_MICRO_EXCLUDED_KEYS).
    "BK01": DEVICE_TYPE_STREAM,
    "BK11": DEVICE_TYPE_STREAM,
    "BK31": DEVICE_TYPE_STREAM,
    "BK41": DEVICE_TYPE_STREAM,
    "BK51": DEVICE_TYPE_STREAM,
    "BK61": DEVICE_TYPE_STREAM,
    # STREAM AC 5000 (#177): verified against live hardware in Enhanced mode,
    # cross-checked against the app and an independent Tibber Pulse meter.
    "ES22": DEVICE_TYPE_STREAM_AC5000,
    # STREAM 5000 (#231). Same product type as the ES22 above (396) on a
    # different model number, and a capture from a live unit sends the same
    # four telemetry families: 254/39, 254/40, 32/2 and 32/50. Replayed
    # through the ES22 parser unchanged those frames yield 26 readings that
    # agree across independent message families - the home node total matches
    # the sum of the flow edges feeding it, the grid meter matches the export
    # derived from those edges - so this is the same parser rather than a
    # guess from the shared name. Controls are a separate question and stay
    # off, see STREAM_AC5000_CONTROL_PREFIXES in ../const.py.
    "ES21": DEVICE_TYPE_STREAM_AC5000,
    # PowerStream microinverter (#230, capture from #188). Standard Mode
    # only: the whole device arrives as flat JSON under the `20_1.`
    # namespace, and the reporter capture closes the unit's own power
    # balance exactly, so the field map rests on arithmetic rather than on
    # the field names. Mapping the prefix is what makes the name check below
    # the second line of defence instead of the only one.
    "HW51": DEVICE_TYPE_POWERSTREAM,
}

_SN_PREFIX_DISPLAY_NAMES: dict[str, str] = {
    # A base DELTA 3 reports an empty product name through the app API, same
    # as the BK series below (#182).
    "P231": "DELTA 3",
    "BK01": "Stream Micro",
    "BK11": "Stream Ultra",
    "BK31": "Stream AC Pro",
    "BK41": "Stream Max",
    "BK51": "Stream AC",
    "BK61": "Stream Ultra X",
    "ES22": "STREAM AC 5000",
    "ES21": "STREAM 5000",
}

def get_device_name(product_name: str, sn: str = "") -> str:
    """Return a human-friendly name for the device.

    A serial-prefix-derived name is provided for the device families that
    report an empty product name through the app API: the Stream (BK-series)
    units and the base DELTA 3. For the rest the product name is returned
    when present and
    an empty string otherwise, so callers keep their existing fallback
    (device-type display name or bare serial).
    """
    if product_name:
        return product_name

    if not sn:
        return ""
    base_name = _SN_PREFIX_DISPLAY_NAMES.get(sn[:4].upper(), "")
    if not base_name:
        return ""

    serial_tail = sn[-4:]
    if len(serial_tail) == 4 and serial_tail.isdigit():
        return f"{base_name} ({serial_tail})"
    return base_name


def get_device_type(product_name: str, sn: str = "") -> str:
    """Classify a device based on its SN prefix or productName string.

    Returns DEVICE_TYPE_POWEROCEAN, DEVICE_TYPE_DELTA, DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_SMARTPLUG, DEVICE_TYPE_STREAM, DEVICE_TYPE_STREAM_AC5000,
    DEVICE_TYPE_POWERSTREAM, or DEVICE_TYPE_UNKNOWN.
    """
    # The prefix is exact evidence, the product name a substring guess, so
    # the prefix wins. Every prefix mapped before this ordering existed
    # agreed with its keyword anyway; an ES22 would not, since "STREAM AC
    # 5000" matches the BK-series keyword.
    if sn:
        prefix = sn[:4].upper()
        if prefix in _SN_PREFIX_MAP:
            return _SN_PREFIX_MAP[prefix]

    name = (product_name or "").lower()
    for kw in _POWERSTREAM_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_POWERSTREAM
    for kw in _POWEROCEAN_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_POWEROCEAN
    for kw in _DELTA3_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_DELTA3
    for kw in _DELTA_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_DELTA
    for kw in _SMARTPLUG_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_SMARTPLUG
    for kw in _STREAM_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_STREAM
    return DEVICE_TYPE_UNKNOWN
