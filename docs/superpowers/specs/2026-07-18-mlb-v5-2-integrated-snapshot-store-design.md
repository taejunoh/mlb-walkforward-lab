# MLB v5.2 통합 소스 스냅샷 저장소 설계

날짜: 2026-07-18
상태: 승인된 구현 설계 (`Task 8B` 구현 입력)
범위: 검증된 소스 후보를 원자적으로 저장하고, 동일 바이트를 read-only로 재생 검증하는 저장소 계층

## 1. 결정 요약

통합 저장소는 `mlb_backtest/v52_snapshot_store.py`가 소유한다. 이 모듈은 검증된 81개 정규화 스냅샷을 content-addressed 파일로 쓰고, 단일 최종 커밋 포인트인 `source_inventory.json`을 공개하며, inventory와 스냅샷을 다시 계산해 읽는 loader를 제공한다.

`mlb_backtest/v52_verify_sources.py`는 순수한 replay façade로 남긴다. verifier는 네트워크, 디렉터리 탐색, 파일 쓰기, phase 전환을 하지 않는다. verifier가 만든 메모리상의 결과만 snapshot-store가 받아서 저장한다. 따라서 검증 로직과 영속화/동시성 로직이 서로 다른 신뢰 경계를 갖는다.

이번 작업의 상태 문자열은 `READY_CANDIDATE`이다. 이는 정규화 결과와 전역 lineage가 검증되었다는 뜻이지, 실행 레지스트리의 `SOURCES_READY`로 자동 전환한다는 뜻이 아니다. `ACQUIRING` 또는 `SOURCES_READY` 레지스트리에서 읽을 수 있지만 inventory를 읽는 것만으로 phase를 전진시키지 않는다. `SOURCES_READY` gate는 승인된 독립 `SOURCE_REVIEW` 최소 1개와 서로 다른 `CLEAN_SOURCE_REPLAY` 영수증 정확히 2개가 충족될 때까지 후속 작업으로 남긴다.

## 2. 고정된 데이터 범위

### 2.1 파티션 수

역사 범위는 2018–2024 정규 시즌이며, 월별 Statcast를 제외한 다섯 계절 단위 소스는 시즌당 한 파티션이다. Statcast는 정규 시즌 월 단위로 분할한다. 아래 표의 합계는 모든 source를 합친 전체 파티션 수이며, 괄호 안에는 그중 Statcast 월 파티션 수를 함께 표시한다.

| 시즌 | 전체 파티션 수 (Statcast 월 파티션) |
|---:|---:|
| 2018 | 12 (7) |
| 2019 | 12 (7) |
| 2020 | 8 (3) |
| 2021 | 12 (7) |
| 2022 | 12 (7) |
| 2023 | 13 (8) |
| 2024 | 12 (7) |
| 합계 | 81 |

전체 81개는 다음과 같이 구성된다.

- `baseball_savant_statcast` 월별 46개
- 계절별 `mlb_schedule_game_feed` 7개
- 계절별 `mlb_stats_pitching` 7개
- 계절별 `venue_team_timezone_table` 7개
- 계절별 `prior_schedule_history` 7개
- 계절별 `mlb_stats_pitcher_bf` 7개

논리적인 `source-season` 행은 6개 source × 7개 시즌 = 42개다. Statcast의 한 source-season 행은 시즌별 Statcast-only 월 snapshot 수 `7/7/3/7/7/8/7`을 묶으며, 월별 snapshot 자체는 81개 전체 snapshot 집합에만 한 번씩 나타난다.

### 2.2 소스 식별자

inventory의 `source` 값은 다음 폐쇄 집합만 허용한다.

1. `mlb_schedule_game_feed` (`season_game_evidence_snapshot`)
2. `baseball_savant_statcast` (`pitch`)
3. `mlb_stats_pitching` (`season_d1_league_pitching_reference`)
4. `mlb_stats_pitcher_bf` (`player_career_batters_faced_reference`)
5. `venue_team_timezone_table` (`effective_dated_venue_team_reference`)
6. `prior_schedule_history` (`verified_game_projection`)

