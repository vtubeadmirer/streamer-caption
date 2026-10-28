# 정확 우선 자막 엔진 (SimulStreaming)

작성: 2026-10-29 · 상태: **2단계(앱 통합) 완료, 아직 배포하지 않음** — 3단계(선택 내려받기)가 끝나야 사용자에게 나갑니다.

## 1. 한 줄 요약

관리자 페이지의 **자막 엔진**에서 고릅니다.

| 엔진 | 화면에 보이는 모습 | 쓰는 프로그램 |
|---|---|---|
| **기본 (빠름)** — 기본값 | 말하자마자 회색 미리보기가 뜨고, 말이 끝나면 확정 자막으로 바뀝니다. 미리보기가 틀리거나 "감사합니다" 같은 헛자막이 섞일 수 있습니다. | faster-whisper (CTranslate2) |
| **정확 우선** | 약 1.3초 늦게 뜨지만, 한 번 뜬 글자는 바뀌지 않습니다. 단어가 하나씩 흰 글씨로 늘어나고 문장이 끝나면 그대로 확정됩니다. 문제가 생기면 자동으로 기본 엔진으로 바뀝니다. | SimulStreaming + Whisper **large-v3-turbo** (고급 옵션: large-v3) |

정확 우선에 필요한 추가 구성요소(실행 환경 약 2.5 GB 내려받기 + turbo 모델 1.5 GB)가 없으면 선택지는 회색으로 보이고
"추가 구성요소가 필요합니다 — 다음 업데이트에서 내려받기 지원 예정"이라고 안내합니다.

## 2. 실험 결과 (2026-10-28~29, RTX 4060 Ti 16GB, 한국어 TTS 대본 10분, 1배속 실시간)

`C:\dev\simulstreaming-lab` 의 측정값(서버 단독)입니다. "첫 글자"는 말한 뒤 그 글자가 처음 나타나기까지, CER은 글자 오류율,
헛자막은 "감사합니다/시청해 주셔서/다음 영상" 같은 대본에 없는 문구 수입니다.

| 설정 | 첫 글자 p50 / p90 | CER | 헛자막 (확정/미리보기) | GPU 메모리 |
|---|---|---|---|---|
| 기존 엔진 turbo, beam 3, 청크 0.5 | 0.54 / 1.33초 (회색, 확정은 3.14초) | 1.0% | 0 / **69** | +1.6 GB |
| **SimulStreaming turbo, ft 40 — 정확 우선 기본값** | **1.29 / 1.57초** | **1.0%** | **0 / –** | torch 5.3 GB |
| SimulStreaming turbo, ft 25 | 1.07 / 1.33초 | 1.8% | 3 / – | torch 5.3 GB |
| SimulStreaming large-v3, ft 40 — 고급 옵션 | 1.46 / 1.80초 | 0.7% | 0 / – | torch 9.8 GB, 가드 없을 때 13.4 GB 까지 |
| SimulStreaming large-v3, ft 25 | 1.16 / 1.49초 | 4.1% | 8 / – | torch 9.8 GB |

- SimulStreaming 은 글자를 고치지 않으므로 "첫 글자 = 확정"입니다(조기 표시 생존율 100%).
- `frame_threshold` 40 은 25 보다 0.2~0.3초 늦지만 두 모델 모두 오류가 크게 줄고 헛자막이 0 이 되어 기본값입니다.
  12 는 더 빠르지만(0.97초) 오류율 9.5%, 헛자막 14개입니다. fp16(large-v3, min_chunk 0.3, ft 40)은 CER 2.0%로 fp32 보다 나빴습니다.
- **turbo 를 기본으로 한 이유**: large-v3 는 0.3%p 더 정확하지만 메모리를 두 배 쓰고(9.8 GB, 폭주 시 13.4 GB) OBS·게임과
  함께 쓰기에 빠듯합니다. large-v3 는 고급 옵션이며, 여유 메모리가 부족하거나 메모리 부족으로 꺼지면 turbo 로 대신합니다.
- 50분 연속 실행(turbo, ft 25): 서버 생존, 메모리 증가 없음(예약 5,250 → 5,260 MiB), 처리 지연 증가 없음, 갱신 1회 최대 0.66초.
- 공식 서버를 그대로 쓰면 한국어에서 약 3분 뒤 **죽거나**(토크나이저 IndexError), 문장 끝 처리에서 50초 넘게 **멈췄습니다**.
  아래 4장의 수정으로 두 문제가 사라졌습니다.

