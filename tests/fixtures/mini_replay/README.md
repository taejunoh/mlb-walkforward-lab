# Mini replay fixture

This deterministic fixture has five selection dates. The first three form the
initial replay window; its third Daily Top 1 is `VOID`, and the fourth date is
also `VOID`, so replay must continue through the fifth date. The expected totals
are five Top 1 selections, three graded Top 1 selections, and two voids. May 2 is
a marked doubleheader. The fixture also
contains a neutral 50/50 environment component with zero quality and a lineup
reconstructed from an official box score.

`schedule.json` is seeded through `RawStore` as the immutable raw-schedule
manifest prerequisite for both calibration and feature replay. `games.json`
contains only pre-lock candidate inputs. `initial_outcomes.json` is unavailable
until after the initial prediction lock. Each file under
`extension_outcomes_by_date/` remains unavailable until its cumulative round
lock exists. `outcomes.json` and `extension_outcomes.json` are combined
CLI-contract samples used by standalone stage tests. Tests
also inject nested market/outcome keys into otherwise valid candidates to prove
that leakage validation is recursive rather than limited to top-level fields.