기존 verifier의 payload 계약과 key 집합은 변경하지 않는다. 특히 Statcast 의미 payload에 split receipt 바인딩을 추가하지 않는다. split lineage는 envelope의 `split_receipt_sha256s`로만 표현한다.

## 3. 책임 경계와 API

### 3.1 순수 verifier façade

`v52_verify_sources.py`가 제공하는 API는 입력 객체와 인증된 query/receipt를 받아 `NormalizedSnapshot` 또는 81개 snapshot tuple을 반환한다.

- 입력: `run_id`, sealed query graph, `VerifiedQueryRecord` 집합, upstream game snapshots
- 출력: canonical envelope bytes, SHA-256, payload, 실제 terminal query/sidecar 해시, 실제 split receipt 해시
- 금지: HTTP, 환경변수 기반 경로 탐색, `open(..., "w")`, `mkdir`, inventory 갱신, registry phase 변경

### 3.2 snapshot-store API

`v52_snapshot_store.py`는 canonical root `data/v5_2_qd/historical/sources/`를 기본 저장소로 사용하며 최소한 다음의 명시적 API를 갖는다.

- `build_inventory(run_id, snapshots, query_graph, current_phase) -> SourceInventory`
  - 81개 snapshot과 graph를 전역 검증하여 메모리 inventory를 만든다.
  - 호출 시 registry phase가 `ACQUIRING` 또는 `SOURCES_READY`인지 확인하지만 phase를 inventory bytes에 저장하지 않는다. 저장 상태는 항상 `READY_CANDIDATE`다.
- `publish_snapshot_store(root, inventory, snapshots, *, lock_owner) -> PublishReceipt`
  - lock을 획득한 뒤 immutable blob과 checksum, inventory를 원자적으로 공개한다.
- `load_snapshot_store(root, expected_run_id, query_graph, upstream_game_snapshots) -> LoadedSnapshotStore`
  - 디렉터리 열거를 권위로 삼지 않고 inventory가 지목한 경로만 읽는다.
  - verifier를 통해 81개를 다시 계산하고 저장 bytes, SHA, lineage, inventory와 exact compare한다.
- `read_inventory(root) -> SourceInventory`
  - `source_inventory.sha256`를 먼저 검증하고 `source_inventory.json`을 닫힌 schema로 파싱한다.

모든 반환 객체는 immutable/frozen 구조이며, loader는 파일을 수정하지 않는다. `publish_snapshot_store` 외의 API는 write capability를 받지 않는다.

## 4. 파일 레이아웃과 폐쇄 inventory

### 4.1 content-addressed 레이아웃

저장소의 canonical root는 `data/v5_2_qd/historical/sources/`다. run ID는 inventory의 closed binding으로 검증하며 경로에 삽입하지 않는다.

```text
{root}/
  .snapshot-store.lock
  {season}/{source}/{snapshot_sha256}.snapshot.json
  source_inventory.sha256
  source_inventory.json
```

경로의 `{season}`은 4자리 정수 문자열이고, `{source}`는 위 폐쇄 source 집합의 값이다. 파일명 digest는 파일 전체 canonical JSON bytes(최종 개행 포함)의 SHA-256과 정확히 같다. inventory는 digest가 가리키는 파일의 상대 경로와 기대 길이까지 바인딩한다.

임시 파일은 동일 디렉터리 아래에서만 `.tmp-{run_id}-{random}` 이름으로 만들며, inventory가 참조하지 않는 잔여 임시 파일과 이미 공개된 blob은 loader 권위에서 제외한다.

### 4.2 `source_inventory.json` 필드

최상위 object는 다음 19개 key만 허용한다. 추가/누락/중복 key는 즉시 거부한다.

```json
{
  "schema_version": 1,
  "artifact_kind": "V5_2_SOURCE_INVENTORY",
  "run_id": "...",
  "status": "READY_CANDIDATE",
  "critical_errors": [],
  "network_attempts": 0,
  "source_gate_ready": true,
  "run_spec": {},
  "acquisition_contract": {},
  "terminal_checkpoint": {},
  "schedule_inventory": {},
  "game_evidence_inventory": {},
  "timezone_gate": {},
  "snapshot_sha256s": [],
  "logical_query_ids": [],
  "split_receipt_sha256s": [],
  "snapshots": [],
  "source_seasons": [],
  "counts": {}
}
```