## 3. 앱 안에서의 동작

```
마이크 ──► VAD/세그먼트(기존) ──► faster-whisper 대기 모델 (항상 켜 둠)
     └──► SimulStreaming 서버 (별도 프로세스, TCP) ──► 증분 단어 ──► 자막 조립 ──► 오버레이·기록·요약
```

1. 앱이 켜지면 먼저 faster-whisper 모델을 올립니다. 정확 우선 서버가 모델을 읽는 20초 남짓 동안은 기본 엔진이 자막을 냅니다.
2. 서버가 준비되면 **말이 잠깐 멈춘 순간**(세그먼트가 열려 있지 않고, 기본 엔진의 확정 작업도 없을 때)에 전환합니다.
   한 문장이 두 엔진에 나뉘지 않습니다. 20초 안에 쉬는 순간이 없으면 그냥 전환합니다.
3. **서버 메시지의 `text` 는 새로 나온 조각뿐입니다**(" 오늘", "은 날씨", "가" …). 앱은 조각을 현재 줄에 이어 붙여 흰 글씨
   (`caption_pending` + `metadata.stable`)로 보여 주고, 문장부호 뒤에 다음 단어가 오면, 말이 끝나면(VAC `is_final`), 또는 한 줄
   글자 수(`caption.max_chars_per_line`)를 넘으면 그 줄을 확정(`caption_confirmed`)합니다. 오버레이는 같은 화면 갱신에서
   흰 줄을 확정 줄로 바꿉니다(깜빡임 없음). (예전 실험 어댑터는 조각을 전체 문장으로 덮어써서 CER 90% 넘는 조각 자막이 났습니다.)
4. 한 음절 단어("네", "도")도 보입니다: 정확 우선 줄에는 `pending_min_chars` 와 한 글자 필터를 적용하지 않습니다.
   문장부호만 남은 조각(".")은 따로 자막이 되지 않습니다. 그 밖의 방어 필터(환각 문구, 반복, 차단어)는 그대로 적용합니다.
5. 유행어 교정, 문장 나누기, 15분 요약, 방송 기록, 세션 로그는 기존 엔진과 똑같이 거칩니다.
6. **마이크 + PC 듀얼**: GPU 하나에 모델 하나만 올리므로 정확 우선은 **마이크**를 맡고 PC 소리는 기본 엔진이 맡습니다.
   PC 소리만 쓰는 경우에는 PC 소리를 정확 우선이 맡습니다.
7. 오디오는 별도 스레드가 막힘 없는 소켓으로 보냅니다. 서버가 몇 초 멈춰도 캡처 루프가 멈추거나 끊기지 않습니다
   (예전 실험 어댑터는 0.25초 소켓 시간 제한이 보내기에도 걸려 2~4초 멈춤에 세션이 끝났습니다).

## 4. 공식 소스에 대한 수정 (래퍼 방식)

`experiments/vendor/SimulStreaming` 은 ufal/SimulStreaming **077ea37** 과 바이트 단위로 같습니다(줄바꿈 제외).
수정은 모두 `engines/simulstreaming/server.py` 가 시작할 때 덧씌웁니다. 이렇게 한 이유:

- 업스트림을 새 버전으로 바꿀 때 폴더를 통째로 교체하면 됩니다. 수정 내역이 한 파일에 모여 있습니다.
- 덧씌울 함수가 없으면(업스트림 구조가 바뀌면) 서버가 시작 단계에서 종료 코드 3으로 끝나고 앱은 기본 엔진을 씁니다.
  고치지 않은 코드가 몰래 도는 일이 없습니다.
- 라이선스 출처가 분명합니다(7장).

