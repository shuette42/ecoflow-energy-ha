"""Stream (BK-series) SET command bodies for the IoT Developer API.

Core library module: no Home Assistant imports. The signed HTTP PUT adds the
device serial (`sn`) itself, so the bodies here carry everything else.
"""

from __future__ import annotations

from typing import Any

# Source: EcoFlow IoT Developer API, Stream "Feed-in control". `cfgFeedGridMode`
# takes 2 to allow feeding into the grid and 1 to stop it; the same 2 / 1 come
# back in the quota key `feedGridMode`.
FEED_IN_ON = 2
FEED_IN_OFF = 1


def build_feed_in_control_command(turn_on: bool) -> dict[str, Any]:
    """Return the quota SET body for the Stream Feed-in control switch."""
    return {
        "cmdId": 17,
        "cmdFunc": 254,
        "dirDest": 1,
        "dirSrc": 1,
        "dest": 2,
        "needAck": True,
        "params": {"cfgFeedGridMode": FEED_IN_ON if turn_on else FEED_IN_OFF},
    }