최상위 key는 정확히 다음 19개이며 추가/누락 key는 거부한다.

`schema_version`, `artifact_kind`, `run_id`, `status`, `critical_errors`, `network_attempts`, `source_gate_ready`, `run_spec`, `acquisition_contract`, `terminal_checkpoint`, `schedule_inventory`, `game_evidence_inventory`, `timezone_gate`, `snapshot_sha256s`, `logical_query_ids`, `split_receipt_sha256s`, `snapshots`, `source_seasons`, `counts`.

`artifact_kind`는 `V5_2_SOURCE_INVENTORY`, `status`는 `READY_CANDIDATE`, `critical_errors`는 빈 배열, `network_attempts`는 0, `source_gate_ready`는 true로 고정한다. inventory는 registry phase를 전혀 포함하지 않으며 phase 변경과 독립적이다.

`run_spec`, `acquisition_contract`, `schedule_inventory`, `game_evidence_inventory`, `timezone_gate`는 각각 `path`, `sha256` 두 key만 가진 authority binding closed object다. `terminal_checkpoint`는 `path`, `sha256`, `sequence`, `stage` 네 key만 가지며 `stage`는 `REMAINING`으로 고정한다. 모든 path는 canonical relative path이고 SHA는 해당 파일의 canonical bytes를 다시 계산한 값이어야 한다. 이 객체들에는 임의의 최신 포인터나 미검증 경로를 허용하지 않는다.

`snapshots`는 정확히 81개 object이며 각 object의 key는 다음 10개뿐이다.

- `season` (integer)
- `source` (폐쇄 집합)
- `grain` (source별 고정 grain)
- `status` (`READY`)
- `snapshot_sha256` (64 lowercase hex)
- `relative_path` (`{season}/{source}/{sha}.snapshot.json`)
- `byte_length` (양의 integer)
- `logical_query_ids` (정렬된 실제 terminal query ID 목록)
- `source_query_sidecar_sha256s` (동일 순서의 sidecar hash 목록)
- `split_receipt_sha256s` (parent query ID 순서의 실제 receipt hash 목록; 비분할은 빈 배열)

`snapshot_sha256s`는 canonical snapshot row 순서를 그대로 따르는 81개 digest 배열이다. `logical_query_ids`와 `split_receipt_sha256s`는 각각 snapshot rows에서 모은 sorted unique exact union이다. 전자는 terminal logical query ID만 포함하고, 후자는 실제 split receipt hash만 포함한다.

`source_seasons`는 정확히 42개 object이며 key는 `season`, `source`, `grain`, `status`, `snapshot_sha256s`, `logical_query_ids`, `split_receipt_sha256s`다. `status`는 `READY`로 고정한다. 계절 소스는 `snapshot_sha256s` 1개, Statcast는 해당 월 수만큼의 digest를 canonical 월 순서로 가진다. 별도 digest/receipt 중복 필드는 존재하지 않는다.

`counts`는 정확히 `snapshot_count=81`, `source_season_count=42`, `source_count=6`, `logical_query_count`, `split_receipt_count`, `by_source`, `by_season` key를 가진다. `by_source`는 Statcast 46 및 나머지 다섯 source 각 7의 snapshot cardinality를, `by_season`은 `2018=12`, `2019=12`, `2020=8`, `2021=12`, `2022=12`, `2023=13`, `2024=12`를 기록하는 폐쇄 object다.

inventory에는 mutable `latest` 포인터, 파일 mtime, host 정보, 토큰, 원시 응답, 미검증 query, fabricated success sidecar를 넣지 않는다. inventory bytes 자체의 SHA는 파일 옆의 `source_inventory.sha256`가 보관하며, checksum 파일에는 lowercase hex와 단일 개행만 허용한다.

## 5. 빌드 불변식

`build_inventory`는 publish 전에 다음을 모두 검사한다.

