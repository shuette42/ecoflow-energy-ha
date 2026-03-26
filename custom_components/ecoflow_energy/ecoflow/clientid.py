"""EcoFlow Portal WSS MQTT ClientID Generator.

Generates MQTT client IDs for the WSS connection to the EcoFlow broker (port 8084).
Must be called on every connect/reconnect — old client IDs are rejected after disconnect.

Reverse-engineered from: main.9d3dab6f.js (ef-device-user-jt_v2.7.9)
"""

import hashlib
import random
import time
import uuid

# fmt: off
# Lookup table: 32 pairs (appKey, secret)
# Extracted from EcoFlow Portal public JavaScript bundle
BT = [
    ("e80b6010a485434a806e5e531479a37c", "aed8900fd005458cb41a762ffe375e1b"),
    ("c36fba460766448f892fb1d10ec9a887", "2e44ccd5180941f69e305189210658fa"),
    ("2bd767c721c04e9da13580e7865dba10", "64c51fefca384bd3af7a780cd1c19715"),
    ("c71d7eaf92f54d978993b06749b23f8d", "7ae9e4834af54fbda072821b379162cc"),
    ("675ca589c2dd4e0f85559a12d1b8aa0a", "d42c990ed1614b318a1c62d55b2fbcdb"),
    ("6e6f462422644ed9bf86081160d26026", "2c56c937ecf44a86a690a7072ee73809"),
    ("3b7d21606a23480dacfa3616c01d645d", "b38e858e4a6c47f79503ad4f93eb58a0"),
    ("bf8fb28e329649dea068441b6a605742", "921273f0a8e249d388c5aa30ff4b05da"),
    ("273dba38ae724783a22641b2495f7077", "39ec9138f14748d6af4dca88339087fe"),
    ("9675fcfaf74a4eb488992dabd0786073", "e3581d5cb76f447596f50a63f2dc9949"),
    ("87f8d63eaa604149a23e3561b2e06e2b", "2d29de38431146a0bdca289d1f4d146e"),
    ("b3a553c2424e4a0e9083b6ee97f8d538", "47ad0dbd3a7d46b8a0bd9c7f04cceceb"),
    ("79e7410c36354130aecfc714451f43d3", "2d8cdfc12da544fe93b0aa7c3ea5bab8"),
    ("b037d78184f5442a959c70ae92fcb4f4", "888ae575907f4b0eafbd9606520635bc"),
    ("5d0d3683884d48fd9449f5fedddc9725", "abb484e211cc4c58a2b735f0aed09ff5"),
    ("12a0dd6c74c642268c44cf5c9fdd4397", "d7c2668c5f054617bee886c744c818ee"),
    ("687c0db8f1064542a597a4629a81f0fd", "72b9ffe730b84f0e96dabd7164198549"),
    ("0521ba1633e4441c88020ed9640a8bd3", "7014eff3e00b42b7af257c3db8f3bb8d"),
    ("d3053164fde24dbdb5681bddc1fce936", "1beb9d0033974e78acd832ccc8a428eb"),
    ("72d5fe77efa6415db3373e3798e5107b", "1ed248b5d7304cc58c2f8a1ab1e83bc8"),
    ("71a22a54960e4f57aa92d88c6ea390de", "2cb1af3a6bc64d66b20eaf6a86ff6c25"),
    ("3041d0ff3e494b30a3faecc54af6d1a9", "4fc85aef548843d1bcdae60e72ac34f7"),
    ("a2ab845e94924c85898c4c21a9bd29d9", "db3df7955ff749c4b3601088afb39bb1"),
    ("795d8b7fa08b4f6a9174d20548109a58", "3c63b21e0efb414f901436e481cfed4e"),
    ("1f6d87c3efe744f3965f44a2e805f15d", "34ab2574b87b4fd3b10fbb2f4198b1c8"),
    ("ec00f2a6d4d24b57904c07219973c8c0", "f700ab1d9c164635a8db3499f5de692f"),
    ("b41c58a3073a42cc8887fe8839857e2b", "2c6140898c3e48f78f4ed09eee6a78c3"),
    ("102a7727f9564f969f98d3925b5626c5", "5c5190512e204fb69f3d69d7448eb4be"),
    ("3eca576d1bbe488ca40770b4ba15ba6c", "c61e14165d464b13bdb438caf80e240c"),
    ("5c664f634f1344af8084d356a3bbae51", "305685a24d764c57b29a97f690edc788"),
    ("a585293382a24cdab24236d2f4410408", "7f6e7d3bd50b4d6b8030bbed4447edbd"),
    ("1f7a3710538444c08af20618fa06863a", "62d78b3799554080a24814b98492f753"),
]
# fmt: on


def generate_client_id(user_id: str) -> str:
    """Generate a valid MQTT ClientID for EcoFlow Portal WSS (port 8084).

    A new ClientID is required on every connect/reconnect — the broker
    rejects old ClientIDs after disconnect.

    Args:
        user_id: The userId from the login response.

    Returns:
        ClientID in format: WEB_{uuid}_{userId}_{appKey}_{timestamp}_{verifyHash}
    """
    t = random.randint(0, len(BT) - 1)
    app_key, secret = BT[t]
    client_uuid = str(uuid.uuid4())
    timestamp = int(time.time() * 1000)

    base = f"WEB_{client_uuid}_{user_id}"
    hash_input = f"{secret}{base}{timestamp}"
    verify_hash = hashlib.md5(hash_input.encode()).hexdigest().upper()

    return f"{base}_{app_key}_{timestamp}_{verify_hash}"
