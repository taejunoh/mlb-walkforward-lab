# Live history verification ID order

The user approved this correction after the recorded diagnosis in `live-model-attempts/2026-08-31/missing-coordinates-result.md` and the explanation in chat. Keep live feature rows chronological. Sort only `expected_game_pks` numerically before constructing `ScheduleHistoryBuild`. Do not weaken the sorted/unique/positive contract or silently deduplicate at this boundary.

The cached official schedule and selected Statcast contain game 823244 on 2026-03-25 before game 823081 on 2026-03-26. Date order is therefore not numeric ID order. The alternatives, changing feature row order or weakening the shared validator, alter unrelated behavior and are outside this repair.

Acceptance: a regression with those two dates/IDs fails before the fix and passes after it, retaining chronological rows and independently asserting numeric-sorted IDs. Related history and real fixture E2E modules pass. Feature formulas, weights, model card and source evidence are unchanged.

Use only the existing LFG isolated worktree. Preserve source caches, prior failures and user changes; no Documents access, sudo, dependency install, push, merge, PR, custody/production/Task12/Task14 work, or forbidden manifest access. After review, the controller may run one current-day model attempt using existing caches and already-authorized official MLB/Savant inputs. No duplicate or automatic retry; report the actual outcome.