1. 81개 snapshot의 `(season, source, grain)` 키가 정확히 한 번씩 존재하며 각 row의 status는 `READY`다.
2. Statcast 월 범위가 2018–2024의 sealed 46-root query plan과 bytewise 일치한다.
3. 각 snapshot은 해당 root의 모든 terminal READY leaf sidecar를 정확히 포함한다. root 또는 split parent를 성공 sidecar로 꾸미지 않는다.
4. 각 child leaf가 하나의 root에만 귀속되고, split receipt의 parent/child range가 graph와 정확히 일치한다.
5. split receipt의 parent 순서를 canonical parent query ID 순으로 정해 envelope hash 배열을 만든다. 순서가 바뀌면 snapshot bytes와 digest가 달라지고 loader가 거부한다.
6. Statcast의 `played_game_pks`는 7개 인증된 game snapshot에서만 계산한다. `played=true`이고 해당 월의 official date 안에 있는 게임만 포함하며 canceled/unplayed는 제외한다.
7. 42개 source-season row의 snapshot 참조가 81개 snapshot을 한 번씩 덮고, 누락·중복·교차 시즌이 없다.
8. 전역 logical-query union과 실제 split-receipt hash union이 snapshot별 union과 exact set equality를 갖는다. 성공 receipt hash는 전역 receipt union에 포함하지 않는다.
9. 모든 SHA가 lowercase 64-hex이고, component SHA와 normalized snapshot SHA가 충돌하지 않는다.
10. canonical JSON serialization은 `sort_keys=true`, UTF-8, 최종 `\n` 한 개로 고정한다.

실패 시 inventory 생성과 publish 모두 중단한다. 부분 결과를 `READY_CANDIDATE`로 공개하지 않는다.

## 6. 원자적 publish 순서

단일 writer만 존재하도록 `.snapshot-store.lock`을 `O_CREAT|O_EXCL`, no-follow, 단일 link로 만든다. lock 파일에는 run ID와 writer nonce만 기록하며 PID나 mutable heartbeat를 권위 정보로 사용하지 않는다.

publish 순서는 다음과 같다.

1. 루트와 season/source 디렉터리를 no-follow로 만들고 각 디렉터리의 inode/type을 기록한다.
2. 메모리 inventory와 81개 canonical snapshot bytes를 독립적으로 다시 검증한다.
3. 각 blob을 같은 디렉터리의 임시 파일에 `O_EXCL`로 full write한다. `fsync(file)` 후 대상 digest 경로에 no-clobber hard-link로 공개하고 임시 이름을 unlink한다. 부모 디렉터리를 fsync하여 공개 이름만 남긴다.
4. 대상 digest 경로가 이미 있으면 no-clobber 규칙을 적용한다. 기존 파일이 regular single-link이고 bytes/SHA가 정확히 같으면 재사용하고, 다르면 충돌로 실패한다. 기존 파일을 교체하는 연산은 사용하지 않는다.
5. 각 source 디렉터리를 fsync한다.
6. 임시 `source_inventory.sha256`를 full write·fsync하고 no-clobber hard-link로 먼저 공개한 뒤 임시 이름을 unlink한다. 기존 checksum이 다르면 실패한다.
7. `source_inventory.json`을 임시 파일에 full write·fsync하고, 부모 디렉터리 fsync 후 no-clobber hard-link로 마지막에 공개한 뒤 임시 이름을 unlink한다. 이 파일이 유일한 최종 commit point다.
8. 루트 디렉터리를 fsync한다.
9. 공개 직후 read-only loader를 실행하여 81개 재생, inventory hash, 경로, bytes를 exact compare한다.
10. loader 성공 후 lock을 닫고 unlink한다. lock 삭제 실패는 publish 실패로 보고하며 inventory는 이미 유효하게 공개된 상태로 남는다.

inventory가 존재하지 않는 crash 상태는 미공개로 취급한다. blob과 checksum만 남은 상태, 일부 blob만 남은 상태, temp 파일이 남은 상태는 모두 loader가 무시한다. `source_inventory.json`이 존재하면 loader는 모든 참조 대상과 checksum을 요구하며 하나라도 없거나 변조되면 fail closed한다.

## 7. 보안·동시성·파일 타입 방어

