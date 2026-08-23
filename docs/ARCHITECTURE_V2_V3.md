# 2차/3차 개발 아키텍처

## 목표

이 문서는 현재 v2_light를 안정적인 방송용 기반으로 완성한 뒤, 3차에서
스트리밍 STT 엔진을 교체할 수 있도록 경계를 다시 정의한다.

우선순위는 다음과 같다.

1. RTX 4060 Ti 16GB에서 모델 하나만 유지한다.
2. 마이크와 PC 재생음을 동일한 오디오 계약으로 처리한다.
3. 임시 자막 때문에 확정 자막이 늦어지거나 사라지지 않게 한다.
4. 침묵 환각과 실제 발화를 구분할 근거를 로그에 남긴다.
5. 멈춘 네이티브 CUDA 호출은 Python 스레드가 아니라 프로세스 경계에서 복구한다.

## 현재 데이터 흐름

MicrophoneChunkSource / SystemAudioChunkSource
-> AudioChunk contract
-> VAD + AudioRingBuffer
-> PendingWindowScheduler
-> SttWorker (confirmed first)
-> BaselineEngine
-> DefenseFilter
-> SlangDictionary
-> WebSocket + admin + JSONL

ProcessSttWorker는 STT 모델만 자식 프로세스에 두고, main.py는 오디오,
VAD, 링버퍼, 확정 작업 큐, 관리자 서버를 계속 유지한다. run_supervised.py는
오디오 복구 한도 초과 또는 STT 자식 복구 한도 초과 때만 전체 앱을 다시
시작하는 마지막 안전망이다.

모든 입력 소스는 다음 계약을 지킨다.

- 16 kHz float32 mono
- 절대 샘플 기준의 offset_sec
- RMS, clipping 비율, 장치 오류, 재시작 횟수
- chunks(max_seconds) 반복자
- 오류 시 제한 횟수만큼 장치 다시 열기

PC 재생음 모드는 선택한 Windows 출력 장치로 나가는 전체 소리를 캡처한다.
특정 브라우저나 앱 한 개만 분리하지 않으며 알림음, 음악, 게임 소리도 포함될
수 있다.

## 2차 개발: 안정화 기반

### 구현 완료

- create_audio_chunk_source()로 마이크/PC 재생음 선택
- SoundCard WASAPI loopback 입력과 멀티채널 mono downmix
- 마이크 callback 정지 감지와 장치 자동 재연결
- 확정 STT 큐 우선 처리
- GPU가 바쁠 때 새 임시 자막 작업 생략
- 임시 작업 통계를 superseded와 skipped로 분리
- VAD 세그먼트의 실제 음성 비율 기록
- Whisper no_speech_prob, avg_logprob 기록
- 화이트리스트 이후에 음성 비율/무음 확률/신뢰도 필터 적용
- 20초 STT 지연 경고, 60초 정지 판정
- 복구 종료 코드 74/75를 감시하는 외부 프로세스 감독기
- 관리자 화면의 입력 소스, 출력 장치, 재연결 및 STT 상태 표시

dropped pending STT jobs는 확정 자막 손실을 뜻하지 않았다. 새 구조에서는
이 값을 다음과 같이 분리한다.

- superseded_pending_jobs: 더 최신 미리보기 또는 확정 구간 때문에 폐기
- skipped_pending_jobs: STT가 바쁠 때 애초에 만들지 않은 미리보기
- confirmed queue: 폐기하지 않음

### 구현된 2차 복구

- 부모가 active/queued confirmed 작업의 원본 audio와 sample range를 소유
- STT 자식 정지 또는 비정상 종료 시 active confirmed를 같은 job_id로 재전송
- 재전송 결과에 recovered, recovery_source, recovery_attempt 기록
- 새 자식이 준비되는 동안 들어온 confirmed 작업은 부모 큐에 순서대로 유지
- pending 작업만 교체 가능하며 confirmed 작업은 폐기하지 않음
- ConfirmedRangeLedger가 job_id와 sample range를 기준으로 중복 결과 차단
- capture_ring_end_sample과 마지막 committed sample을 로그에 기록

따라서 STT 자식만 실패한 경우에는 AudioRingBuffer를 다시 복사해 만들 필요
없이 부모가 보유한 정확한 확정 작업을 재전송한다. VAD가 아직 끝내지 않은
현재 발화도 부모 segmenter에 남아 있다가 이후 confirmed 작업으로 제출된다.

