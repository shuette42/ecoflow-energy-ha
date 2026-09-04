# Solar Tracker - Entity Reference

Full list of all entities created for the EcoFlow Solar Tracker.

**Totals:** 6 sensors

The tracker reports through the account connection only, so it needs **Enhanced Mode**.

One product answers under two serial prefixes: `HZ31` on the module serial and `S02F` on the kit serial. Both are the same device and both are recognised. If you own two trackers you may well see one of each.

Read-only in this release. Setting the angle and switching between manual and automatic follow in a later one; the reporter has both commands measured on his own hardware.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Tilt Angle | ° | - | enabled | The angle the panel currently stands at |
| Target Angle | ° | - | enabled | The angle the tracker is moving towards |
| Optimal Angle | ° | - | enabled | The angle the tracker calculates as best right now. Reads unknown while the device reports no figure |
| Light Level | - | - | enabled | The brightness reading the tracker uses. See the note below on why it carries no unit |
| Mode | - | - | enabled | `manual` or `auto` |
| Battery | % | - | enabled | Charge of the tracker's own battery |

---

## Notes

### The angles read the same as the app

The device reports every angle ten degrees below the value the EcoFlow app shows, and the integration adds those ten degrees back. A raw 0 is 10°, a raw 75 is 85°. Verified against the app on two trackers.

### Light Level has no unit

The vendor's own schema calls the field lux, and the numbers do not behave like lux: the highest reading on file is over 1.4 million, roughly ten times the brightest daylight ever measured. Whatever scale the device uses, it is not the one the name suggests, so the value is published as the plain number the tracker sends. Giving it a unit would be a claim the data does not support, and units are hard to take back once history has been recorded under them.

### Optimal Angle can read unknown

When the tracker has no calculated optimum, it sends the field filled with the largest number it can express. That is the device saying it has no answer, not an angle, so the sensor reads unknown rather than a value nobody could act on.

### What is deliberately absent

Several fields the device sends are not published, because no recording settles what they mean:

- The battery temperature, whose scale is unconfirmed
- A counter that changes as the tracker moves, which two separate readings showed is not the mode it was first taken for
- Seven further fields the reporter himself grouped as medium or low confidence

They stay out until a recording settles them. A sensor that shows a number nobody can explain is worse than an absent one.

### Devices that have stopped reporting

A tracker that is registered on the account but no longer sends anything appears as a device without values, the same as any other account device in that state. Nothing is wrong with the integration; the device is simply silent.

---

## Evidence

The field map comes from @gabbo99g-creator, who runs two trackers and supplied the recordings, the cross-checks against the app on both units, and the measurement that showed the mode field was a movement counter rather than the selected mode. See [#339](https://github.com/shuette42/ecoflow-energy-ha/issues/339).
