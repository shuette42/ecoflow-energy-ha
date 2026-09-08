# Moving from another EcoFlow integration

If you already run a different EcoFlow integration in Home Assistant, this page describes what happens when you add this one, what carries over, what does not, and the order of steps that keeps your system working while you switch.

Nothing here asks you to remove anything first. Both integrations are ordinary config entries and they can run side by side for as long as you want. That is deliberate: the safest way to switch is to let the old entities keep filling while you confirm the new ones are correct, and only then move your dashboards over.

## What carries over and what does not

| Thing | Carries over? | Why |
|---|---|---|
| Device discovery | No, but it is quick | You enter your credentials once and your devices are found again. Nothing is read from the other integration. |
| Entity ids | No | Every entity here gets a new id, built by Home Assistant from the device name and the entity name. |
| Long-term statistics | No | Statistics are stored per entity id. New ids start new statistics. The old ones stay where they are. |
| Detailed history | No | Same reason, and it is short-lived on every entity anyway: the recorder purges it after ten days by default. |
| Automations and scripts | No, they need editing | They reference entity ids, so each one has to point at the new id. |
| Dashboards | No, they need editing | Same reason. Cards reference entity ids. |
| Helpers, template sensors, utility meters | No, they need editing | Same reason. A utility meter also restarts its own counting when you re-point it. |
| Energy dashboard slots | No, they need re-selecting | Each slot points at one entity id and has to be pointed at the new one. |
| The device itself and its cloud account | Yes | Nothing on the device or in your EcoFlow account is changed by any of this. |

The reason none of it carries over is worth one sentence, because it also explains why no integration could do better here. An entity's identity in Home Assistant is its unique id, and in this integration a unique id is the device serial number plus an internal key for the reading. That string is specific to this integration, so it can never accidentally collide with another integration's entities, and equally it can never claim them. There is no supported way for one integration to adopt another's entities, and any attempt to fake one would put your history at risk rather than preserve it.

## The recommended sequence

Work through this in order. Steps 1 to 4 change nothing you rely on, so you can stop after any of them and be exactly where you started.

1. **Install this integration and add your devices.** Leave the other integration installed and running. Both will poll or receive data independently.

2. **Let it run for a few hours.** Check that the new entities are filling with plausible values and that they agree with what you already see. Battery state of charge, grid power and PV power are the fastest way to tell.

3. **Compare the readings you actually care about.** Some values are named differently, some are split more finely, and some exist here that did not exist before. The [entity reference](README.md) lists everything each device exposes, so you can find the new home for each old reading before you need it.

4. **Write down the pairs.** For every old entity id that appears in an automation, a dashboard card or the energy dashboard, note the new entity id next to it. This is the only genuinely tedious part of the switch and doing it on paper first makes the rest mechanical. Developer Tools, then States, filtered by your device name, gives you the full list of new ids in one place.

5. **Move the dashboards.** Edit each card and swap the entity. Nothing breaks while you do this, because the old entities still exist and still update.

6. **Move the automations, scripts and helpers.** Same swap. Reload automations afterwards and check that none of them logs an unknown entity.

7. **Re-point the energy dashboard.** See the section below, because this one has a consequence the others do not.

8. **Only now, remove the other integration.** Its entities disappear at that point. The two kinds of recorded data behave differently and it is worth knowing which is which: the detailed history behind a sensor is purged on the recorder's own schedule regardless of any of this, ten days by default, so it is short-lived either way. The long-term statistics that feed the energy dashboard are not deleted when an entity goes away, they stay in the database, but with no entity left to select they are no longer reachable from the dashboard. If you still want to look back through the old figures, the way to keep that possible is to leave the other integration installed for now. There is no rush to this step.

If something in step 2 or 3 does not look right, that is the point to open an issue rather than to continue. A wrong reading found before the migration costs you nothing.

## Renaming entity ids by hand

Home Assistant lets you change an entity id in the entity's settings dialog. It is tempting to rename the new entities to match the old ones so that everything keeps working untouched. This does something useful and something less useful than it looks, and it is worth being precise about which is which.

What it does: from the moment of the rename, the new entity records under the old id. Automations and cards that reference the old id find it again. Future long-term statistics accumulate under the old id.

What it does not do: it does not merge the old recorded history with the new. Home Assistant moves the statistics that belong to the entity being renamed, not the statistics of a different entity that used to carry that id. While the old integration's entity is still present and still owns that id, the rename is refused outright with "Entity with this ID is already registered".

That leaves renaming into an id whose old entity has been removed, and there the two sets of statistics meet: the orphaned ones from the old entity are still in the database under that name, and the rename now files the new entity's figures under the same name. This is not a case worth being adventurous in. If you want the old id back, do it early, before the new entity has accumulated anything you would miss, and check the energy dashboard afterwards rather than assuming.

There is also a cost. An id renamed by hand no longer matches what this integration produces, which makes it harder to answer a question about your setup later, both for you and for anyone helping you. Two situations where the rename is nonetheless the right call: a very large dashboard, where one rename saves editing dozens of cards, and a template sensor chain built on top of a specific id.

If you do rename, do it after the old integration is removed. Disabling its entity is not enough: a disabled entity still holds its id in the registry, so the rename is refused just the same.

## The energy dashboard

The energy dashboard is the one place where the switch has a visible, permanent effect, so it deserves its own step.

Go to Settings, Dashboards, Energy, and re-point every slot your EcoFlow hardware fills. Depending on your setup that is some combination of:

- Grid consumption and grid return
- Solar production, one entry per string or per tracker
- Home battery, both the energy going in and the energy coming out

Pick the new entities in each slot and save.

What happens next is expected and is not a fault: the totals in the energy dashboard restart from the switch-over point. The energy dashboard sums a statistic per entity, and the new entity has no statistic before today. Yesterday and last month keep showing the old figures for as long as the old entity is still there. Your year-to-date total is effectively cut at the day you switched.

Once the old entity is gone, its statistics are not deleted with it, but there is no entity left to select in a dashboard slot, so the old figures are no longer reachable from the energy dashboard. That is the practical reason for step 8 of the sequence above: remove the old integration last, and only when you no longer need to look back through it.

There is no way around this that does not involve editing the statistics database directly, which is not something to do casually and not something this integration can do for you.

One practical suggestion: if you are not in a hurry, switch the energy dashboard at the start of a month or a billing period. The break in the numbers then falls on a boundary you already think in, instead of in the middle of a period you wanted to compare.

## What is genuinely lost

Being plain about it: you lose the recorded history of the readings, and you spend an evening editing references.

The history loss is real and permanent for the energy dashboard totals. Everything else is inconvenience rather than loss. Where a counter is reported by the device itself, it comes back at its true value as soon as the new entity starts reading it, because the device never stopped counting. Where a counter is accumulated inside Home Assistant, it starts again from zero. Automations and dashboards are work, not risk, because you do that work while the old integration is still running and can undo any of it by putting the old entity back.

If you would rather not lose the long-term figures at all, the honest answer is to keep both integrations installed and let the old entities sit there recording nothing further. They cost very little, they keep the old statistics attached to an entity you can still select, and you can remove them a year from now when the comparison no longer matters to you. The detailed history, as opposed to those long-term figures, is a different matter: the recorder purges it after ten days by default on every entity you own, so there was never a year of it to save.