| 수정 | 이유 (실험 런) |
|---|---|
| `Tokenizer.split_tokens_on_unicode` 범위 검사 | 한글 한 글자가 바이트 토큰 두 개에 걸칠 때 IndexError 로 서버가 죽음 (S0, 3분 10초) |
| `hide_incomplete_unicode` 가 끝의 미완성 UTF-8 조각 전체를 다음으로 넘김 | 앞 글자까지 잘려 깨진 한글 (S1a, 10분에 57자) |
| greedy 디코더 폭주·중복 방지 (같은 토큰·패턴 반복, 문장 처음부터 다시 받아쓰기, 120토큰 초과 시 멈춤) | " . . . ." 반복으로 53초 멈춤과 13.4 GB VRAM (S1u), 같은 문장 두 번 |
| Silero VAC 를 로컬 사본에서만 읽음: 실행 환경의 `models/silero_vad.jit` → silero-vad 패키지 → 기존 torch.hub 캐시(`trust_repo=True`) | 업스트림은 trust_repo 없이 torch.hub 로 GitHub master(고정 안 됨)를 받음. torch 2.14 는 신뢰 확인 질문을 띄우고, 입력이 닫혀 있으면 시작 단계에서 죽음 |
| 모델 파일을 있는 그대로 사용 (`whisper._download`) | 업스트림은 시작할 때마다 3 GB 파일 전체를 메모리에 읽어 SHA-256 을 계산하고, 다르면 인터넷에서 다시 받음 |
| 서버 루프를 새로 작성 | PCM16 바이트가 홀수로 끊길 때 정렬 유지, 1초마다 heartbeat, 로그에 자막 글자 미기록, 원인별 종료 코드, 앱 프로세스가 끝나면(`--parent-pid`, Windows 프로세스 핸들 대기) 바로 종료 — 앱이 강제 종료돼도 VRAM 을 붙잡은 서버가 남지 않음 |

- **모델 파일 이름은 반드시 `large-v3-turbo.pt` / `large-v3.pt`** 그대로여야 합니다. SimulStreaming 이 단어 정렬에 쓰는
  어텐션 헤드를 파일 이름으로 고르기 때문입니다. 다른 이름이면 앱이 그 파일을 쓰지 않고, 서버도 시작하지 않습니다(종료 코드 5).
- 업스트림 기본 로그 수준은 DEBUG 라서 인식된 단어를 모두 로그에 씁니다. 래퍼는 WARNING 으로 고정하고, 서버 로그
  (`logs/<세션>/simulstreaming_server.log`)에는 상태·원인·시간만 남깁니다. 버그 번들은 `.log` 파일을 넣지 않습니다.
- 종료 코드: 3 시작 실패 · 4 CUDA 없음 · 5 모델 파일 없음/이름 다름 · 6 실행 환경 없음(Silero 포함) · 7 그래픽 메모리 부족 ·
  8 모델 갱신 멈춤 · 9 그 밖의 오류 · 10 앱 프로세스 종료.
- 서버의 표준입력은 비워 둡니다(`DEVNULL`). 처음에는 앱 종료 감지를 표준입력 파이프의 EOF 로 했는데, Windows 에서 다른
  스레드가 그 파이프를 읽고 있으면 `import torch` 중 표준입력을 확인하는 호출이 멈춰 서버가 아예 뜨지 않았습니다(통합 실행에서 발견).

### 고치지 않은 업스트림·앱 문제

- 문장 끝 헛문구는 `frame_threshold` 25 에서 나오고 40 에서는 10분에 0개였습니다. 그래서 기존 헛자막 필터를 정확 우선 출력에도 그대로 둡니다.
- 드물게 "....." 반복 때문에 한 문장이 통째로 빠질 수 있습니다(가드가 반복을 끊지만 그 문장은 비어 나옵니다).
- **앱의 "자막" 필터**: 방어 필터가 "자막"이 들어간 30자 이하 자막을 환각으로 보고 숨겨서 진짜 문장도 사라집니다(두 엔진 공통,
  10분 통합 실행에서 5줄). 이번 변경에서는 고치지 않고, 따로 모아 고칠 목록(PENDING_FIXES)으로 넘겼습니다.

## 5. 자동 전환 (fallback)

정확 우선이 동작하는 동안에도 기본 엔진의 VAD 세그먼트는 계속 만들어지고, 처리하지 않은 채 보관됩니다.

| 원인 (`reason`) | 감지 방법 | 다시 시도 |
|---|---|---|
| `start_failed`, `start_timeout` | 서버가 준비 전에 종료, 또는 `connect_timeout_sec`(180초) 안에 접속 안 됨 | 예 |
| `runtime_missing`, `model_missing`, `source_missing`, `no_cuda` | 종료 코드 6/5/3/4, 파일 확인 | 아니오 |
| `not_enough_vram` | 시작 직전 `nvidia-smi` 여유 메모리가 모델 요구량(turbo 6.3 GB, large-v3 11 GB)보다 적음 | 아니오 |
| `out_of_memory` | 종료 코드 7 (불러오는 중이든 방송 중이든) | large-v3 이면 turbo 로 한 번, turbo 면 아니오 |
| `crashed`, `connection_lost` | 프로세스 종료, 연결 끊김 | 예 |
| `stalled` | heartbeat 가 `stall_timeout_sec`(15초) 동안 없음, 보낸 음성이 30초 넘게 쌓임, 서버 자체 감시(모델 갱신 1회 30초 초과) | 예 |
| `too_slow` | 서버가 음성보다 `max_lag_sec`(10초) 넘게 뒤처진 상태가 5초 지속 | 예 |
| `internal_error` | 앱 쪽 정확 우선 처리 코드의 예외 (자막은 계속) | 아니오 |

