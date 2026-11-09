# 9월 1~30일 분석 현황

확인 시각: 2026-09-30T15:47:02Z. **전체 분석 완료가 아니라, 기존 예측 검증 완료 + 누락 경기 재현 차단 상태입니다.**

기존 보고서·trace 33쌍을 검증했습니다. 저장된 행 121개, 중복을 제외한 경기 예측 89개이며 검증 오류는 0개입니다. 기존 예측과 primary claim은 변경하지 않았습니다.

공식 일정은 370개 날짜별 항목, 369개 고유 경기 ID입니다. 연기·재편성 항목을 별개의 실제 경기로 단정하지 않습니다. 9월 28일은 일정이 없습니다.

## 날짜별 현황

| 날짜 | 공식 일정 항목 | 기존 보고서 | 승률이 저장된 경기 |
|---|---:|---:|---:|
| 2026-09-01 | 15 | 2 | 9 |
| 2026-09-02 | 15 | 4 | 13 |
| 2026-09-03 | 9 | 2 | 3 |
| 2026-09-04 | 16 | 0 | 0 |
| 2026-09-05 | 15 | 1 | 9 |
| 2026-09-06 | 15 | 1 | 3 |
| 2026-09-07 | 11 | 1 | 0 |
| 2026-09-08 | 15 | 1 | 10 |
| 2026-09-09 | 15 | 0 | 0 |
| 2026-09-10 | 5 | 2 | 1 |
| 2026-09-11 | 15 | 0 | 0 |
| 2026-09-12 | 15 | 3 | 5 |
| 2026-09-13 | 15 | 1 | 0 |
| 2026-09-14 | 10 | 0 | 0 |
| 2026-09-15 | 15 | 2 | 12 |
| 2026-09-16 | 15 | 2 | 4 |
| 2026-09-17 | 9 | 0 | 0 |
| 2026-09-18 | 15 | 0 | 0 |
| 2026-09-19 | 15 | 0 | 0 |
| 2026-09-20 | 15 | 0 | 0 |
| 2026-09-21 | 3 | 0 | 0 |
| 2026-09-22 | 16 | 0 | 0 |
| 2026-09-23 | 16 | 9 | 16 |
| 2026-09-24 | 12 | 2 | 4 |
| 2026-09-25 | 17 | 0 | 0 |
| 2026-09-26 | 13 | 0 | 0 |
| 2026-09-27 | 15 | 0 | 0 |
| 2026-09-28 | 0 | 0 | 0 |
| 2026-09-29 | 4 | 0 | 0 |
| 2026-09-30 | 4 | 0 | 0 |

## 기존에 저장된 최초 승률

아래는 각 날짜·경기에서 가장 먼저 저장된 **기존 preliminary 모델 계산값**입니다. 실제 승률로 검증됐다는 뜻이 아니며, prospective 표본 지위는 원래 receipt를 따릅니다. 후속 예측으로 최초 값을 교체하지 않았습니다. 경기 결과와 적중 여부는 이 결과 제외 분석에 넣지 않았습니다.

