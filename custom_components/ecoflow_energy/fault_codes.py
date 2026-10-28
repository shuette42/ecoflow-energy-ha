"""Names of the PowerOcean fault codes, looked up in Home Assistant's language.

The names live in the ``selector.fault_code.options`` translations (code as the
key), so they follow the user's language like every other text. They come from
one source only, the list published in EF-PowerOcean-TcpModbus, and are not
checked against EcoFlow's own material (LIKELY, not verified).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

# The sensor key and the parser key of the fault event list.
FAULT_CODES_KEY = "fault_codes"
_PREFIX = f"component.{DOMAIN}.selector.fault_code.options."


def _names_of(translations: dict[str, str]) -> dict[int, str]:
    return {
        int(key[len(_PREFIX) :]): text
        for key, text in translations.items()
        if key.startswith(_PREFIX) and key[len(_PREFIX) :].isdigit()
    }


async def async_load_fault_names(hass: HomeAssistant) -> dict[int, str]:
    """Return ``{code: name}`` in the configured language.

    Home Assistant fills a text a language file lacks from the English file, so
    fr, it, nl and pl show the English name until they are translated.
    """
    return _names_of(
        await async_get_translations(hass, hass.config.language, "selector", {DOMAIN})
    )