전환할 때:

1. 화면의 흰 줄(이미 맞게 나온 글자)을 확정 자막으로 남깁니다.
2. 보관 중이던 세그먼트 가운데 정확 우선이 아직 받아쓰지 못한 부분을, 마지막으로 나온 단어의 끝 시각에서 잘라
   기본 엔진에 넘깁니다. 진행 중인 세그먼트도 같은 시각에서 잘라 처리합니다. 그래서 멈춤이 15초 동안 이어져도
   그 사이의 말이 빠지지 않고, 이미 나온 글자는 반복되지 않습니다.
3. 관리자 페이지 자막 엔진 칸에 "정확 우선 엔진이 응답하지 않아 기본 엔진으로 자막을 이어갑니다. 잠시 후 정확 우선
   엔진을 다시 시도합니다." 같은 안내를 띄우고, 세션 로그에 `engine_status` (`state`, `reason`, `model`, 숫자만) 를 남깁니다.
   자막 글자는 로그 사유에 들어가지 않습니다.
4. 다시 시도는 `max_restarts`(2회)까지, 30초 → 60초로 간격을 늘립니다. 다 쓰면 그 방송이 끝날 때까지 기본 엔진을 씁니다.
   앱을 다시 시작하면 처음부터 다시 시도합니다.

## 6. 설정 (`config.json`)

| 키 | 기본값 | 뜻 |
|---|---|---|
| `streaming.engine` | `"baseline"` | `"baseline"` 기본, `"simulstreaming"` 정확 우선 (관리자 페이지에서 바꿈, 재시작 필요) |
| `simulstreaming.model` | `"large-v3-turbo"` | `"large-v3"` 는 고급 옵션 (여유 메모리 11 GB 미만이거나 메모리 부족이면 turbo) |
| `simulstreaming.frame_threshold` | `40` | 클수록 늦지만 정확 |
| `simulstreaming.min_chunk_size` / `vac_chunk_size` / `beams` | `0.5` / `0.04` / `1` | 실험에서 가장 좋았던 값 (가드는 greedy 디코더 전제) |
| `simulstreaming.python_executable`, `model_dir`, `model_path` | `""` | 비우면 7장의 기본 위치를 찾음. `model_path` 는 파일 이름이 정확해야 함 |
| `simulstreaming.stall_timeout_sec`, `max_lag_sec`, `max_restarts`, `retry_delay_sec` | 15, 10, 2, 30 | 자동 전환 기준 |
| `simulstreaming.port` | `0` | 서버가 빈 로컬 포트를 직접 열고 그 번호를 파일로 알림 (앱이 미리 골라 두면 모델을 읽는 20초 사이 다른 프로그램이 가져갈 수 있었음) |

설정 스키마 4: 예전 `config.json` 에 그대로 저장돼 있던 실험값(0.8 / 0.32 / 25 / 43007 / 90초)은 위 기본값으로 한 번 바뀝니다.
사용자가 직접 바꾼 값은 그대로 둡니다. 개발 중 잠깐 쓰인 `model: "auto"` 는 읽을 때 turbo 로 바뀝니다.

## 7. 파일 위치, 실행 환경, 용량, 라이선스

찾는 순서 (`engines/simulstreaming/availability.py`):

1. `simulstreaming.python_executable` / `model_dir` / `model_path` 설정값
2. 설치본: `<설치 폴더>\simulstreaming\.venv\Scripts\python.exe`, `...\simulstreaming\models\large-v3-turbo.pt`
   (고급: `large-v3.pt`), `...\simulstreaming\models\silero_vad.jit` — **3단계 내려받기가 여기에 풉니다**