| 날짜 | 원정 @ 홈 | 예상 승리팀 | 모델 승률 | 최초 저장(UTC) |
|---|---|---|---:|---|
| 2026-09-01 | New York Mets @ Tampa Bay Rays | Tampa Bay Rays | 55.4% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | San Francisco Giants @ Pittsburgh Pirates | Pittsburgh Pirates | 61.4% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | Toronto Blue Jays @ Cleveland Guardians | Cleveland Guardians | 55.9% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | San Diego Padres @ Cincinnati Reds | San Diego Padres | 62.0% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | Atlanta Braves @ Washington Nationals | Atlanta Braves | 53.6% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | Seattle Mariners @ Boston Red Sox | Seattle Mariners | 51.8% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | Detroit Tigers @ Minnesota Twins | Minnesota Twins | 51.9% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | Milwaukee Brewers @ Chicago Cubs | Chicago Cubs | 50.1% | 2026-09-01T20:41:41.244006Z |
| 2026-09-01 | Athletics @ Texas Rangers | Texas Rangers | 59.3% | 2026-09-01T20:41:41.244006Z |
| 2026-09-02 | San Diego Padres @ Cincinnati Reds | San Diego Padres | 61.3% | 2026-09-02T15:58:39.204827Z |
| 2026-09-02 | Atlanta Braves @ Washington Nationals | Washington Nationals | 53.5% | 2026-09-02T15:58:39.204827Z |
| 2026-09-02 | Athletics @ Texas Rangers | Texas Rangers | 57.3% | 2026-09-02T15:58:39.204827Z |
| 2026-09-02 | Baltimore Orioles @ Colorado Rockies | Baltimore Orioles | 68.9% | 2026-09-02T17:40:43.56115Z |
| 2026-09-02 | Philadelphia Phillies @ Arizona Diamondbacks | Philadelphia Phillies | 50.6% | 2026-09-02T17:40:43.56115Z |
| 2026-09-02 | Seattle Mariners @ Boston Red Sox | Seattle Mariners | 52.6% | 2026-09-02T17:40:43.56115Z |
| 2026-09-02 | New York Mets @ Tampa Bay Rays | Tampa Bay Rays | 52.3% | 2026-09-02T22:02:25.593479Z |
| 2026-09-02 | San Francisco Giants @ Pittsburgh Pirates | Pittsburgh Pirates | 55.1% | 2026-09-02T22:02:25.593479Z |
| 2026-09-02 | Toronto Blue Jays @ Cleveland Guardians | Toronto Blue Jays | 53.9% | 2026-09-02T22:02:25.593479Z |
| 2026-09-02 | Detroit Tigers @ Minnesota Twins | Minnesota Twins | 51.1% | 2026-09-02T22:02:25.593479Z |
| 2026-09-02 | Miami Marlins @ Kansas City Royals | Kansas City Royals | 50.4% | 2026-09-02T22:02:25.593479Z |
| 2026-09-02 | Milwaukee Brewers @ Chicago Cubs | Milwaukee Brewers | 67.9% | 2026-09-02T22:02:25.593479Z |
| 2026-09-02 | Chicago White Sox @ Houston Astros | Houston Astros | 58.4% | 2026-09-02T22:02:25.593479Z |
| 2026-09-03 | San Francisco Giants @ Pittsburgh Pirates | Pittsburgh Pirates | 56.8% | 2026-09-03T15:31:30.130705Z |
| 2026-09-03 | Toronto Blue Jays @ Cleveland Guardians | Cleveland Guardians | 59.5% | 2026-09-03T15:31:30.130705Z |
| 2026-09-03 | Chicago White Sox @ Houston Astros | Houston Astros | 59.7% | 2026-09-03T15:31:30.130705Z |
| 2026-09-05 | Los Angeles Angels @ Pittsburgh Pirates | Pittsburgh Pirates | 60.0% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Milwaukee Brewers @ Cincinnati Reds | Milwaukee Brewers | 58.8% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Tampa Bay Rays @ Texas Rangers | Texas Rangers | 51.0% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Boston Red Sox @ Baltimore Orioles | Boston Red Sox | 57.3% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Toronto Blue Jays @ Kansas City Royals | Kansas City Royals | 52.0% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Minnesota Twins @ Chicago White Sox | Minnesota Twins | 52.0% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | New York Yankees @ San Diego Padres | New York Yankees | 50.1% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Arizona Diamondbacks @ Houston Astros | Arizona Diamondbacks | 56.2% | 2026-09-05T21:34:54.805831Z |
| 2026-09-05 | Washington Nationals @ Los Angeles Dodgers | Los Angeles Dodgers | 61.5% | 2026-09-05T21:34:54.805831Z |
| 2026-09-06 | Athletics @ Seattle Mariners | Seattle Mariners | 64.9% | 2026-09-06T19:18:38.274521Z |
| 2026-09-06 | New York Yankees @ San Diego Padres | New York Yankees | 52.9% | 2026-09-06T19:18:38.274521Z |
| 2026-09-06 | Minnesota Twins @ Chicago White Sox | Chicago White Sox | 59.9% | 2026-09-06T19:18:38.274521Z |
| 2026-09-08 | Colorado Rockies @ New York Yankees | New York Yankees | 69.8% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Tampa Bay Rays @ Atlanta Braves | Tampa Bay Rays | 53.5% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Chicago Cubs @ Milwaukee Brewers | Milwaukee Brewers | 69.0% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Arizona Diamondbacks @ Kansas City Royals | Arizona Diamondbacks | 50.3% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Pittsburgh Pirates @ Chicago White Sox | Chicago White Sox | 56.2% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Texas Rangers @ Seattle Mariners | Seattle Mariners | 52.8% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Washington Nationals @ San Diego Padres | San Diego Padres | 64.7% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Toronto Blue Jays @ Athletics | Toronto Blue Jays | 55.2% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | St. Louis Cardinals @ San Francisco Giants | St. Louis Cardinals | 50.8% | 2026-09-08T22:53:50.836891Z |
| 2026-09-08 | Cincinnati Reds @ Los Angeles Dodgers | Los Angeles Dodgers | 73.2% | 2026-09-08T22:53:50.836891Z |
| 2026-09-10 | Texas Rangers @ Seattle Mariners | Seattle Mariners | 50.9% | 2026-09-10T19:29:45.937941Z |
| 2026-09-12 | Colorado Rockies @ Detroit Tigers | Detroit Tigers | 60.6% | 2026-09-12T15:50:10.766384Z |
| 2026-09-12 | New York Mets @ New York Yankees | New York Yankees | 53.3% | 2026-09-12T15:50:10.766384Z |
| 2026-09-12 | Pittsburgh Pirates @ Chicago Cubs | Pittsburgh Pirates | 53.4% | 2026-09-12T16:48:46.806087Z |
| 2026-09-12 | Baltimore Orioles @ Toronto Blue Jays | Toronto Blue Jays | 60.7% | 2026-09-12T16:48:46.806087Z |
| 2026-09-12 | Kansas City Royals @ Boston Red Sox | Boston Red Sox | 64.0% | 2026-09-12T16:48:46.806087Z |
| 2026-09-15 | Athletics @ Tampa Bay Rays | Tampa Bay Rays | 57.8% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Milwaukee Brewers @ Pittsburgh Pirates | Milwaukee Brewers | 63.4% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Chicago White Sox @ Cleveland Guardians | Cleveland Guardians | 52.4% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Los Angeles Dodgers @ Cincinnati Reds | Los Angeles Dodgers | 73.6% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Philadelphia Phillies @ Washington Nationals | Philadelphia Phillies | 64.7% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Detroit Tigers @ Toronto Blue Jays | Detroit Tigers | 53.1% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Baltimore Orioles @ New York Mets | New York Mets | 53.5% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | New York Yankees @ Minnesota Twins | New York Yankees | 59.1% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Atlanta Braves @ Chicago Cubs | Chicago Cubs | 53.6% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | San Francisco Giants @ St. Louis Cardinals | St. Louis Cardinals | 52.9% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Boston Red Sox @ Texas Rangers | Texas Rangers | 59.7% | 2026-09-15T21:53:16.995286Z |
| 2026-09-15 | Kansas City Royals @ Houston Astros | Houston Astros | 56.1% | 2026-09-15T21:53:16.995286Z |
| 2026-09-16 | Chicago White Sox @ Cleveland Guardians | Cleveland Guardians | 65.1% | 2026-09-16T16:43:15.05975Z |
| 2026-09-16 | San Francisco Giants @ St. Louis Cardinals | St. Louis Cardinals | 52.9% | 2026-09-16T16:43:15.05975Z |
| 2026-09-16 | New York Yankees @ Minnesota Twins | New York Yankees | 55.0% | 2026-09-16T16:43:15.05975Z |
| 2026-09-16 | Detroit Tigers @ Toronto Blue Jays | Detroit Tigers | 52.7% | 2026-09-16T16:43:15.05975Z |
| 2026-09-23 | Washington Nationals @ Detroit Tigers | Detroit Tigers | 63.5% | 2026-09-23T16:08:14.124877Z |
| 2026-09-23 | Toronto Blue Jays @ Baltimore Orioles | Baltimore Orioles | 56.5% | 2026-09-23T15:07:44.118408Z |
| 2026-09-23 | Minnesota Twins @ San Francisco Giants | San Francisco Giants | 51.6% | 2026-09-23T17:08:21.10467Z |
| 2026-09-23 | Toronto Blue Jays @ Baltimore Orioles | Baltimore Orioles | 50.2% | 2026-09-23T22:12:08.997543Z |
| 2026-09-23 | St. Louis Cardinals @ Pittsburgh Pirates | Pittsburgh Pirates | 59.0% | 2026-09-23T20:09:36.739105Z |
| 2026-09-23 | Milwaukee Brewers @ Philadelphia Phillies | Milwaukee Brewers | 57.2% | 2026-09-23T20:09:36.739105Z |
| 2026-09-23 | Tampa Bay Rays @ New York Yankees | New York Yankees | 56.6% | 2026-09-23T21:11:04.662685Z |
| 2026-09-23 | Cleveland Guardians @ Boston Red Sox | Boston Red Sox | 54.3% | 2026-09-23T21:11:04.662685Z |
| 2026-09-23 | Cincinnati Reds @ Atlanta Braves | Atlanta Braves | 75.4% | 2026-09-23T20:09:36.739105Z |
| 2026-09-23 | Chicago White Sox @ Kansas City Royals | Chicago White Sox | 54.1% | 2026-09-23T20:09:36.739105Z |
| 2026-09-23 | Miami Marlins @ Chicago Cubs | Chicago Cubs | 59.1% | 2026-09-23T21:11:04.662685Z |
| 2026-09-23 | New York Mets @ Texas Rangers | New York Mets | 61.3% | 2026-09-23T21:11:04.662685Z |
| 2026-09-23 | Arizona Diamondbacks @ Colorado Rockies | Arizona Diamondbacks | 51.9% | 2026-09-23T21:11:04.662685Z |
| 2026-09-23 | Los Angeles Angels @ Athletics | Los Angeles Angels | 62.7% | 2026-09-23T22:12:08.997543Z |
| 2026-09-23 | San Diego Padres @ Los Angeles Dodgers | Los Angeles Dodgers | 68.6% | 2026-09-23T23:14:14.276176Z |
| 2026-09-23 | Houston Astros @ Seattle Mariners | Seattle Mariners | 58.2% | 2026-09-24T01:15:21.211583Z |
| 2026-09-24 | Milwaukee Brewers @ Philadelphia Phillies | Philadelphia Phillies | 51.6% | 2026-09-24T20:13:47.463923Z |
| 2026-09-24 | Cleveland Guardians @ Boston Red Sox | Boston Red Sox | 54.3% | 2026-09-24T20:13:47.463923Z |
| 2026-09-24 | Tampa Bay Rays @ New York Yankees | New York Yankees | 57.8% | 2026-09-24T20:33:03.766913Z |
| 2026-09-24 | Cincinnati Reds @ Atlanta Braves | Atlanta Braves | 71.5% | 2026-09-24T20:13:47.463923Z |

