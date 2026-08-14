# v5.1-BT 수동 감사 기록

## 감사 범위

- 프로토콜: Retrospective Daily Top 1, 모델 `v5.1-BT`, Backtest.
- 최초 100개 MLB 경기일: 2025-05-01~2025-08-12. `VOID` Top 1은 0건이어서 2025-09-28까지의 교체 연장은 사용하지 않았다.
- 모든 잠금 시각은 예정된 첫 투구의 정확히 T-45분이다.
- 아래 3개 날짜에서 경기 참가자, 실제 선발, 잠금 시각, D-1 절단, 예측 및 최종 결과를 원본 산출물과 대조했다.

## 2025-05-01 — gamePk 778097

- 참가자/구장: Kansas City Royals(원정) @ Tampa Bay Rays(홈), George M. Steinbrenner Field.
- 실제 선발: Seth Lugo(607625, KC) / Shane Baz(669358, TB). 두 팀 모두 probable-starter mismatch=`false`; 선발 provenance=`RECONSTRUCTED_ACTUAL_STARTER`.
- 최종 라인업 참가자 ID: KC `[663697, 677951, 686469, 521692, 672580, 686681, 592192, 666023, 671221]`; TB `[802415, 650490, 664040, 666018, 691406, 670224, 608671, 676609, 666163]`.
- 시작/T45: `2025-05-01T17:10:00Z` / `2025-05-01T16:25:00Z`.
- Top 1: Tampa Bay Rays, 확률 0.6193001628, starter edge 12.2696732234, offense edge 1.0866787893, bullpen edge 8.4563961651, completeness 0.3857785317.
- 결과: Kansas City Royals 8-2 승. 예측 실패.
- D-1 절단: max history date와 schedule-used max date 모두 `2025-04-30`; target Statcast는 제외됨(`statcast_target_excluded=true`). D-1 리그 ERA 4.031644004944376.
- 소스 해시: schedule manifest `50c009b7865a1a8f11d5342014f4e6dca986cb210b13ab9ec49326e55d1c8dd8`; D-1 Statcast manifest `ed97c3ddfff25ac32582fc90eb44efe9255d1754612129af83daf7962b653dc4`; D-1 league-ERA manifest `0d68d617401a321ba6121ccc1ceeef0ed40959779a3a15f8353da21ce2fc2794`.

## 2025-06-15 — gamePk 777499

- 참가자/구장: Toronto Blue Jays(원정) @ Philadelphia Phillies(홈), Citizens Bank Park.
- 실제 선발: José Berríos(621244, TOR) / Zack Wheeler(554430, PHI). 두 팀 모두 probable-starter mismatch=`false`; 선발 provenance=`RECONSTRUCTED_ACTUAL_STARTER`.
- 최종 라인업 참가자 ID: TOR `[666182, 665489, 680718, 672386, 665926, 676391, 702176, 683770, 682729]`; PHI `[607208, 656941, 664761, 592206, 596146, 592663, 681082, 805249, 669016]`.
- 시작/T45: `2025-06-15T17:35:00Z` / `2025-06-15T16:50:00Z`.
- Top 1: Philadelphia Phillies, 확률 0.6373237432, starter edge 19.7651012334, offense edge -0.8056385471, bullpen edge 4.0018998301, completeness 0.5123861951.
- 결과: Philadelphia Phillies 11-4 승. 예측 성공.
- D-1 절단: max history date와 schedule-used max date 모두 `2025-06-14`; target Statcast는 제외됨. D-1 리그 ERA 4.027113749398364.
- 소스 해시: schedule manifest `50c009b7865a1a8f11d5342014f4e6dca986cb210b13ab9ec49326e55d1c8dd8`; D-1 Statcast manifest `ce90b30119339820ee466970a00501afee3c35f4e5a27ca9173b1f8f01145edf`; D-1 league-ERA manifest `ede85a508febeb6b08c9f6dde59c94fc5f6931393cb6f324b078b84427ada97c`.

## 2025-08-12 — gamePk 776774

- 참가자/구장: Minnesota Twins(원정) @ New York Yankees(홈), Yankee Stadium.
- 실제 선발: Travis Adams(701519, MIN) / Carlos Rodón(607074, NYY). 두 팀 모두 probable-starter mismatch=`false`; 선발 provenance=`RECONSTRUCTED_ACTUAL_STARTER`.
- 최종 라인업 참가자 ID: MIN `[668885, 621439, 680777, 807712, 668904, 665019, 681508, 666397, 680837]`; NYY `[663757, 592450, 641355, 519317, 700250, 665862, 502671, 641857, 683011]`.
- 시작/T45: `2025-08-12T23:05:00Z` / `2025-08-12T22:20:00Z`.
- Top 1: New York Yankees, 확률 0.6492080217, starter edge 9.6849413444, offense edge 13.8366384135, bullpen edge 6.3146300496, schedule edge 5.0, completeness 0.3668183945.
- 결과: New York Yankees 9-1 승. 예측 성공.
- D-1 절단: max history date와 schedule-used max date 모두 `2025-08-11`; target Statcast는 제외됨. D-1 리그 ERA 4.1289829239223295.
- 소스 해시: schedule manifest `50c009b7865a1a8f11d5342014f4e6dca986cb210b13ab9ec49326e55d1c8dd8`; D-1 Statcast manifest `f7571b3c2b6287bb9b42d72d9a40c887c37b13903cd7dfa6989a528629a7f4c2`; D-1 league-ERA manifest `c6382d44818a8deb6eafc8671ad8a2d34fb54500a302d3ff6cdd4c4a3f36bf3e`.

## 전체 결과

- Top 1: 67승 33패, N=100, 정확도 0.67, Wilson 95% CI `[0.5730516033971139, 0.7543701620610456]`.
- Brier 0.22137165792176453, log loss 0.6351164919136184, ECE 0.11508160079200008, 최대 연패 5.
- Top 3: 186승 114패, N=300, 정확도 0.62.
- 홈팀 baseline: 59/100=0.59. D-1 시즌 승률 상위팀 baseline: 65/99=0.6565656565656566(동률 1경기 제외).
- 시장 비교: paired market N=0이므로 시장 대비 우위는 계산하지 않았다.

## 중요한 제한사항

- 1,340개 잠금 레코드 전부 `INCOMPLETE`, `High` uncertainty, `Toss-up` tier다. defense와 environment가 모두 neutral 50, quality 0이기 때문이다.
- 라인업은 경기 후 확정 라인업을 재구성한 `RECONSTRUCTED_FINAL_LINEUP`이다. 이것은 실제 T-45에 보였던 라인업 스냅샷이 아니다.
- 선발은 `RECONSTRUCTED_ACTUAL_STARTER`다. 참가자 동일성은 보장하지만 실제 당시 probable-starter 스냅샷과 동일하다는 의미는 아니다.
- 시장 스냅샷이 없어 paired market N=0이며, 시장 대비 성능 주장은 할 수 없다.
- 원본 다운로드는 2025-09-28까지 포함하지만 각 경기의 특징 계산은 `max_history_date <= D-1`로 차단했고 target Statcast를 제외했다.
