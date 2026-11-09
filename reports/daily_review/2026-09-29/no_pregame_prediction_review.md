# 2026-09-29 사전 예측 커버리지 공백

**상태:** `NO_PREGAME_PREDICTION` — 커버리지 전용 운영 오류 기록이며, 정식 결과 수집·정산이 아니다.

공식 StatsAPI 일정 상태를 2026-09-30 05:05:27 UTC에 확인했을 때 대상 4경기 모두 `Final`이었다. 사전에 저장된 예측과 primary claim은 0건이며, 검증 표본도 추가되지 않았다.

| 경기 (gamePk) | gameType | 시작 (UTC) | 공식 상태 | 사전 예측 저장 |
| --- | --- | --- | --- | --- |
| PHI @ ATL (849845) | F | 2026-09-29 18:00 | Final | 아니오 |
| CWS @ HOU (849849) | F | 2026-09-29 21:00 | Final | 아니오 |
| BOS @ NYY (849851) | F | 2026-09-30 00:00 | Final | 아니오 |
| CHC @ SD (849843) | F | 2026-09-30 02:00 | Final | 아니오 |

## 원인과 해석

기존 R-only workflow가 지원하지 않는 postseason `gameType F`여서 예측 커버리지가 만들어지지 않았다. 이는 모델 `PASS`가 아니며, 라인업 문제로 원인을 돌리지 않는다. 사전 저장 예측이 없으므로 정확도, Brier score, log loss, P&L 및 총점 진단은 모두 해당 없음(`null`)이다. 이를 0% 적중률이나 4패로 해석하지 않는다.

결과 조회에 사용한 StatsAPI 요청은 일정·경기 상태 필드만 요청했으며 점수나 승자는 수집하지 않았다. 따라서 본 문서는 결과 정산서가 아니다.

## 입력 및 조치 완전성

- `reports/experimental_daily/2026-09-29`: 없음
- `reports/experimental_daily_totals/2026-09-29`: 없음
- `audit/prospective_ml_daily_flow/2026-09-29`: 없음
- 새 primary claim 0건, validation sample 추가 0건
- 소급 예측 생성, 모델 재적합, 소스 코드 변경 없음

## 근거

- 공식 일정/상태 조회: [MLB StatsAPI schedule](https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2026-09-29&endDate=2026-10-01&fields=totalGames,dates,date,games,gamePk,gameType,gameDate,status,abstractGameState,detailedState,codedGameState,teams,away,home,team,id,name) — 읽기 전용 GET, 관측 시각 `2026-09-30T05:05:27Z`.
- 현행 운영 점검 기록: `reports/daily_review/2026-09-30/operations-check-20260930T050446Z.json`.
- 이전 운영 점검 기록: `reports/daily_review/2026-09-29/operations-check-20260930T020446Z.json`.
