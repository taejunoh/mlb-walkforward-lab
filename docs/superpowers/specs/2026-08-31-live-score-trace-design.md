# Live score trace, without a model change

## Approved direction and bounded scope

The user approved work toward reproducible predictions, independent evaluation, price-aware decisions, and prospective records, then requested progress before today's games. This first delivery addresses reproducibility only. It does not claim calibrated live probabilities, predictive advantage, betting value, or completed historical/prospective evaluation.

Keep the model card, normalization, feature construction, weights, scoring formula, primary prediction classification, and ranking unchanged. Preserve all previous reports and user files. Work only in the existing LFG live-venue-timezone-fix worktree. No privileged/custody/production access, installations, remote code, push, merge, or PR.

## Design

Capture the already computed raw feature row and scoring outputs during the existing single live scoring loop through an optional list sink. The default service return contract and primary report schema remain unchanged. Include every scored game, not just the top three. Record non-ranked and exact-neutral disposition explicitly; do not fabricate a pick for a neutral score or a score for an unscored game.

Each trace row retains game identity, original feature hash, raw feature values, the actual away/home component scores and game-level qualities returned by the production scorer, signed home-minus-away weighted contributions, the production total edge, home probability, selected side/probability where present, and eventual primary rank/disposition. Raw features preserve the underlying per-side quality information. Do not add another scoring or fetch pass just to capture data.

The CLI writes a sibling `<primary-stem>.trace.json` after the existing primary report/readiness persistence. Its canonical content digest binds the actual primary relative path/content digest and frozen model-card file digest. Research-only and not-a-betting-recommendation labels are explicit. Use existing trusted-root/atomic/canonical persistence conventions; do not overwrite previous artifacts. Surface a sidecar write failure rather than claiming trace completion, preserving the primary report already written.

Small offline verification takes only one stored feature row and the frozen card: verify card/feature/primary/trace digests, call the production scorer, and compare scores, qualities, signed contributions, edge, probability and selection. This verifies scoring reproducibility, not reconstruction of the complete historical source pipeline. Reject non-finite/bool numeric inputs, invalid ranges/keys, identity/digest mismatches, and altered calculations. Floating comparisons use absolute tolerance 1e-12; capture the original edge rather than changing production summation order.

## Acceptance

1. With the optional sink absent or present, otherwise identical service calls produce identical primary payloads and probabilities/ranking.
2. Trace captures all actually scored rows, including scored-not-top-three and exact-neutral rows, while keeping primary semantics.
3. A saved trace plus the matching card reproduces its captured scores/probabilities without network or bulk history loading.
4. Tampered raw values, component contributions, probabilities, card/primary binding, and non-finite numeric values fail verification.
5. CLI integration persists and rereads the sidecar bound to its actual primary; pre-existing primary fixtures remain compatible.
6. Relevant offline tests pass, independent spec and quality review have no unresolved Critical/Important issues, and original model/report digests remain unchanged.

## Explicit deferrals

No odds adapter, betting selection policy, O/U model, retraining, OOF calibration, new historical batch, or general infrastructure refactor in this patch. A fresh pregame live data capture requires the separate requested read-only network authorization. Existing output is historical, not a current lineup/scratch or price check.
