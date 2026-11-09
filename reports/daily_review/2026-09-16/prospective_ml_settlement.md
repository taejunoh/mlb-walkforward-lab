# 2026-09-16 사전 저장 ML 승패 정산

저장된 primary claim 4건을 공식 최종 결과와 대조했다. 예측 승자는 1승 3패(정확도 25%)였고, 봉인된 홈승률을 기준으로 Brier score는 0.245561, log loss는 0.682516이다. 표본은 이 날짜의 primary claim 4경기뿐이므로 모델 품질이나 개선 여부를 입증하지 않는다.

| 경기 (gamePk) | 사전 선택 승자 (선택 확률) | 실제 점수 (원정–홈) | 결과 | 입력 completeness |
| --- | --- | --- | --- | ---: |
| White Sox @ Guardians (824382) | Guardians (홈승률 0.6510) | 3–6 | 적중 | 0.584 |
| Giants @ Cardinals (823004) | Cardinals (홈승률 0.5289) | 6–5 | 실패 | 0.432 |
| Yankees @ Twins (823655) | Yankees (원정승률 0.5504) | 4–5 | 실패 | 0.542 |
| Tigers @ Blue Jays (822763) | Tigers (원정승률 0.5270) | 1–5 | 실패 | 0.576 |

예측은 각 게임의 2026-09-16 사전 스냅샷에 저장된 값 그대로 정산했다. ML 보고서 trace 실행 시각은 2026-09-16 16:43:15.059750 UTC, 네 후보 스냅샷의 관측 시각은 16:48:00.010502 UTC다. 그에 대응하는 primary claim 저장 시각은 16:48:00.152446, 16:48:00.173135, 16:48:00.191346, 16:48:00.216204 UTC다. 시각과 경기별 예측·입력 근거는 동봉 JSON `games[]`에 각 claim, snapshot, 참가자 receipt/payload 및 settlement head 경로와 함께 기록했다. 최종 결과는 공식 Stats API 응답에서 2026-09-30 11:43:38–11:43:44 UTC에 관측되었다.

## 입력·정산 완전성

세 pregame receipt 중 두 건(`4460f407…` 및 `a1751cdc…`)에는 primary claim 참조가 하나도 없다. 따라서 정산 대상이 없으며, 이 두 receipt에는 적중률을 계산하지 않는다. 나머지 receipt에는 primary claim 4건이 있었고, postgame receipt는 `workflow_complete=true`, `errors=[]`로 네 건 모두 FINAL 정산을 기록한다. 이 검토 이전의 settlement head는 없었다.

일자별 15개 receipt/result 행 중 primary claim 4건이 아닌 나머지 11개(gamePk: 822926, 823332, 824467, 822680, 823576, 824626, 822846, 824140, 824306, 823979, 825031)는 `NOT_PRIMARY`로 기록한다. 이들은 예측 실패나 정산된 primary 표본이 아니다.

네 예측의 입력 completeness는 0.432–0.584였다. 이 숫자로 결과 원인이나 누락 입력의 인과 효과를 판별할 수 없어 경기별 사후 원인 설명을 덧붙이지 않았다. 재예측·재학습은 하지 않았다. 정식 admitted 시장 증거와 베팅 실행 내역이 없어 betting P&L도 산출하지 않았다.

## 저장 총점 예측의 별도 진단

첫 저장 총점 보고서의 `PROJECTED` 15행을 공식 2026-09-16 최종 결과와 gamePk 및 원정·홈 팀 ID로 대조했다. 결과 캡처는 2026-09-30 11:48:28 UTC에 저장되었으며 15경기는 모두 FINAL이고 동점이 아니었다. 15행 모두 저장된 예측의 경기 시작 시각은 보고서 시각(2026-09-16 12:50:18.654611 UTC) 이후였다.

이 제한된 단일 일자 비교에서 총점 평균 절대 오차는 1.609점, RMSE는 2.170점, 저장된 80% 구간 포함은 15/15였다. 이는 기술적 진단치일 뿐 공식 O/U 정산, 베팅 성과 또는 모델 개선 증거가 아니다.

| 경기 (gamePk) | 예측 합계 | 실제 합계 | 절대 오차 | 저장 80% 구간 | 포함 |
| --- | ---: | ---: | ---: | ---: | :---: |
| White Sox @ Guardians (824382) | 8.902 | 9 | 0.098 | 3–15 | 예 |
| Giants @ Cardinals (823004) | 9.944 | 11 | 1.056 | 4–16 | 예 |
| Yankees @ Twins (823655) | 9.042 | 9 | 0.042 | 3–15 | 예 |
| Tigers @ Blue Jays (822763) | 8.408 | 6 | 2.408 | 3–14 | 예 |
| Athletics @ Rays (822926) | 9.031 | 7 | 2.031 | 3–15 | 예 |
| Brewers @ Pirates (823332) | 8.943 | 9 | 0.057 | 3–15 | 예 |
| Dodgers @ Reds (824467) | 9.523 | 8 | 1.523 | 4–15 | 예 |
| Phillies @ Nationals (822680) | 9.070 | 3 | 6.070 | 3–15 | 예 |
| Orioles @ Mets (823576) | 8.671 | 8 | 0.671 | 3–14 | 예 |
| Braves @ Cubs (824626) | 9.373 | 12 | 2.627 | 4–15 | 예 |
| Red Sox @ Rangers (822846) | 8.630 | 10 | 1.370 | 3–14 | 예 |
| Royals @ Astros (824140) | 8.546 | 7 | 1.546 | 3–14 | 예 |
| Padres @ Rockies (824306) | 10.050 | 12 | 1.950 | 4–16 | 예 |
| Mariners @ Angels (823979) | 8.487 | 9 | 0.513 | 3–14 | 예 |
| Marlins @ Diamondbacks (825031) | 9.171 | 7 | 2.171 | 4–15 | 예 |

총점 예측 trace·보고서·원본 report hash 및 공식 결과 캡처 경로와 해시는 JSON `saved_totals_diagnostic`에 보존했다. 비교에 쓴 공식 최종 결과 전체 목록은 지정된 캡처 파일이며, ML 승패 정산은 네 claim 각각의 기존 결과 receipt와 settlement head를 참조한다.

## 근거 및 한계

기준 ML 보고서는 `reports/prospective_ml_observation/c9d13305d1446a474f1f5a0eafb46ea03a7dd0e14164dd9425c8dbf957e4ba05.json`, shadow 비교 보고서는 `reports/prospective_ml_shadow/eecf6b183b46769a0def327d8e02de1877d698ea8afbb487a9bab7f7019f7b26.json`이다. 본 정산에서는 기준 primary 4건만 사용했다. 네 봉인 확률과 결과 라벨로 Brier 및 log loss를 독립 재계산했으며 기준 보고서의 반올림 값과 일치한다. 원본 결과 수집을 다시 실행하지 않았다.
