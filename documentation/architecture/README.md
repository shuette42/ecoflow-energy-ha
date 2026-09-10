# Architecture

Design decisions behind the EcoFlow Energy integration, written down so that a
reader with the repository and nothing else can follow why the code is shaped
the way it is.

- [decisions.md](decisions.md) - the architecture decision register. One entry
  per decision (`ADR-NNN`), oldest first, each with its context, the decision,
  the trade-offs, the alternatives that were rejected and why, and the
  consequences for the code. Code comments and tests cite these numbers.

## How to read the register

Every entry keeps its number for good, so a citation such as `ADR-024` in a
docstring stays valid. A few numbers are not in the register: they belong to
decisions about the maintainer's own working setup rather than the product, or
were never used. The register lists them in one sentence near the top so a
cited number can always be placed.

A decision is not rewritten when it is superseded. The later decision says
what it changes and the earlier one gets an addendum pointing forward, so the
history of a choice stays readable.

## Where evidence lives

The register states measurements and their results. The raw material behind
them (device captures, reporter downloads, one-off analysis scripts) is not
part of the repository: it identifies the people who shared it. The register
therefore says what was measured and what came out, not where the file sits.
