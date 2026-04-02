"""EcoFlow API constants and default configuration."""

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
DEVICE_TYPE_SMARTPLUG = "smartplug"
DEVICE_TYPE_UNKNOWN = "unknown"

# Keywords used to classify devices from productName strings
_POWEROCEAN_KEYWORDS = ("powerocean", "power ocean")
_DELTA_KEYWORDS = ("delta",)
_SMARTPLUG_KEYWORDS = ("smart plug", "smartplug")

_SN_PREFIX_MAP = {
    "HJ31": DEVICE_TYPE_POWEROCEAN,
    "HJ32": DEVICE_TYPE_POWEROCEAN,
    "R351": DEVICE_TYPE_DELTA,
    "R331": DEVICE_TYPE_DELTA,
    "HW52": DEVICE_TYPE_SMARTPLUG,
}


def get_device_type(product_name: str, sn: str = "") -> str:
    """Classify a device based on its productName string or SN prefix.

    Returns DEVICE_TYPE_POWEROCEAN, DEVICE_TYPE_DELTA, DEVICE_TYPE_SMARTPLUG,
    or DEVICE_TYPE_UNKNOWN.
    """
    name = product_name.lower()
    for kw in _POWEROCEAN_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_POWEROCEAN
    for kw in _DELTA_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_DELTA
    for kw in _SMARTPLUG_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_SMARTPLUG
    if sn:
        prefix = sn[:4].upper()
        if prefix in _SN_PREFIX_MAP:
            return _SN_PREFIX_MAP[prefix]
    return DEVICE_TYPE_UNKNOWN
