# MLB v5.1 100픽 진단

## Executive Summary

67승 33패는 고무적이지만 시즌 승률 기준선과 다른 7경기는 4 대 3에 그쳤다. 추가 예측 우위는 입증되지 않았다. 재구성된 최종 참가자는 실제 T-45 재현성을 입증하지 않는다.

## 지표·모집단·분모 정의

모집단은 2025-05-01~2025-08-12의 봉인된 Daily Top 1 100경기다. Accuracy=W/N; Brier=mean((p-y)^2); Log loss=-mean(y log p+(1-y)log(1-p)); ECE는 고정 5개 구간의 N가중 절대 gap이다. 모든 계산은 봉인된 JSON의 unrounded values를 쓴다.

## 1차 결과

67승 33패, N=100, 적중률 67.00%, Wilson 95% 57.3052%–75.4370%. Brier 0.2213716579, Log loss 0.6351164919, ECE 0.1150816008, 최대 연패 5.

## 짝지은 기준선

### Table contract: `paired_baselines`

- Population: `Daily Top 1 paired games`
- Formula: `paired correctness 2x2; exact McNemar; paired delta`
- Definitions: N=sum of four paired cells; model/baseline wins come from exact paired cells; delta=model accuracy minus baseline accuracy; interval uses z=1.97.
- Exclusions: `season-record tie excluded`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl; data/v5_1_bt/outcomes/baseline.json`

| baseline | N | model | baseline | both correct | model only | baseline only | both wrong | delta | paired 95% interval | McNemar p |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| home | 100 | 67/100 | 59/100 | 52 | 15 | 7 | 26 | +8.00pp | -1.10pp–+17.10pp | 0.1338 |
| D-1 season record | 99 | 66/99 | 65/99 | 62 | 4 | 3 | 30 | +1.0101pp | -4.25pp–+6.27pp | 1.0000 |

The full paired cells are: home both correct 52, model only 15, baseline only 7, both wrong 26; D-1 both correct 62, model only 4, baseline only 3, both wrong 30. Intervals use z=1.97. 시즌승률 tie 1건은 분모에서 제외했다. 시장 N=0이므로 시장 우위나 베팅 성과를 평가하지 않는다.

## 확률 보정과 판별

### Table contract: `calibration`

- Population: `Daily Top 1 N=100`
- Formula: `fixed-bin probability mean, accuracy, gap, Brier contribution`
- Definitions: N=bin count; gap=absolute mean-probability minus actual-accuracy difference; Brier contribution is weighted over all Top 1 picks.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| bin | N | mean probability | actual accuracy | gap | Brier contribution |
| --- | --- | --- | --- | --- | --- |
| 50-54 | 0 | — | — | — | 0.0000000000 |
| 54-58 | 1 | 0.565420 | 0.000000 | 0.565420 | 0.0031969951 |
| 58-63 | 17 | 0.615177 | 0.470588 | 0.144589 | 0.0461498282 |
| 63-68 | 59 | 0.653417 | 0.762712 | 0.109295 | 0.1151687714 |
| 68-100 | 23 | 0.697232 | 0.608696 | 0.088536 | 0.0568560633 |

Probability–outcome Spearman ρ=0.0077, p=0.939; confidence–outcome Spearman ρ=0.0365, p=0.719. ≥63%: 59/82=71.9512%; ≥68%: 14/23=60.8696%. 이 결과는 post-hoc이며 monotonic calibration is not validated; 새 컷이 아니고 이 100건으로 bin과 threshold를 최적화하지 않는다.

## 일별 순위

### Table contract: `rank`

- Population: `graded daily ranks 1-3`
- Formula: `wins and N by immutable daily_rank`
- Definitions: W=correct predictions and N=graded predictions at immutable daily_rank.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| rank | W | N | accuracy |
| --- | --- | --- | --- |
| 1 | 67 | 100 | 67.0000% |
| 2 | 60 | 100 | 60.0000% |
| 3 | 59 | 100 | 59.0000% |

Rank 1 대 2/3 exact McNemar p는 0.349/0.291이다. 순서는 방향성으로 유리하지만 우월성은 입증되지 않았다.

## 시간·팀 집중

### Table contract: `monthly`

- Population: `Daily Top 1 N=100`
- Formula: `wins and N by official_date month`
- Definitions: Month is the actual YYYY-MM official_date key; W/L/N and accuracy use graded Top 1 picks.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| month | W | L | N | accuracy |
| --- | --- | --- | --- | --- |
| 2025-05 | 21 | 10 | 31 | 67.7419% |
| 2025-06 | 22 | 8 | 30 | 73.3333% |
| 2025-07 | 14 | 13 | 27 | 51.8519% |
| 2025-08 | 10 | 2 | 12 | 83.3333% |

7월 대 나머지 Fisher p=0.0589는 불안정성 증거이지 체제 발견이 아니다. home 52/78=66.6667%; away 15/22=68.1818%. 23개 팀, 상위 6개: New York Yankees 14, Philadelphia Phillies 11, Houston Astros 9, San Diego Padres 9, Los Angeles Dodgers 7, Seattle Mariners 7; 합계 57%다. 작은 셀을 지속적 팀 효과로 해석하지 않는다.

### Table contract: `teams`

- Population: `Daily Top 1 N=100`
- Formula: `wins, N, accuracy, Wilson interval by predicted winner`
- Definitions: Team is predicted winner; W/L/N, accuracy and Wilson 95% interval are descriptive small-cell results.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| team | N | W | accuracy | Wilson 95% |
| --- | --- | --- | --- | --- |
| Arizona Diamondbacks | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| Athletics | 1 | 0 | 0.0000% | 0.0000%–79.3457% |
| Atlanta Braves | 3 | 2 | 66.6667% | 20.7655%–93.8510% |
| Boston Red Sox | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| Chicago Cubs | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| Chicago White Sox | 1 | 1 | 100.0000% | 20.6543%–100.0000% |
| Detroit Tigers | 5 | 4 | 80.0000% | 37.5528%–96.3777% |
| Houston Astros | 9 | 4 | 44.4444% | 18.8775%–73.3353% |
| Kansas City Royals | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| Los Angeles Angels | 1 | 0 | 0.0000% | 0.0000%–79.3457% |
| Los Angeles Dodgers | 7 | 4 | 57.1429% | 25.0454%–84.1783% |
| Milwaukee Brewers | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| Minnesota Twins | 6 | 4 | 66.6667% | 29.9988%–90.3231% |
| New York Mets | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| New York Yankees | 14 | 10 | 71.4286% | 45.3505%–88.2788% |
| Philadelphia Phillies | 11 | 10 | 90.9091% | 62.2635%–98.3768% |
| Pittsburgh Pirates | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| San Diego Padres | 9 | 3 | 33.3333% | 12.0582%–64.5803% |
| San Francisco Giants | 5 | 3 | 60.0000% | 23.0720%–88.2382% |
| Seattle Mariners | 7 | 6 | 85.7143% | 48.6865%–97.4321% |
| Tampa Bay Rays | 1 | 0 | 0.0000% | 0.0000%–79.3457% |
| Texas Rangers | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| Toronto Blue Jays | 4 | 4 | 100.0000% | 51.0100%–100.0000% |

## 컴포넌트

### Table contract: `components`

- Population: `Daily Top 1 N=100`
- Formula: `chosen-side signed edge mean by outcome`
- Definitions: Edges are signed for the chosen side; win/loss means split on the observed Top 1 result.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| chosen-side edge | win mean | loss mean |
| --- | --- | --- |
| starter | 14.879 | 13.751 |
| offense | 8.287 | 7.049 |
| bullpen | 11.752 | 14.238 |
| schedule | 1.313 | 2.424 |

불펜 절대 edge 하/상위 절반은 74%/60%, signed 불펜 edge–결과 ρ=-0.151, p=0.135다. prior-season 가설일 뿐이며 가중치·shrinkage·부호·threshold를 바꾸지 않는다.

## 데이터 품질·누수

전체 1,340개 잠금 후보와 Top 1 전부 INCOMPLETE/High/Toss-up이고 수비·환경은 50, quality 0이다. Top 1 completeness는 0.2358031605–0.5769861616, mean 0.4602764334다. Lineup은 RECONSTRUCTED_FINAL_LINEUP, starter는 RECONSTRUCTED_ACTUAL_STARTER로 실제 T-45 snapshot이 아니다. 7,265경기 동일 pool의 α/β는 rolling-origin OOF가 아니다. Feature isolation is PASS: max_history_date <= D-1 and target gamePk Statcast excluded. Locked-row isolation is PASS: locked prediction rows contain no outcome fields and bind exactly to later evaluated prediction fields. 그러나 outcome-join 실행 타임스탬프가 없다. 통계 as-of 집계 상태는 `LIMITATION`이고 잠금→결과 결합 순서는 `NOT_TIMESTAMP_PROVEN`이다. The proven PASS subchecks do not establish outcome-join chronology and do not cure participant-time or source-revision limitations. 참가자는 사후 최종 기록이고 retrieval hash는 재현성을 주지만 과거 DB snapshot은 아니다.

## Source hashes

- `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`: `db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76`
- `data/v5_1_bt/reports/summary.json`: `eea2faa794adb0372a54a6c4c10a7fe443c6f4b123056edb554396dbf3231b49`
- `data/v5_1_bt/outcomes/baseline.json`: `97e97094a7db5144c4e5d86aed2b9d96d178f6277fd2e035526780c9b90aa0e8`
- `data/v5_1_bt/locked/predictions.jsonl`: `bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb`
- `data/v5_1_bt/locked/lock_manifest.json`: `f38e881d7dc3ffff523c0117fa12abebbb71bc7b35624cb14ef4412397dcd26b`
- `audit/v5_1_bt/lock_manifest.json`: `59aa1cbfca3d8eaa9c6f48e88615b9018a3aa828c39ac77e883096be566f4274`
- `audit/v5_1_bt/model_card.json`: `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`
- `audit/v5_1_bt/manual_audit.md`: `814c02d7bb0da9e9cda591d9f9c7681396d7272e8df8fd37a1bbb3f86dd8fc66`
- `config/v5_1_bt.json`: `1bd3008a7bb95b622263c1ce73bb07182c1ffeea39ab1efd1ae38c271cc729d9`
- `data/v5_1_bt/features/candidate_raw_features.jsonl`: `a1817d4a7e50c77f35fbd03f3f79aa8e629e4b686de88b66c8c8d3877924d4a4`

## 권고

v5.2-QD는 예측 구조를 유지하고 데이터 완전성 의미와 OOF 보정만 동결한다. 역사 500과 실제 T-45 prospective ledger를 분리한다.

## 한계와 가정

독립 전향 검증, 시장 우위, ROI, CLV, 수익성, actionable betting 또는 v5.2 예측력 개선을 주장하지 않는다.