아직 남은 raw ring replay는 main.py 자체가 강제 종료된 경우의 복구다.
메모리 링버퍼는 프로세스 종료와 함께 사라지므로 이 범위까지 보장하려면
암호화되지 않은 임시 WAV 또는 memory-mapped journal을 디스크에 남겨야
한다. 방송 음성의 개인정보와 디스크 쓰기 비용 때문에 기본 활성화하지
않고 별도 선택 기능으로 다룬다.

## 3차 개발: 엔진 프로세스 격리

### 3A 구현 완료

현재 기본 worker_mode는 process다.

Audio + VAD main process
-> ProcessSttWorker controller
-> single STT child process
-> Baseline faster-whisper adapter

- STT 자식만 모델을 소유해 VRAM 이중 점유 방지
- multiprocessing Queue로 job과 CaptionEvent 전달
- 20초 지연 경고
- 일반 작업 60초, 첫 모델 로딩 300초 제한
- 자식 시작 60초 제한
- 복구 한도 안에서는 STT 자식만 종료·재생성
- 복구 한도 초과 때만 run_supervised.py가 전체 앱 재시작
- 관리자 화면에서 process state, PID, restart, recovered 통계 확인
- worker_mode=thread로 이전 호환 경로 선택 가능

### 3B 남은 SimulStreaming 어댑터

공식 SimulStreaming은 faster-whisper CTranslate2 모델을 재사용하지 않는다.
PyTorch 기반 Whisper .pt 모델, AlignAtt 정책, 별도 requirements를 사용한다.
공식 문서는 large-v3에 최소 10GB VRAM을 권장하므로 4060 Ti 16GB 용량에는
들어갈 수 있지만, 현재 프로필과 다른 런타임 및 모델 캐시가 필요하다.

통합 조건:

1. Baseline 자식과 SimulStreaming 자식을 동시에 띄우지 않음
2. 동일 IPC의 engine factory만 교체
3. 공식 JSONL의 partial text는 caption_pending으로 변환
4. is_final 결과만 caption_confirmed로 커밋
5. 4060 Ti에서 peak VRAM, 첫 로딩 시간, 30분 정지율 비교
6. 기준 미달 또는 import 실패 시 Baseline 프로필로 복귀

현재 Queue는 오디오 배열을 복사한다. 30분 테스트에서 CPU 복사 비용이
문제가 될 때만 shared_memory handle 방식으로 바꾼다.

공식 저장소:
https://github.com/ufal/SimulStreaming

## 환각 방어 순서

필터 순서는 다음과 같이 고정한다.

1. 보호 단어/화이트리스트
2. VAD 음성 비율
3. Whisper 무음 확률
4. STT 신뢰도
5. 한 글자/과도한 반복
6. 자막 크레디트 형태
7. 금칙어 마스킹

안녕하세요, 감사합니다, 시청해 주셔서 감사합니다 같은 실제 발화 내용
자체만으로 숨기지 않는다. 침묵 환각 여부는 음향 근거와 함께 판단한다.

## 4060 Ti 검증 기준

- 30분 실시간 발화에서 프로세스 정지 0회
- STT 자동 재시작이 발생하면 90초 안에 자막 복귀
- confirmed job 손실 0
- idle 10분 동안 보이는 환각 자막 0
- PC 재생음 10분 테스트에서 offset 역행 0
- VRAM peak가 프로필 제한 내에 있고 모델 프로세스 중복 0
- 관리자 화면에서 source/restart/stall 원인을 확인 가능

실제 4060 Ti 검증 전에는 2차 완료로 표시하지 않는다.
## SimulStreaming Implementation Addendum (2026-07-11)

This addendum supersedes the earlier same-IPC engine-factory proposal. The official runtime owns continuous streaming/VAC state, so it is integrated through a dedicated TCP path rather than the segment STT job queue.

- Continuous 16 kHz PCM chunks go to the official server.
- Non-final JSON becomes `caption_pending`.
- `is_final` JSON becomes `caption_confirmed`.
- Existing post-processing, WebSocket, admin state, and logs remain downstream.
- The normal faster-whisper process worker and recovery ledger are unchanged.
- Official source is pinned in `experiments/vendor/SimulStreaming` at commit `077ea37`.
- RTX 5070 Ti large-v3 + VAC TCP validation passed; RTX 4060 Ti remains the promotion gate.
- Full measurements and setup steps are in `docs/SIMULSTREAMING_EXPERIMENT.md`.