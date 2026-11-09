# 2026-09-13 결과 검증 및 기록 반영

검증일: 2026-09-15. 공식 MLB Schedule 및 개별 경기 feed를 대조했습니다.

- 공식 Final: 15경기. 미종료 0, 성공 기록의 결과 오류 0.
- 예측 평가: 15경기 모두 UNSCORED_OR_EXCLUDED. 선택된 예측과 forecast_outcome은 모두 null입니다.
- 적중률, Brier, log loss, 수익률에 추가하는 표본: 0건. 이는 0% 적중률이 아니라 계산 대상 없음입니다.
- 과거 예측 재생성, 모델 재학습, 확률 보정, 베팅, commit/push는 수행하지 않았습니다.

## 현재 사용할 성공 기록

[전체 JSON](3f10286bb9c92af648af123485dc1ee33cb948f37a00dd998e3e2703e7c16464.json) · [전체 표](3f10286bb9c92af648af123485dc1ee33cb948f37a00dd998e3e2703e7c16464.md)

JSON 전체 파일 SHA-256: `3f10286bb9c92af648af123485dc1ee33cb948f37a00dd998e3e2703e7c16464`.

## 공식 결과

| gamePk | 경기 | 원정 점수 | 홈 점수 | 승리 | 총점 |
|---:|---|---:|---:|---|---:|
| 824225 | COL @ DET | 1 | 8 | DET | 9 |
| 822682 | LAA @ WSH | 5 | 6 | WSH | 11 |
| 824869 | PHI @ ATL | 9 | 4 | PHI | 13 |
| 823495 | NYM @ NYY | 0 | 2 | NYY | 2 |
| 822764 | BAL @ TOR | 1 | 8 | TOR | 9 |
| 822928 | HOU @ TB | 4 | 14 | TB | 18 |
| 823815 | LAD @ MIA | 4 | 6 | MIA | 10 |
| 823658 | CLE @ MIN | 9 | 2 | CLE | 11 |
| 823734 | CIN @ MIL | 4 | 3 | CIN | 7 |
| 823008 | CWS @ STL | 1 | 3 | STL | 4 |
| 824628 | PIT @ CHC | 4 | 3 | PIT | 7 |
| 824708 | KC @ BOS | 1 | 4 | BOS | 5 |
| 824952 | SEA @ ATH | 7 | 8 | ATH | 15 |
| 825033 | TEX @ ARI | 7 | 6 | TEX | 13 |
| 823171 | SD @ SF | 6 | 4 | SD | 10 |

출처: [MLB 9월 13일 스코어보드](https://www.mlb.com/scores/2026-09-13). 독립 조회 시각 2026-09-15T12:40:50Z. 결과 수집 영수증은 성공 JSON의 각 result.receipt_relative에 연결되어 있습니다.

## 예측 증거 보존

원본 오전 report SHA-256: `35d631db6f6dcfa7aa5dae0ff8a9426d05bb63e5a453fa83e55d780e35cff20c`.

원본 trace SHA-256: `b89ba06ce4d704fd7a472c862f6d961580d60ce8bbd03e1a7eff01c876557ec1`.

반영 전후 모두 동일합니다. 보고서의 preliminary_top3=0, verified_top3=0, unscored_games=15이며 trace.games=[]입니다. 이후 재평가 세션 62849는 exit 2 및 'venue reference is malformed'로 실패했다고 실행 담당자가 보고했고, 해당 날짜의 추가 report/trace는 발견되지 않았습니다. 이 실패를 예측 성공이나 PASS로 바꾸지 않았습니다.

## 기록 작업 중 발견한 경로 문제

상대 root(.)로 실행한 첫 수집은 결과 원문을 저장한 뒤 'raw payload path differs'로 결과 인증에 실패했습니다. 해당 실패 기록 `0b57b404dd71aa2df6fdf738a9464b598215c4d6ce5fe9b43c0d45662598f203.json`과 원문/영수증은 보존했습니다.

절대 root로 15개의 기존 결과 영수증을 각각 명시해 재인증했으며 재수집 없이 성공 기록을 새로 생성했습니다(exit 0). 실패 기록과 성공 기록은 같은 15경기의 처리 버전이며 별도 표본으로 중복 집계하지 않습니다. 소스의 상대/절대 경로 비교 문제 자체는 이번 작업에서 수정하지 않았습니다.

