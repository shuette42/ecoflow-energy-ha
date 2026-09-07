"""Pin the runtime MRO the `_Base` idiom of ADR-022 must never change.

ADR-022 decision 1 makes every coordinator mixin inherit a declaration-only
base bound under `if TYPE_CHECKING:` and bound to `object` at runtime:

    if TYPE_CHECKING:
        from ._typing import CoordinatorState as _Base
    else:
        _Base = object


    class SetCommandsMixin(_Base):

The whole behaviour-neutrality claim of that idiom rests on one fact: at
runtime `_Base is object`, so `class Foo(object)` produces the identical MRO
to `class Foo`, and `EcoFlowDeviceCoordinator.__mro__` is exactly what it was
before `_typing.py` existed. That claim is an assertion about the interpreter,
not about the source text, so it is pinned here rather than argued about - a
later attempt to make the base real (ADR-022 alternative (iii), rejected
because it inserts `CoordinatorState` between `AvailabilityMixin` and
`DataUpdateCoordinator` and moves where every `super()` call in that chain
lands) turns this test red at the point of the attempt.

The same idiom applies to the four config-flow mixins (ADR-022 decision 2),
binding directly to `ConfigFlow`/`OptionsFlow` instead of a declaration file,
so their MRO is pinned too.
"""

from __future__ import annotations

from custom_components.ecoflow_energy.config_flow import (
    EcoFlowEnergyConfigFlow,
    EcoFlowOptionsFlow,
)
from custom_components.ecoflow_energy.coordinator.core import (
    EcoFlowDeviceCoordinator,
)

# Our own classes, in the exact composition order declared at
# `coordinator/core.py:101`. Pinned as an exact prefix: we own every one of
# these names and a reorder or a new mixin inserted out of place is exactly
# the kind of change this test exists to catch.
COORDINATOR_OWN_PREFIX = (
    "EcoFlowDeviceCoordinator",
    "SetupMixin",
    "CredentialsMixin",
    "KeepaliveMixin",
    "MqttIngestMixin",
    "StateApplyMixin",
    "SetCommandsMixin",
    "HttpPollMixin",
    "AvailabilityMixin",
)

CONFIG_FLOW_OWN_PREFIX = (
    "EcoFlowEnergyConfigFlow",
    "SetupFlowMixin",
    "ReauthFlowMixin",
    "ReconfigureFlowMixin",
)

OPTIONS_FLOW_OWN_PREFIX = (
    "EcoFlowOptionsFlow",
    "OptionsFlowMixin",
)


def _mro_names(cls: type) -> tuple[str, ...]:
    return tuple(klass.__name__ for klass in cls.__mro__)


def test_coordinator_mro_own_prefix_is_unchanged():
    """The nine classes we own appear, in order, before Home Assistant's own.

    The tail beyond `DataUpdateCoordinator` (`BaseDataUpdateCoordinatorProtocol`,
    `Protocol`, `Generic`, `object`) belongs to Home Assistant and `typing`,
    not to us - a future HA release may legitimately restructure it, so this
    test asserts our own prefix exactly and only checks that the framework
    tail still contains the coordinator base we actually chain into, rather
    than pinning names we do not own.
    """
    mro = _mro_names(EcoFlowDeviceCoordinator)

    assert mro[: len(COORDINATOR_OWN_PREFIX)] == COORDINATOR_OWN_PREFIX, (
        f"EcoFlowDeviceCoordinator.__mro__ starts with {mro[: len(COORDINATOR_OWN_PREFIX)]!r}, "
        f"expected {COORDINATOR_OWN_PREFIX!r} - the mixin composition order at "
        "coordinator/core.py:101 (or one mixin's own bases) has changed"
    )

    tail = mro[len(COORDINATOR_OWN_PREFIX) :]
    assert "DataUpdateCoordinator" in tail, (
        f"EcoFlowDeviceCoordinator.__mro__ tail {tail!r} no longer contains "
        "DataUpdateCoordinator - the coordinator no longer chains into Home "
        "Assistant's own base class"
    )


