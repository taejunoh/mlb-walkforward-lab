# Outcome-blind post-start input mapping

**Status: `UNEXECUTED_NOT_PILOT_READY`.** The preparation scaffold has not acquired target-date participant data or run the evaluator. A live D−1 history bundle currently violates the evaluator's exact source-binding cardinality contract (91 source bindings versus exactly 4 required), so no safe pilot mapping exists until that contract mismatch receives separate authorized review. Do not invoke the scaffold.

This is a bounded preparation adapter for `mlb_backtest.poststart_research_evaluation`; it does not create prospective claims or read target-game outcomes. The first invocation must specify exactly one `--game-pk` for the Sep 25 pilot. Only after review of that pilot may the date be expanded to the complete R slate and then Sep 26–27.

For target date **D**, use the existing `build_live_history(root, D, frozen_card)` and require its original, unmodified four `SourceBinding` records (`HISTORY_SCHEDULE`, `HISTORY_STATCAST`, `HISTORY_ERA`, `HISTORY_VENUE`), each with `as_of_date == D−1`. Preserve each binding exactly. Serialize only the evaluator's closed D−1 history schema. Pitch/PA rows are allowlisted; `outs_recorded` is derived internal state and omitted. Historical `home_score`/`away_score` remain permissible only in those D−1 rows. Reject any row after D−1, any target-game ID in history, unexpected internal fields, or binding mismatch.

Discover target-day schedule identities through a `fields=`-masked MLB Stats API request containing only date/game/team/venue identity and scheduled time. Require both `officialDate` and the masked schedule occurrence's `gameDate` to match D; a moved/rescheduled occurrence is not attributed to its original date. For each selected game, request `/game/{gamePk}/boxscore` with an explicit field mask limited to team IDs, starting-pitcher IDs, player IDs, and batting orders. The reviewed response contract additionally admits only known harmless metadata (`jerseyNumber`, `parentTeamId`, and `batters`/`bench`/`bullpen` ID lists) plus specifically checked empty metadata containers. No status, scores, winner, result, linescore, play-by-play, or current player/team statistics are requested. Validate paths and value types before mapping; any unreviewed field or nonempty stats/result subtree is a hard failure, never sanitized away.

Map only exact initial batting orders `100` through `900`, in slot order, with nine distinct positive player IDs. Use the first ordered pitcher ID as the starter, requiring a nonempty unique pitcher list. Missing, duplicate, changed, or ambiguous lineup/starter data yields an explicit unscored manifest row; it does not yield an evaluator input. Participant data is explicitly retrospective: record actual source response completion time as `source_retrieved_at_utc` and projection time as `reconstructed_at_utc`; never backdate it to scheduled start.

Build evaluator inputs as follows:

| Evaluator input | Preparation source | Closed output |
| --- | --- | --- |
| D−1 history | `LiveHistoryBundle` | `POSTSTART_RESEARCH_D1_HISTORY_V1`, with exact original history bindings |
| lineup source | masked target-game feed | `POSTSTART_RESEARCH_LINEUP_SOURCE_V1`, only game/team identity, schedule identity, nine IDs per side, two starters, and actual retrieval time |
| participant projection | lineup source + masked date schedule | `POSTSTART_RESEARCH_PROJECTION_V1`, exact evaluator schema; `source_sha256` is SHA-256 of the exact sealed lineup-source bytes |
| career BF | `load_live_career_bf(root, D, [two starters])` | `POSTSTART_RESEARCH_CAREER_BF_V1`, as of D−1; retain returned career bindings separately in the preparation manifest |
| frozen model card | exact configured `audit/v5_1_bt/model_card.json` bytes | evaluator reads that same relative path; authenticate with `load_frozen_live_card_bytes` |

All sealed JSON uses `canonical_json_bytes`; `content_sha256` is the canonical object digest excluding that field. The preparation manifest records target game identities, `READY`/`UNSCORED` plus reasons, exact input relative paths and SHA-256 hashes, masked request/final URLs and raw body hashes, field-mask contract version, actual fetch times, empty-container validation summary, and returned career bindings. Provider raw responses are not persisted. The manifest contains no target results or model metrics. The existing evaluator is invoked separately for each `READY` game's four explicit inputs, after the input set is reviewed.

No acquisition or evaluator invocation is authorized by merely reading this mapping note. The pilot and each subsequent date remain gated on parent review of the script and the previous output.
