# 2026-09-24 사전 저장 ML 승패 정산

공식 MLB Stats API 최종 결과로 사전에 저장된 primary claim 4건을 정산했다. 선택된 승자 예측은 1승 3패(정확도 25%)였다. 통합 Brier score는 0.312991, log loss는 0.828898이다. 표본은 이 날짜의 저장된 primary claim 4경기뿐이므로 모델 품질이나 개선 여부를 입증하지 않는다.

| 경기 (gamePk) | 사전 선택 승자 (선택 확률) | 실제 점수 | 결과 | 입력 completeness |
| --- | --- | --- | --- | ---: |
| Brewers @ Phillies (823411) | Phillies (홈승률 0.5165) | 5–1 | 실패 | 0.601 |
| Guardians @ Red Sox (824707) | Red Sox (홈승률 0.5434) | 1–0 | 실패 | 0.440 |
| Reds @ Braves (824866) | Braves (홈승률 0.7155) | 7–6 | 실패 | 0.613 |
| Rays @ Yankees (823493) | Yankees (홈승률 0.5780) | 4–6 | 적중 | 0.591 |

점수 순서는 원정팀–홈팀이다. primary claim 저장 시각은 앞의 세 경기가 2026-09-24 20:28:51 UTC, Yankees 경기는 20:39:04 UTC다. 이는 claim 저장 시각이지 모델 trace 생성 시각은 아니다. 해당 예측 스냅샷은 앞 경기의 경우 `reports/experimental_daily/2026-09-24/20260924T201347463923Z-a4363ad41e70f92a.trace.json`, 추가 Yankees claim은 `reports/experimental_daily/2026-09-24/20260924T203303766913Z-095c7301fcd8a3f4.trace.json`을 참조한다. 추후 공식 결과는 Stats API `schedule` 응답으로 2026-09-29 20:33:32–20:33:47 UTC 사이에 관측되어 저장·정산되었다. 경기별 요청 URL, 결과 receipt, 저장 스냅샷 및 settlement head 경로와 원본 보고서 참조는 동봉한 JSON에 기록했다.

## 입력·정산 완전성

4경기 모두 정산된 primary claim이다. 홈승률과 사전 선택 방향은 봉인된 스냅샷 그대로이며, 이 정산은 예측을 다시 만들거나 모델을 재적합하지 않았다. completeness 값은 0.440–0.613이었다. 이 숫자만으로 어느 입력 누락이 결과를 만들었는지 식별할 수 없으므로 경기별 인과 설명은 덧붙이지 않았다.

정식 admitted 시장 증거와 베팅 실행 내역이 없어 betting P&L은 계산하지 않았다. 별도 `model-market-review` 및 `public-market-review` 자료는 보존되어 있다. 공개적으로 수집된 가격 자료와 formal admission 여부는 구분한다.

별도 연구 참고로 기존 최초 저장 총점 trace의 7개 pregame projection을 공식 최종 점수와 대조했다. 예측 합계의 평균 절대 오차는 3.276점, RMSE는 3.809점, 저장된 80% 구간은 7경기 중 6경기를 포함했다. 이는 작은 단일 일자 표본의 기술 통계일 뿐이다. 기준점·베팅 자료가 연결되지 않아 공식 O/U 정산이나 베팅수익으로 해석하지 않는다. 나머지 세 경기의 MLB Stats API 응답 원시 게임 요약, 요청 URL 및 관측 UTC 시각은 JSON `saved_totals_diagnostic.games[].result_source`에 보존했다. 기존 총점 예측과 네 primary 경기 결과 receipt는 명시 경로로 참조된다.

## 재현 및 근거

아래는 이미 실행을 마친 두 coordinator CLI 호출의 재현 기록이다. 동일한 수집을 다시 실행하지 않는다. 각각 exit 0 및 `workflow_complete=true`였고, status 재검증은 처리 완료 및 오류 0건을 확인했다.

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_daily_flow postgame --root "$PWD" --pregame-receipt audit/prospective_ml_daily_flow/2026-09-24/6d8dc9b9548f6fe1e51d0314f6892c080efc041ceca17c2f0f6ed710bbdc268a.json
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_daily_flow postgame --root "$PWD" --pregame-receipt audit/prospective_ml_daily_flow/2026-09-24/9109030ceaff7cbd66a7e42e90c931923c178e78b2a7d43647b84a76fbe705f0.json
```

정산 산출물: 3-claim calibration report `reports/prospective_ml_observation/362f048331f16996f2a44410e3c91d728b01f499e2429b1eee9df0cc4548c59f.json` (0승 3패), 1-claim report `reports/prospective_ml_observation/0a6515411a69c3e1a0fdbb8c834cce67201001aaec54d813d68f37a3a3343f19.json` (1승 0패). Coordinator postgame receipts와 네 settlement head 및 공식결과 receipt는 `prospective_ml_settlement.json`의 `postgame_receipts`와 `games`에 기록했다. 기존 `model-market-review.json` 및 `.md`는 수정하지 않았다.