def test_coordinator_state_is_absent_from_the_runtime_mro():
    """`CoordinatorState` must never appear on the real class.

    This is the one fact ADR-022 decision 1 depends on: `_Base` is bound to
    `object` at runtime in every mixin, so `CoordinatorState` - the
    declaration-only base imported only under `TYPE_CHECKING` - must never
    show up in `EcoFlowDeviceCoordinator.__mro__`. If it does, the `_Base`
    idiom stopped being a type-checking fiction and started being a real
    base class, which is exactly the change ADR-022 rejected as alternative
    (iii): it inserts `CoordinatorState` between `AvailabilityMixin` and
    `DataUpdateCoordinator` and moves where every `super()` call in that
    chain resolves.
    """
    mro = _mro_names(EcoFlowDeviceCoordinator)
    assert "CoordinatorState" not in mro, (
        f"EcoFlowDeviceCoordinator.__mro__ contains CoordinatorState: {mro!r} - "
        "the declaration-only base from coordinator/_typing.py has leaked "
        "into the runtime MRO, which changes coordinator behaviour"
    )


def test_config_flow_mro_own_prefix_is_unchanged_and_config_flow_is_not_doubled():
    """The config-flow mixins must not have gained a doubled framework class.

    ADR-022 decision 2 binds each flow mixin's `_Base` directly to
    `ConfigFlow`/`OptionsFlow` under `TYPE_CHECKING`, not to a declaration
    file. At runtime `_Base` is still `object`, so `ConfigFlow` must appear
    exactly once in the final MRO - a second occurrence would mean the
    type-checking fiction and the real inheritance chain have merged.
    """
    mro = _mro_names(EcoFlowEnergyConfigFlow)

    assert mro[: len(CONFIG_FLOW_OWN_PREFIX)] == CONFIG_FLOW_OWN_PREFIX, (
        f"EcoFlowEnergyConfigFlow.__mro__ starts with "
        f"{mro[: len(CONFIG_FLOW_OWN_PREFIX)]!r}, expected {CONFIG_FLOW_OWN_PREFIX!r}"
    )
    assert mro.count("ConfigFlow") == 1, (
        f"EcoFlowEnergyConfigFlow.__mro__ contains ConfigFlow {mro.count('ConfigFlow')} "
        f"times: {mro!r} - expected exactly once"
    )
    assert "CoordinatorState" not in mro, (
        "EcoFlowEnergyConfigFlow.__mro__ must not contain CoordinatorState - "
        "the flow mixins bind their own _Base to ConfigFlow, not to the "
        "coordinator's declaration file"
    )


def test_options_flow_mro_own_prefix_is_unchanged_and_options_flow_is_not_doubled():
    """Same guard as the config flow, for `EcoFlowOptionsFlow` / `OptionsFlow`."""
    mro = _mro_names(EcoFlowOptionsFlow)

    assert mro[: len(OPTIONS_FLOW_OWN_PREFIX)] == OPTIONS_FLOW_OWN_PREFIX, (
        f"EcoFlowOptionsFlow.__mro__ starts with "
        f"{mro[: len(OPTIONS_FLOW_OWN_PREFIX)]!r}, expected {OPTIONS_FLOW_OWN_PREFIX!r}"
    )
    assert mro.count("OptionsFlow") == 1, (
        f"EcoFlowOptionsFlow.__mro__ contains OptionsFlow {mro.count('OptionsFlow')} "
        f"times: {mro!r} - expected exactly once"
    )
    assert "CoordinatorState" not in mro, (
        "EcoFlowOptionsFlow.__mro__ must not contain CoordinatorState - "
        "the flow mixins bind their own _Base to OptionsFlow, not to the "
        "coordinator's declaration file"
    )
