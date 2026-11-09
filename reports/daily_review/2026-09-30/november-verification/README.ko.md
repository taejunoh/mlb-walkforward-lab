# 2026년 11월 검증 증빙

이 디렉터리는 11월 9일 종료 후 검증에서 생성된 메타데이터와 원본 형식의 영수증 파일을 보존합니다. 요약과 해석은 [종료 후 검증 기록](../post-closeout-verification-2026-11-09.json)을 참조하세요. 이 보관 자료는 출처 확인을 위한 보조 자료이며, 새로운 검증이나 검증 주장을 추가하지 않습니다.

## 구성

- `result.json` — 격리된 연구 로더 실행 결과.
- `independent_result.json` — 경기 ID, 날짜·유형, 투구 키의 독립 대조 결과.
- `accepted_native_artifacts.json`, `download_checkpoint.json` — 증빙 채택과 수집 시점 메타데이터.
- `verification.xml` — 기록된 오프라인 테스트 33개와 하위 검증 9개의 원본 출력.
- `network/` — 날짜 구간별 관측 JSON 45개와 `events.jsonl`.
- `history_receipts_postseason_research_v1/` — 일정·Statcast 이력 영수증과 영수증 해시.
- `manifests/` — Statcast manifest 46개, 일정 manifest 1개 및 각 해시 파일.
- `SHA256SUMS` — 보관된 증빙 파일 380개의 체크섬. 이 안내문과 체크섬 파일 자체는 포함하지 않습니다.

이 디렉터리에서 다음 명령으로 보관된 파일의 해시를 확인할 수 있습니다.

```sh
sha256sum -c SHA256SUMS
```

메타데이터의 원본 절대 경로는 수집 당시의 출처를 기록한 것이며, 다른 환경에서 사용할 수 있는 링크가 아닙니다. 외부 MLB/Statcast `.raw` 및 `.csv` 자료는 의도적으로 포함하지 않았습니다. 따라서 이 메타데이터 보관본만으로는 원본 데이터 대조를 다시 실행할 수 없습니다. 보관 사실은 기록된 결과를 새로 검증했다는 의미가 아니며, 실시간 운영이 재개됐다는 뜻도 아닙니다.

저장소의 MIT 라이선스는 프로젝트에서 직접 작성한 코드와 문서에만 적용됩니다. 외부 데이터와 제3자 자료는 제외됩니다. 저장소의 [라이선스](../../../../LICENSE) 및 [제3자 고지](../../../../THIRD_PARTY_NOTICES.md)를 확인하세요.