- 모든 open은 no-follow 플래그와 `lstat`/`fstat` 검사를 함께 사용한다.
- snapshot, checksum, inventory, lock은 regular file·single link만 허용한다. symlink, hardlink, FIFO, socket, directory 대체를 모두 거부한다.
- 부모 디렉터리도 생성 직후와 읽기 직전에 inode/type을 exact compare한다. 경로 교체 race가 관찰되면 즉시 실패한다.
- lock은 stale 판단으로 자동 삭제하지 않는다. 명시적 운영자 정리 없이 두 writer가 동시에 진행되지 않는다.
- 상대 경로는 루트 밖으로 나갈 수 없으며 `..`, 절대 경로, separator injection을 거부한다.
- loader는 디렉터리 목록을 신뢰하지 않고 inventory의 closed path만 열어 unexpected file을 권위 집합에 포함하지 않는다. 다만 root namespace의 symlink/hardlink/special node는 fail closed한다.
- SHA는 파일을 읽은 실제 bytes에서 재계산한다. metadata, mtime, inode만으로 성공 처리하지 않는다.

## 8. read-only exact replay loader

loader는 다음 순서로 실행한다.

1. `.snapshot-store.lock`이 있으면 writer가 publish 중일 수 있으므로 읽기를 중단한다.
2. `source_inventory.sha256`와 `source_inventory.json`을 no-follow로 읽고 canonical JSON과 SHA를 검증한다.
3. inventory schema와 81/42 cardinality, `READY_CANDIDATE`/`READY` status, 전역 union을 검증한다. registry phase는 inventory bytes와 독립된 현재 실행 상태로만 확인한다.
4. inventory가 지정한 81개 경로를 열어 경로·regular/single-link·byte length·SHA를 검증한다.
5. envelope의 `logical_query_ids`, sidecar hash, split receipt hash가 sealed graph의 실제 terminal/receipt lineage와 exact equality인지 확인한다.
6. 원시 evidence와 인증된 game snapshots를 verifier façade에 넘겨 81개를 다시 계산한다.
7. 재계산된 canonical bytes, SHA, payload, snapshot 순서, source-season aggregation, global union을 저장 결과와 bytewise/exact compare한다.
8. 비교가 모두 성공하면 immutable `LoadedSnapshotStore`를 반환한다. loader는 쓰기, cleanup, phase advance, network call을 하지 않는다.

`ACQUIRING`과 `SOURCES_READY` 모두 같은 inventory를 읽을 수 있다. `SOURCES_READY`로 phase를 올리는 함수나 자동 side effect를 loader에 두지 않는다. 승인된 독립 `SOURCE_REVIEW` 최소 1개와 서로 다른 `CLEAN_SOURCE_REPLAY` 영수증 정확히 2개가 별도 gate를 통과한 뒤 실행 레지스트리가 독립적으로 전진한다.

## 9. 데이터 흐름

```text
sealed query/receipt graph + verified evidence
              |
              v
       v52_verify_sources (pure replay)
              |
       81 NormalizedSnapshot objects
              |
              v
       v52_snapshot_store.build_inventory
              |
       closed SourceInventory (42 rows + global unions)
              |
              v
       atomic publish: blobs -> checksum -> inventory commit point
              |
              v
       read-only loader -> fresh replay -> exact compare
```

raw response는 snapshot blob에 다시 저장하지 않는다. 원시 evidence와 query sidecar/receipt는 기존 sealed acquisition namespace가 소유하며, normalized envelope는 그들의 hash와 logical ID만 lineage로 보유한다.

## 10. 실패 처리와 재시도

- 입력 graph/cardinality 오류: `ValueError`로 중단하고 파일을 만들지 않는다.
- sidecar/receipt hash 불일치: lineage integrity 오류로 fail closed한다. 네트워크 재시도는 하지 않는다.
- 대상 파일 digest 충돌: 기존 파일이 동일 bytes가 아니면 publish를 중단한다. overwrite나 unlink 후 재생성은 금지한다.
- checksum/inventory가 이미 있고 동일 bytes: idempotent 재실행으로 간주한다. 다르면 충돌로 중단한다.
- crash 후 `.tmp-*` 또는 unreferenced blob: loader는 무시하되 운영자 정리 전까지 삭제하지 않는다.
- lock 충돌: 대기하거나 stale 제거하지 않고 호출자에게 lock 오류를 반환한다.
- loader exact compare 실패: `LoadedSnapshotStore`를 반환하지 않고 phase도 변경하지 않는다.