## 누락 경기 처리

- 9월 25~27일 및 그 외 누락된 R 경기: 사후 재현 평가기가 근거 binding을 정확히 4개만 허용하지만, 검증된 실제 수집 보고서는 91개(Statcast 55·구장 34·일정 1·ERA 1)를 보존합니다. 자료를 임의로 버릴 수 없어 한 경기 pilot 전 중단했으며, 신규 재현 예측은 0개입니다. 준비 스크립트는 미실행·pilot 준비 미완료 상태입니다.
- 9월 29일 F 경기: 현재 사후 재현 평가기는 정규시즌 R만 지원합니다.
- 9월 30일 F 경기: 실데이터 실행은 승인됐지만, 과거 연기·재편성 경기의 중복 ID 검증 오류로 보고서 생성 전에 중단됐습니다.
- 9월 30일 18:20:50 UTC 추가 확인: PHI–ATL은 경기 중으로 전환돼 경기 전 예측 누락 1건으로 기록했습니다. 사후 예측을 소급 생성하지 않았고, 적중률 표본에는 추가하지 않습니다. 나머지 3경기는 아직 미시작·미평가입니다. [당일 누락 기록](no_pregame_prediction_review.md)
- 그 외 누락 경기: 아래 JSON에 경기별 상태를 남겼으며, 새 예측을 이미 완료한 것으로 표시하지 않았습니다.
- 사후 재현은 NOT_PROSPECTIVE로 분리합니다. 실제 경기 전 예측·적중률·보정 표본으로 소급 편입하지 않습니다.

## 근거

- [원본 연결·고정 모델 검증](existing-september-prediction-verification.md)
- [전체 경기별 상태 및 정확한 원본 trace 경로](september-analysis-coverage-20260930.json)
- [오늘 실데이터 실행 실패 근거](postseason-live-activation-attempt-20260930T1521Z.md)
- [사후 재현 입력 계약 불일치 근거](reconstruction-preparation/input-contract-blocker.md)
