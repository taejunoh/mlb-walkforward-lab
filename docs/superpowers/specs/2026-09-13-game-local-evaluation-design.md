# 경기별 평가 격리 및 시작 후 연구 평가

Date: 2026-09-13
Status: User approved; implementation and scoped integration review completed on 2026-09-13. Tests: 177 passed, 193 subtests. No new live evaluation performed in this implementation task.

## 목적과 순서

첫 경기와 마지막 경기의 시작 시각 차이 때문에 하루 전체 평가가 막히지 않게 한다.
1단계는 기존 사전 평가의 경기별 시간 경계 격리, 2단계는 별도 시작 후 연구 평가다.
두 단계의 출력과 검증 실적을 섞지 않는다. 오늘 경기 재계산은 1단계 회귀 검증 후 수행한다.

## 확인된 원인

`observe_live_slate()`는 Warmup을 허용 상태로 처리하지만 schedule 또는 participant
receipt의 durable time이 예정 시작 시각 이상이면 예외를 던진다. 초기 수집 단계의
한 경기 시간 경계 위반이 전체 실행을 중단한다. 최종 재검증에는 이미 경기별 제외
처리가 있으므로 초기 수집에도 동일한 의도를 적용한다.

## 대안

- 권장: 시간 경계 위반만 경기별 제외로 처리하고, 사후 연구는 별도 명령/출력으로 분리한다.
- 상태가 In Progress로 바뀔 때까지 재실행: 코드 변경은 적지만 같은 장애가 반복된다.
- 시간 검사 해제 또는 포괄적 예외 무시: 사후 입력과 무결성 오류를 숨기므로 채택하지 않는다.

## 1단계: 사전 평가의 경기별 격리

`LiveSlateObservation`에 game_pk별 초기 관측 제외 사유를 표현한다.
정합성이 검증된 schedule에서 해당 경기의 schedule 또는 participant durable time이
scheduled start와 같거나 늦으면 `OBSERVATION_AT_OR_AFTER_START`를 기록한다.
그 경기의 추가 participant 사용/feature 계산을 중단하고 다른 경기를 계속 처리한다.
이미 시작했거나 사전 평가 대상이 아닌 상태는 명시적인 경기별 제외 사유를 남긴다.

`build_live_top3_run()`은 초기 제외 사유를 반영하여 해당 경기만 `EXCLUDED`로 출력한다.
제외 경기에는 확률, 순위, scored trace row를 만들지 않는다. 시작 전 Warmup은 기존대로
허용하고 최종 시간/선발/라인업 재검증은 유지한다. 실행 중 시작 경계를 넘는 경기도
같은 원칙으로 제외한다. 모든 경기가 제외되어도 원인 목록이 있는 정상 보고서를 만든다.

공유 schedule/schema 오류, duplicate identity, receipt/hash/root binding 불일치와
네트워크/전송 오류는 기존 전체 실패를 유지한다. 일반적인 integrity 예외를 포괄적으로
잡아서 제외로 바꾸지 않는다.

변경 후보: `live_sources.py`, `live_top3_service.py`, 필요한 최소 contract/report 표현,
관련 source/service/CLI 테스트. 기존 dirty 변경은 보존하며 unrelated refactor는 하지 않는다.

### 수용 테스트

- 과거 시각 Warmup 1경기와 유효 사전 경기 1경기: 전자만 제외, 후자 score/trace 생성.
- schedule 및 participant durable time 각각 start 직전/동일/직후 경계 검증.
- 수집 도중 시작, 최종 재검증 시작 시각 변경, 전체 경기 제외 시 사유 보존.
- 시작 전 Warmup 및 기존 started/final/unknown 상태 회귀 검증.
- 공유 schema/hash/root/duplicate 오류는 여전히 전체 실패.
- 제외 경기에는 prospective claim/확률/순위가 생성되지 않음.

## 2단계: 명시적 시작 후 연구 경로

기존 daily_top3의 시간 gate를 우회하지 않는다. 신규 독립 명령은 명시적인 입력 참조와
해시를 받아 `POSTSTART_RECONSTRUCTED_RESEARCH` 보고서만 생성한다. 동일 입력과 실행
metadata를 고정했을 때 동일 canonical output을 생성하고 기존 파일을 덮어쓰지 않는다.

입력은 경기 날짜 D 이전 데이터, 고정 모델 artifact, 공식 최초 선발/starting nine의
제한된 projection이다. 당일 경기 통계, 점수, 결과, 플레이, 승자, 실시간 배당은
feature 입력으로 허용하지 않는다. participant 추출은 현재 출전 중인 선수 목록 대신
최초 선발 9명을 사용하며 확인할 수 없으면 해당 경기 평가 불가로 남긴다.

수집 시각과 원본 출처는 실제 값 그대로 기록한다. 사후 확인한 lineup은
`RECONSTRUCTED_FINAL_LINEUP`이며 사전 공지 시각을 입증한다고 주장하지 않는다.
모델의 학습 데이터 cutoff를 검증해야 하고, 실제 사전 존재 증명이 없으면
`MODEL_ARTIFACT_TIME_UNPROVEN`을 명시한다. 파일 mtime을 역사적 증명으로 쓰지 않는다.

출력은 `reports/experimental_poststart_research/<D>/`의 별도 schema/trace로 저장한다.
`NOT_PROSPECTIVE`, `NOT_BET_VALIDATED`를 필수 표기하고 모델 추정 확률 및 산출 근거와
제약을 제공한다. 기존 사전 보고서, primary claim, 시장 PLAY, settlement, calibration
cohort와 historical ledger는 생성하거나 변경하지 않는다. 기존 pure scoring helpers만
재사용하며 결과/정산 수집 경로를 호출하지 않는다.

### 수용 테스트

- 금지된 결과/당일 통계 필드를 입력하면 거부한다.
- D-1 cutoff 위반, 당일 game data 혼입, 최초 선발 불확실은 평가 불가다.
- 시작 후 participant timestamp를 허용하되 필수 연구/재구성 제약을 모두 표시한다.
- 기존 사전/정산/시장/확률 검증 파일이 변경되지 않음을 검사한다.
- 결과를 읽지 않는 계산 경로라는 사실과 당시 실행 가능했던 예측이라는 주장을 구분한다.

## 범위 밖

모델 재학습/확률 보정, 배당 정책 변경, 실시간 베팅 추천, 과거 사전 기록 소급 생성,
대규모 인프라 검증은 포함하지 않는다. 이번 설계로 모델 확률의 신뢰성이 검증됐다고
주장하지 않는다. 네트워크 코드 설치, sudo, push/merge/PR 및 금지 경로 접근도 하지 않는다.
