"""`powerocean_soc_surplus_field`: which surplus field the pair write sends (#631).

The lookup matches the first four characters of the serial, upper-cased, against
the family prefix `R37`. The frame the coordinator publishes for it is covered
in `tests/ha/test_powerocean_soc_set_fields.py`.
"""

import pytest
from ecoflow_energy.ecoflow.const import powerocean_soc_surplus_field


@pytest.mark.parametrize(
    ("sn", "expected"),
    [
        ("R372-test", "field4"),
        ("R371", "field4"),
        ("R374-test", "field4"),
        # Lower case is read as upper case, and only the first four characters count.
        ("r372x", "field4"),
        # The family is a prefix, not a list: a further R37x unit joins it
        # without a table change, as it does for the schedule power ceiling.
        ("R375-test", "field4"),
        ("HJ31-test", "both"),
        ("HW52-test", "both"),
        # Too short to carry the prefix, and no serial at all.
        ("R3", "both"),
        ("", "both"),
    ],
)
def test_surplus_field_by_serial_prefix(sn: str, expected: str) -> None:
    assert powerocean_soc_surplus_field(sn) == expected


def test_missing_serial_gets_the_default() -> None:
    """A coordinator without a serial must not raise on the way to a write."""
    assert powerocean_soc_surplus_field(None) == "both"  # type: ignore[arg-type]
