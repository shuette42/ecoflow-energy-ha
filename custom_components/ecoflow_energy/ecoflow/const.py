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
# Checked before every other list. These names contain a keyword from one of
# the lists above as a substring while belonging to a different product line,
# and the match is by substring with no word boundary. "PowerStream" contains
# "stream", so a PowerStream microinverter was classified as a Stream battery
# and given its full entity set, which then stayed empty for good: the device
# connected, reported nothing this parser understands, and settled on stale
# (#188). Landing in DEVICE_TYPE_UNKNOWN instead is the honest outcome - the
# user gets the unsupported-device notice and can contribute a raw capture,
# which is the path that leads to real support.
_NOT_THIS_FAMILY_KEYWORDS = ("powerstream", "power stream")

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
    # European PowerOcean variant (#89): verified against live hardware in
    # Enhanced mode - telemetry matches the EcoFlow app (grid, battery, MPPT).
    "J32D": DEVICE_TYPE_POWEROCEAN,
    # Single-phase European PowerOcean variant (#89): verified via reporter
    # diagnostics in Enhanced mode - live data across grid/battery/MPPT;
    # single-phase unit, so only grid phases A and B carry values.
    "J32E": DEVICE_TYPE_POWEROCEAN,
    # PowerOcean Plus variants (#88): higher-power 3-phase hybrid units
    # (e.g. P3-S1, ~25-30 kW). Not exposed through the Developer API, so
    # Enhanced mode only - same situation as J32D/J32E. Routed to the
    # PowerOcean parser so entities are created; field layout to be
    # confirmed via reporter diagnostics on a Plus unit.
    "R371": DEVICE_TYPE_POWEROCEAN,
    "R374": DEVICE_TYPE_POWEROCEAN,
    "HJ3C": DEVICE_TYPE_POWEROCEAN,
    "R351": DEVICE_TYPE_DELTA,
    "R331": DEVICE_TYPE_DELTA,
    "D3M1": DEVICE_TYPE_DELTA3,
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
    """Classify a device based on its productName string or SN prefix.

    Returns DEVICE_TYPE_POWEROCEAN, DEVICE_TYPE_DELTA, DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_SMARTPLUG, DEVICE_TYPE_STREAM, or DEVICE_TYPE_UNKNOWN.
    """
    name = (product_name or "").lower()
    for kw in _NOT_THIS_FAMILY_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_UNKNOWN
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
    if sn:
        prefix = sn[:4].upper()
        if prefix in _SN_PREFIX_MAP:
            return _SN_PREFIX_MAP[prefix]
    return DEVICE_TYPE_UNKNOWN