3. 개발 폴더: `experiments\simulstreaming-runtime\` (`setup_simulstreaming_experiment.bat`, START_ADVANCED 메뉴 AK)

실행 환경 (`requirements_simulstreaming_windows.txt`, 실험 venv 와 같은 버전): torch **2.14.1+cu126**(cu128 빌드는 2.11 에서
멈춤), librosa 1.0.0, tiktoken 0.14.0, tqdm 4.70.1. torchaudio 는 필요 없습니다. silero-vad pip 패키지는 torchaudio 를
끌어오므로 쓰지 않고, `silero_vad.jit` 모델 파일 하나(약 2 MB)를 models 폴더에 둡니다.

| 구성요소 | 크기 | 라이선스 |
|---|---|---|
| SimulStreaming 소스 (앱에 포함, `experiments/vendor/SimulStreaming`) | 수 MB | MIT (2025-10-22 부터), Copyright (c) 2025 Charles University |
| 그 안의 Whisper-Streaming 파생 코드 · OpenAI Whisper 코드 · Silero VAD iterator | – | MIT |
| 그 안의 **Simul-Whisper** 파생 코드 (`simulstreaming/whisper/simul_whisper/`) | – | **Apache-2.0** — 전문과 수정 고지를 `THIRD_PARTY_NOTICES.md` 부록 A·B 에 넣었습니다 |
| PyTorch CUDA 실행 환경 | 내려받기 약 2.5 GB, 설치 후 약 4.5 GB | PyTorch BSD-3-Clause, CUDA 구성요소는 NVIDIA 약관, librosa ISC, tiktoken MIT |
| `large-v3-turbo.pt` (기본) | 1.5 GB (1,617,941,637 B) | MIT (OpenAI Whisper 가중치) |
| `large-v3.pt` (고급, 선택) | 2.9 GB (3,087,371,615 B) | MIT (OpenAI Whisper 가중치) |
| `silero_vad.jit` | 약 2 MB | MIT |

## 8. 2단계 통합 실행 결과 (실제 앱 파이프라인)

`main.run_v2_light` 를 그대로 쓰고 마이크 대신 WAV 를 1배속으로 넣었습니다(`clip10.wav` = 실험 대본 음성의 앞 10분,
앞에 60초 무음을 두어 앱이 다 켜진 뒤 말이 시작되게 함). 기본 엔진 대기 모델은 사용자 설치본과 같은 turbo / beam 3 / int8_float16.

| 설정 | 첫 글자 p50 / p90 | CER (엔진 원문) | CER 앱 확정 자막 (숨긴 줄 포함 / 실제 표시) | 헛자막 | 깨진 글자 | GPU 메모리 (기본 엔진 대기 모델 포함) | 실험 (서버 단독) |
|---|---|---|---|---|---|---|---|
| **turbo, ft 40 (기본값)** | **1.32 / 1.60초** | **0.98%** | 0.98% / 4.75% | **0** | 0 | +6.5 GB (전체 최대 7.4 GB) | S12: 1.29 / 1.57초, 0.98%, 헛자막 0 |
| large-v3, ft 40 (고급) | 1.37 / 1.68초 | 0.76% | 0.76% / 4.54% | 0 | 0 | +10.9 GB (전체 최대 11.8 GB) | S5: 1.46 / 1.80초, 0.72%, 헛자막 0 |

- 앱을 거친 결과가 서버 단독 실험과 같습니다. 숨긴 줄까지 넣으면 앱 확정 자막의 CER 이 엔진 원문과 똑같습니다
  (조각 이어 붙이기·문장 나누기·유행어 처리에서 생긴 오류 없음). 실제 표시 CER 이 높은 것은 4장의 "자막" 필터가
  진짜 문장 5줄(89자)을 숨겼기 때문입니다 — 이 필터는 PENDING_FIXES 에서 따로 고칩니다.
- turbo: 흰 줄 갱신 710번, 확정 자막 163줄, 회색 미리보기 0번, 문자의 76% 가 말한 뒤 1.5초 안에 화면에 나옴.
  서버 준비 약 13초(가져오기 3.7초 + 모델 8.3초), 앱 시작부터 정확 우선 전환까지 약 13초.

**서버를 강제로 끈 시험 1 — 문장 사이 (turbo, 10분 전체, 150초에 서버 프로세스 종료)**

- 150.8초 종료 → 151.2초에 `crashed` 감지, 기본 엔진으로 전환 (0.5초).
- 기본 엔진이 바로 다음 문장부터 확정 자막을 냄(종료 후 60초 동안 14줄). 같은 문장 반복도, 빠진 문장도 없음.
- 다시 시도: 181.3초 서버 다시 시작 → 195.8초 준비 → 199.9초 말이 멈춘 틈에 정확 우선 복귀(그 뒤 문장부터 다시 정확 우선).
- 종료 전후(−10 ~ +30초) 대본 150자 가운데 화면에 끝내 안 나온 글자 1자. 10분 전체 표시 CER 4.79% (끄지 않은 실행 4.75%).
  오류 이벤트 0, 앱 종료 코드 0.

**서버를 강제로 끈 시험 2 — 문장 한가운데 (turbo, 앞 3분, 103.8초에 종료)**

- 일곱 어절짜리 문장(102.0~107.2초)을 말하는 도중, 앞 두 어절이 흰 줄로 나온 뒤에 종료.
- 이미 나온 두 어절은 전환(0.6초 뒤) 때 확정 자막으로 남고, 기본 엔진은 그 어절이 끝난 지점부터 잘라 받아써서 나머지
  다섯 어절만 냈습니다(두 줄). 반복·누락 없음.
- 30초 뒤 다시 시작해 16.5초 만에 준비, 정확 우선으로 복귀.

## 9. 3단계에서 남은 일 (선택 내려받기)

1. **내려받기 버튼**: 관리자 페이지 자막 엔진 칸의 "추가 구성요소가 필요합니다" 자리에 "내려받기 (약 4 GB)" 버튼.
   GPU 메모리(10 GB 이상)와 디스크 여유(약 7 GB)를 먼저 확인하고, 진행률을 업데이트 배너와 같은 방식으로 보여 줍니다.
2. **실행 환경 패키지**: torch 2.14.1+cu126 + librosa + tiktoken + tqdm 을 `runtime.yml` 처럼 별도 릴리스 자산으로 만들고
   SHA-256 을 `release-manifest.json` 에 적습니다. librosa 는 업스트림 import 때문에만 필요하므로(numba/scipy 수백 MB),
   가벼운 대체 모듈로 줄일 수 있는지 확인합니다.
3. **모델 파일**: 기본은 `large-v3-turbo.pt` 만 받습니다(OpenAI 공식 주소, SHA-256 은 URL 에 있음 — 서버는 시작 때 다시 해시하지
   않으므로 내려받을 때 한 번 확인). `large-v3.pt` 는 16 GB GPU 에서 고급 옵션으로만 받게 합니다. 파일 이름을 바꾸지 않습니다.
   `silero_vad.jit` 도 고정된 버전으로 함께 받습니다. 중단 후 이어받기(`.partial`).
4. **업데이트와의 관계**: 앱 업데이트(`versions\<버전>`)와 따로 두고, 실행 환경 버전을 `simulstreaming\RUNTIME_VERSION` 에
   적어 서버 래퍼와 호환성을 확인합니다. 지우기 버튼(디스크 회수)도 필요합니다.
5. **라이선스 묶음**: 내려받은 휠의 `*.dist-info/licenses` 를 `licenses.txt` 로 모으고 THIRD_PARTY_NOTICES 를 갱신합니다.
6. **실제 PC 확인**: 10~12 GB GPU 에서 turbo, OBS·게임과 함께 쓸 때 `too_slow`·`out_of_memory` 전환, 노트북 GPU, 30분 실제 마이크.
7. 릴리스 전 `docs/PUBLIC_RELEASE_CHECKLIST.md` 의 정확 우선 항목을 확인합니다.

## 10. 개발자용

- 상태 확인: `simulstreaming_probe.bat` (START_ADVANCED AJ) — 파일, GPU 여유 메모리, 모델 선택, 실행 환경 자체 점검(`server.py --self-check`)
- 한 번만 정확 우선으로 실행: `run_simulstreaming_experiment.bat` (AI) = `main.py --simulstreaming-experiment` = v2_light + 정확 우선
- 테스트(GPU 없음): `tests/test_simulstreaming_engine.py` (가짜 서버 `tests/simul_fake_server.py` 로 시작·정상·충돌·멈춤·
  메모리 부족·지연·재시도·더 가벼운 모델·앱 종료·전체 파이프라인·듀얼 입력), `tests/test_admin_engine_api.py`,
  `tests/test_caption_overlay_stable_pending.mjs`, `tests/test_support_bundle_privacy.py`
