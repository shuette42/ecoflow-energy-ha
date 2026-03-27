"""EcoFlow API constants and default configuration."""

# MQTT Broker
MQTT_HOST = "mqtt-e.ecoflow.com"
MQTT_PORT_TCP = 8883
MQTT_PORT_WSS = 8084
MQTT_WSS_PATH = "/mqtt"

# Default MQTT settings
DEFAULT_MQTT_KEEPALIVE = 120
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
