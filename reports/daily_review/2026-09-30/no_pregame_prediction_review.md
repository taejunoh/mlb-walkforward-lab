# 2026-09-30 사전 예측 커버리지 공백

**상태:** `PARTIAL_NO_PREGAME_PREDICTION` — PHI@ATL(849841)의 예약 시작 cutoff가 지났으나 저장된 사전 예측이 없는 커버리지 전용 기록입니다. 정식 정산이 아닙니다.

부모가 제공한 score-free 공식 일정 관측(2026-09-30 18:20:50 UTC)에서 849841은 `In Progress`로 표시되었습니다. 실제 첫 투구의 정확한 시각은 확인되지 않았습니다. 경기 결과·점수·승자는 요청하거나 수집하지 않았습니다.

| 경기 (gamePk) | 시작 (UTC) | 관측 상태 | 사전 예측 저장 | 커버리지 |
| --- | --- | --- | --- | --- |
| PHI @ ATL (849841) | 2026-09-30 18:00 | In Progress | 아니오 | 새 cutoff 누락 |
| CWS @ HOU (849846) | 2026-09-30 21:00 | Pre-Game | 아니오 | cutoff 미도래 |
| BOS @ NYY (849848) | 2026-10-01 00:00 | Scheduled | 아니오 | cutoff 미도래 |
| CHC @ SD (849842) | 2026-10-01 02:00 | Scheduled | 아니오 | cutoff 미도래 |

## 해석

현재까지 4경기 중 1경기의 예정 cutoff가 지났고, PHI@ATL에 저장된 사전 예측은 없습니다. 이를 모델 `PASS`, 적중 실패, 0% 정확도 또는 정식 경기 결과로 간주하지 않습니다. 검증 표본 추가는 0건이며 accuracy, Brier score, log loss, P&L 및 총점 진단은 모두 해당 없음(`null`)입니다. 남은 3경기는 예정 또는 경기 전 상태입니다.

기존 postseason 일정 중복-ID 검증 실패와 post-start 입력의 4개 대 91개 이력 source-binding 불일치는 그대로 유지되며, 변경 없이 재시도하지 않았습니다. 예측·post-start 평가·정산 실행, 소스 수정 및 결과 수집은 없습니다. 이전 정산/receipt inventory는 Sep16=4, Sep23=16, Sep24=4로 변경되지 않았습니다.

## 근거

- 일정/status 출처: [MLB StatsAPI schedule](https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2026-09-29&endDate=2026-10-01&fields=totalGames,dates,date,games,gamePk,gameType,gameDate,status,abstractGameState,detailedState,codedGameState,teams,away,home,team,id,name) — 부모가 확인한 읽기 전용 score-free 관측, `2026-09-30T18:20:50Z`.
- 현행 운영 점검 기록: `reports/daily_review/2026-09-30/operations-check-20260930T182003Z.json`.
- 이전 운영 점검 기록: `reports/daily_review/2026-09-30/operations-check-20260930T175003Z.json`.

운영 기록 파일은 `2026-09-30T18:27:22Z`에 작성되었습니다.
