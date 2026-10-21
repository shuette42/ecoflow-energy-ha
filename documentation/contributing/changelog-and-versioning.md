# Changelog and Versioning

The one file every contributor touches, and the one file you must leave alone. Read this before you open a pull request.

## What you do

- Add a line to `CHANGELOG.md` for every change under `custom_components/`. Reason: the changelog is what owners read to learn what changed on their screen.
- Put the line in the topmost version block, under the sub-heading that fits: `### Added`, `### Changed`, `### Fixed` or `### Removed`. Create the sub-heading in that block if it is missing.
- Say in your pull request description if the topmost block is already a published release (compare it with the [Releases](https://github.com/shuette42/ecoflow-energy-ha/releases) page). Write the line anyway, and the maintainer moves it under the next version. Reason: contributors cannot always tell whether a release has shipped, and the maintainer sets the target version.
- Do not create a new version block yourself. Reason: there is one block per target version, and it grows until that version is released.

## How to write the line

- Write for the owner of the device, not for whoever reviews your pull request. Say what changes on their screen, which device it applies to, and how it was confirmed. Reason: the owner decides whether to update, and a line about internals tells them nothing.
- Name the device by its serial prefix in backticks, for example the Ocean 2 Plus 8 kW (`RE41`). Reason: the prefix is how users and the code tell models apart. Use the first four characters only and never a full serial.
- Say what was confirmed and on how much evidence. "Confirmed on one unit" is a result, "should work" is not. When a part is untested, say so in the line.
- End the line with the issue reference in the form `(Ref #N)`. Reason: it links the entry to the discussion without closing the issue.
- Credit the person whose work carried the change. When a capture, a diagnostics download or a hardware test from another user made the change possible, name them with `Thanks to @name for ...` and say what their work showed. Reason: their evidence is what the entry rests on.
- Keep to plain hyphens and commas. Do not use en or em dashes. Reason: the documentation tests reject them.

## Issue references

- Write `Ref #N` in commit messages and in the pull request description. Do not write `Closes #N`, `Fixes #N` or `Resolves #N`. Reason: a closing keyword closes the issue when the pull request merges, before the reporter has confirmed anything.
- Be careful with a sentence that denies a close, such as "this does not close #7". GitHub reads the keyword and ignores the "not". Write "#7 stays open" instead.
- Use a commit subject that starts with the kind of change: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:` or `refactor:`, then a short sentence and the pull request number in brackets. Example: `fix: raise the RE41 Backup Reserve ceiling together with the reserve (#604)`.
- Write commit messages and pull request text in English.

## What you leave alone

- Do not edit `custom_components/ecoflow_energy/manifest.json`. Reason: the maintainer sets the version when the change is scheduled, and a contributor bump collides with the version of the running release cycle. A device pull request that adds support leaves the version alone. The [device checklist](../add-your-device.md) says the same.

## Why versions work the way they do

- A full release has the form `MAJOR.MINOR.PATCH`. A pre-release adds `-beta.N`, visible in HACS only with beta versions switched on.
- During a pre-release cycle only the beta number goes up. The `MAJOR.MINOR.PATCH` part stays at the target version, `manifest.json` shows that target without the `-beta` suffix, and the changelog keeps one growing block for it (ADR-005 in the [decision register](../architecture/decisions.md)). Reason: a bump per fix creates versions that were never released.
- The patch number increases only after a full release exists, for a fix to that release.

## Changes that need no version

- A change that touches only documentation, or only `tests/`, needs no version change. Reason: nothing the integration does changes for the user. Two past examples: a documentation change that corrected the README and one existing changelog entry left `manifest.json` alone, and a pull request that only added test fixtures touched neither the changelog nor the manifest.
- If you are unsure whether your change counts as one of these, ask in the issue before you push. Reason: the maintainer decides, and an answer in the thread saves a round of review.