재시도는 같은 `run_id`, 같은 sealed graph, 같은 RunSpec에서만 허용한다. 새 query, 새 receipt, 새로운 월 범위를 임의로 추가하는 재시도는 별도 run으로 취급한다.

## 11. 테스트 매트릭스

### 11.1 구조/계약

- 81개 snapshot과 42개 source-season row의 정확한 cardinality
- 시즌별 전체 snapshot 수 `12/12/8/12/12/13/12` 및 Statcast-only 월 snapshot 수 `7/7/3/7/7/8/7` 검증
- 폐쇄 key, source/grain, `READY_CANDIDATE`, phase independence 검증
- 전역 query/split-receipt union의 snapshot union과 exact equality
- canonical bytes/개행/SHA 및 digest collision 거부

### 11.2 lineage/재생

- terminal leaf 누락·중복·orphan·gap·overlap·cycle 거부
- 실제 split receipt만 허용; fabricated parent success sidecar 거부
- split receipt 순서 permutation 거부
- played game만 Statcast population에 포함; canceled/unplayed 제외
- loader가 verifier 재생 결과와 저장 bytes를 exact compare

### 11.3 파일시스템/원자성

- temp write 도중 crash, checksum만 존재, inventory만 부분 존재
- 동일 digest idempotent 재실행과 다른 bytes 충돌
- lock 경쟁, lock 잔류, hard-link publish race
- symlink/hardlink/FIFO/socket/directory 대체 및 부모 inode 교체
- 경로 탈출(`..`, 절대 경로, separator injection) 거부
- unexpected directory entry와 unreferenced blob의 권위 제외

### 11.4 순수성/운영 경계

- verifier 모듈 import 시 filesystem/network mutation이 없음
- snapshot-store loader가 open-for-write, mkdir, phase transition을 호출하지 않음
- `ACQUIRING`/`SOURCES_READY` 양쪽에서 동일 inventory bytes
- 승인된 독립 `SOURCE_REVIEW` 최소 1개와 서로 다른 `CLEAN_SOURCE_REPLAY` 영수증 정확히 2개 없이 readiness 승격하지 않음

## 12. 범위와 비목표

이번 구현에 포함한다.

- 81개 normalized snapshot의 in-memory 통합 replay
- content-addressed blob, checksum, closed inventory, lock
- atomic no-clobber publish와 crash residue 규칙
- read-only exact replay loader
- READY_CANDIDATE와 phase-independent inventory

이번 구현에 포함하지 않는다.

- HTTP/Statcast/MLB API 수집 또는 재수집
- feature engineering, fitting, OOF, pick/card 생성, 결과 집계
- inventory로부터 실행 phase 자동 전환
- `SOURCES_READY` 승인된 독립 `SOURCE_REVIEW` 최소 1개와 서로 다른 `CLEAN_SOURCE_REPLAY` 영수증 정확히 2개
- mutable latest pointer, garbage collector, 원시 response 복제
- prospective/live collector 또는 500-pick 운영

## 13. 승인 기준

다음 조건을 모두 충족할 때 Task 8B의 snapshot-store slice를 완료로 판정한다.

1. 설계에 정의된 81/42 cardinality와 source/grain closed set을 코드와 schema가 동일하게 강제한다.
2. 동일 RunSpec과 sealed graph에서 두 번 빌드한 inventory와 81개 blob이 byte-identical이다.
3. publish 직후 loader가 네트워크 없이 81개를 재생하고 모든 bytes/SHA/lineage/union을 exact compare한다.
4. 충돌, race, symlink/hardlink, crash residue 테스트가 모두 fail-closed로 통과한다.
5. loader가 `ACQUIRING` 또는 `SOURCES_READY`에서 phase를 변경하지 않는다.
6. protected v5.1 파일과 기존 source snapshot payload 계약이 변경되지 않는다.
7. 코드·schema·테스트와 검증된 publish evidence가 feature branch에 기록되며, 생성된 source store와 inventory/blob은 canonical data root에 repository data policy를 적용해 관리한다. `master` 머지는 후속 closed milestone 이후로 남긴다.
