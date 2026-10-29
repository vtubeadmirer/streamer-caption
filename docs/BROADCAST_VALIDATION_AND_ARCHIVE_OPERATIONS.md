# 방송 검증 및 아카이브 운영 가이드

## 목적

실제 방송 중 발견한 자막 문제를 정확한 시점과 함께 남기고, 방송 종료 후 로그·지연·메모리·GPU·저장 일치 여부를 한 번에 검사한다. 강제 종료된 세션은 다음 실행에서 복구하며, 누적된 방송 기록은 백업 후 안전하게 정리한다.

## 실제 사용 순서

1. 평소처럼 `START_HERE.bat`에서 `B`로 자막을 실행한다.
2. 브라우저에서 `http://127.0.0.1:8766/validate`를 연다.
3. 문제가 생긴 순간 `환각 자막`, `말했지만 누락`, `단어 오인식` 중 하나를 누른다.
4. 잘 인식된 비교 기준이 필요하면 `정상 인식`을 누른다.
5. 방송이 끝나면 `VALIDATE_LATEST_BROADCAST.bat`를 실행한다.
6. Codex 분석이 필요하면 `make_support_bundle.bat`로 지원 묶음을 만든다. 최신 검증 보고서가 자동으로 포함된다.

메모에는 실제로 말한 문장이나 원하는 표기를 적는 것이 좋다. 예: `샹도웨쭈라고 말했는데 산도웨이쭈로 나옴`.

## 구조

```mermaid
flowchart LR
    A[마이크 또는 PC 재생음] --> B[실시간 STT 파이프라인]
    B --> C[session.jsonl]
    B --> D[SQLite 확정 자막]
    E[/validate 사용자 표시] --> F[SQLite annotations]
    C --> G[통합 검증기]
    D --> G
    F --> G
    H[memory.csv] --> G
    G --> I[validation_report.json 및 md]
    D --> J[/history 아카이브 관리]
    J --> K[백업 ZIP]
    J --> L[종료 세션 정리]
```

## 문제 표시 데이터

표시 하나에는 다음 정보가 저장된다.

- 방송 세션 ID
- 방송 시작 기준 오디오 시점
- 표시 종류
- 표시 당시 마지막 자막
- 사용자가 입력한 메모
- 생성 시각

최근 표시는 `/validate`에서 바로 취소하거나 고칠 수 있다. 줄을 누르면 메모(없으면 표시 당시 자막)가 든 입력칸이 열리고, Enter는 메모로 저장하고 Esc는 저장하지 않고 닫는다. 표시 내용은 방송 종료 리포트와 통합 검증 보고서에도 포함된다. 방송이 끝난 뒤 고친 메모는 그 뒤에 만드는 검증 보고서에 들어간다.

## 통합 검증 보고서

`VALIDATE_LATEST_BROADCAST.bat`는 최신 `logs/<session>/session.jsonl`을 검사해 같은 폴더에 아래 파일을 만든다.

- `validation_report.json`: 기계 분석용 전체 수치
- `validation_report.md`: 사람이 읽기 쉬운 요약

주요 검사 항목은 JSONL 파싱 오류, JSONL과 SQLite 확정 자막 일치, 엔진 오류, STT 복구 실패, STT P95 지연, 메모리 증가 추세, GPU 메모리 여유, 사용자 문제 표시다.

판정은 다음과 같다.

- `PASS`: 자동 검사와 사용자 표시에서 문제가 발견되지 않음
- `REVIEW`: 사용자가 누락·환각·오인식 시점을 표시함
- `WARN`: 지연·메모리·GPU 등 권장 기준을 넘음
- `FAIL`: 엔진 오류, 복구 실패, 저장 불일치처럼 즉시 확인할 문제가 있음

`REVIEW`는 프로그램 고장을 뜻하지 않는다. 사람이 표시한 구간을 튜닝 자료로 검토하라는 의미다.

## 강제 종료 복구

정상 종료되지 않아 SQLite에서 열린 상태로 남은 세션은 다음 자막 실행 또는 검증 실행 때 확인된다.

- 기록된 프로세스가 살아 있으면 건드리지 않는다.
- 프로세스가 종료된 세션만 마지막 자막 시각 기준으로 닫는다.
- 세션 메타데이터에 `unclean_shutdown_recovered`와 복구 시각을 남긴다.
- 해당 로그 폴더가 있으면 방송 리포트를 다시 생성한다.

## 아카이브 관리

`http://127.0.0.1:8766/history`의 `아카이브 관리`에서 실행한다.

- `전체 아카이브 백업`: SQLite의 일관된 스냅샷과 manifest를 ZIP으로 저장
- `기간 정리`: 지정 일수보다 오래된 종료 세션만 정리
- `선택 방송 삭제`: 현재 선택한 종료 세션 한 개만 정리

기간 정리와 선택 삭제는 먼저 `reports/archive_backups`에 백업 ZIP을 만든다. 실행 중인 열린 세션은 삭제할 수 없다. SQLite의 세션·자막·표시만 삭제하며 `logs/<session>` 원본 로그는 자동 삭제하지 않는다.

## 로컬 API

- `GET /api/archive/info`
- `GET /api/archive/annotations?session_id=<id>`
- `POST /api/archive/annotation`
- `POST /api/archive/annotation-update`
- `POST /api/archive/annotation-delete`
- `POST /api/archive/export`
- `POST /api/archive/delete-session`
- `POST /api/archive/prune`

개별 삭제는 실수 방지를 위해 `session_id`와 같은 값의 `confirm_session_id`가 필요하다. 표시 메모 고치기는 `session_id`, `annotation_id`, `note`(최대 1000자)를 보내며 메모만 바뀐다.

## 업데이트와 개인정보

업데이트 ZIP은 `config.json`, 유행어·보호 단어 파일, `data/transcripts.sqlite3`, 로그, 샘플 음성을 포함하거나 덮어쓰지 않는다. 아카이브 백업 ZIP에는 SQLite 자막 기록이 포함되므로 외부 전달 전에 내용을 확인한다. 일반 지원 묶음에는 원본 음성과 SQLite 전체 DB가 포함되지 않는다.

## 남은 실제 검증

자동 테스트는 데이터 계약·API·복구·백업·화면 렌더링을 확인한다. 실제 4060 Ti 방송 환경의 마이크 특성, 방 소음, 말투, 장시간 GPU 지연과 의미상 정확도는 실제 방송 또는 녹음 테스트에서 최종 확인해야 한다. 이때 `/validate` 표시와 지원 묶음을 함께 사용하면 로그만 전달하는 것보다 문제 원인을 훨씬 정확하게 찾을 수 있다.
