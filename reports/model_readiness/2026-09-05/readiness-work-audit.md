# 모델 사용 가능성 점검 — 2026-09-05

## 현재 결론

운영 오류, 실제 저표본, 미구현 입력, 독립 검증 자료 부족은 서로 다른 문제다. 코드 테스트 통과는 예측력 통과가 아니다. 이번 점검에서는 기존 모델 카드와 과거 산출물을 변경하지 않았다.

## 확인된 작업과 차단점

| 영역 | 확인된 원인 | 다음 작업 |
|---|---|---|
| 경기 전 상태 | ATL/PHI와 DET/CLE는 Warmup/PW인데 허용목록이 Scheduled/Pre-Game뿐 | 시작 시각 전이라는 기존 제한을 유지하며 Warmup 회귀 수정 |
| 일정 피처 | 공식 doubleHeader=N을 parser가 None으로 버려 명시적 일반경기를 unknown으로 처리 | 명시적 N과 진짜 미상을 구분하는 전달·피처 회귀 수정 |
| 총점 | 9월1일 날짜뿐 아니라 평가 기간/receipt/schema도 고정 | 날짜별 추론과 평가 기간의 의미를 함께 정한 후 변경 |
| 환경·수비 | 의도적 50점/품질0 fallback; 현 로컬 입력 adapter없음 | 과거 시점 재현 가능한 source/adapter 없이는 품질을 올리지 않음 |
| ARI/HOU 선발 | HOU814490 career BF58, 예상이닝 표본3 등 실제 저표본 | 데이터가 없는 것을 수집 오류나 높은 신뢰도로 바꾸지 않음 |
| 시간 분리 보정 | 2022정규화/2023보정/이미 본2025평가 완료 | 같은2025실험 반복 금지; 고정후 새로운 경기의 사전 기록 |
| v5.2 OOF | 선언된 인증source_inventory와 사전RunSpec 피처생산기 부재 | 기존LFG자료 재사용/인증가능성 확인 후 필요한 취득·생산 단계 설계 |

## 기존 실험을 다시 돌리지 않는 이유

기존 고정1,340경기에서 baseline은718승/622패(53.5821%), 후보는717승/623패(53.5075%). Brier0.2462707574→0.2461723072, logloss0.6855527297→0.6853621820로 변화가 작다. 2025결과는 이미 본 자료여서 이 숫자는 회고 비교이지 독립검증 또는 수익성 증명이 아니다. 원본: reports/model_readiness/2026-09-02/temporal_calibration/RESULTS.md 및 evaluate-corrected/evaluation_report.json.

기존 v5.1 sibling의2022/2023/2024 rawfeature 각2,430행은 존재하지만 v5.2의 인증 sourceinventory와 동등하지 않다. 확인한 선언 경로에 superseding source_inventory.json과 sidecar가 없다는 뜻이지 LFG전체에 과거자료가 없다는 뜻이 아니다. 2024 H24는 열지 않았다.

## 이번 회차 검증

- temporal calibration candidate + prospective shadow/report:33 tests, OK.
- live contracts/service + daily totals:89 tests, OK.
- probability calibration + OOF ledger + source gate:48 tests, OK.
- 합계170개 기존 회귀/계약 테스트. 새로운 수정의 RED/GREEN 결과나 모델 성능 검증으로 해석하지 않는다.
- 현재 활성카드SHA256:9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759.
- 디스크 확인시 사용가능약24GiB. 2018–2024전체 재취득은 시작하지 않음.

## 실행과 설계의 경계

이미 승인된 prospective shadow CLI로 오늘9경기의 고정 비교 기록을 실제 경기 전에 완료했다. primary_count=9, pending_count=9, paired_count=0, excluded_count=0; SHADOW_ONLY/NO_PROMOTION. 보고서 reports/prospective_ml_shadow/5f8683602d6e40867b6639606f889d68de1989e9b81a51f2cb7ada00b3c2adf7.json의 실제 SHA256과 sidecar가 일치한다. 결과를 열거나 정산하지 않았다.

새 운영/총점 계약은 docs/superpowers/specs/2026-09-05-daily-evaluation-readiness-design.md의 Proposed 설계로 분리한다. 새 설계가 승인되기 전 production코드·schema·테스트 변경은 하지 않는다. 과거기록, frozen카드, 사용자dirty변경을 보존했다. 완료 직전 기존 tracked diff와 활성카드SHA가 시작시점과 정확히 동일함을 확인했다. git diff --check 통과.
