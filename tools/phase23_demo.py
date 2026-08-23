from __future__ import annotations

import argparse
import copy
import socket
import sys
import time
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.admin_server import AdminServer, AdminState
from core.config import load_config
from core.transcript_archive import TranscriptArchive
from features.broadcast_report import write_session_report


DEMO_CAPTIONS = [
    (8, "오늘은 방송 자막 설정과 유행어 사전을 같이 점검해 보겠습니다."),
    (105, "샌드위치를 샹도웨쭈라고 말해도 같은 단어로 바뀌는지 확인할게요."),
    (245, "마이크 입력은 잘 들어오고 있는데 조용한 말은 가끔 빠집니다."),
    (390, "Silero 기준값을 조금 낮추면 작은 목소리도 더 잘 잡힙니다."),
    (545, "대신 기준을 너무 낮추면 키보드 소리가 말로 잡힐 수 있어요."),
    (710, "자막은 두 줄로 유지하고 긴 문장은 자연스럽게 나누겠습니다."),
    (875, "방금 샹도웨쭈 발음은 제대로 치환됐네요!"),
    (1040, "새로 온 시청자는 지난 30분 요약을 먼저 보면 됩니다."),
    (1240, "검색에서 샌드위치를 누르면 말한 시점으로 바로 이동합니다."),
    (1510, "첫 번째 설정 점검은 여기까지 하겠습니다."),
    (1810, "이제 게임 레이드 이야기로 넘어가 볼게요."),
    (1935, "오늘 보스 패턴은 첫 장판 다음에 바로 모이는 방식입니다."),
    (2080, "딜 타이밍을 놓치면 다음 페이즈가 길어져요."),
    (2225, "방금 회피 진짜 아슬아슬했어요 ㅋㅋ"),
    (2390, "파티원 두 명이 쓰러졌지만 부활 타이밍은 남아 있습니다."),
    (2540, "이번에는 오른쪽 안전 지대로 먼저 이동하겠습니다."),
    (2710, "보스 체력이 10퍼센트 남았습니다!"),
    (2870, "마지막 궁극기 들어가고 레이드 성공했습니다."),
    (3040, "이 장면은 하이라이트 후보로 남겨도 좋겠네요."),
    (3350, "다음 방송에서도 같은 파티로 도전해 보겠습니다."),
    (3610, "마지막 주제는 다음 달 여행과 맛집 일정입니다."),
    (3745, "첫날에는 부산역 근처에서 점심을 먹을 예정이에요."),
    (3890, "저녁에는 해운대 쪽 식당을 예약하려고 합니다."),
    (4040, "시청자 추천 메뉴 중에서는 돼지국밥이 가장 많았습니다."),
    (4205, "둘째 날 비가 오면 실내 전시를 먼저 보겠습니다."),
    (4370, "날씨가 좋으면 바닷가 산책을 길게 할 수 있어요."),
    (4535, "여행 경비와 이동 시간도 방송 전에 다시 정리할게요."),
    (4700, "추천 장소는 채팅 기록에서 검색해서 목록으로 만들겠습니다."),
    (4880, "다시보기 주소를 연결하면 이 시점도 쉽게 찾을 수 있습니다."),
    (5120, "오늘 방송은 여기까지입니다. 감사합니다!"),
]


def find_available_port(host: str, preferred: int) -> int:
    for port in (preferred, 8877, 8977, 9077):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No Phase 2/3 preview port is available.")


def seed_demo(db_path: Path) -> tuple[TranscriptArchive, str]:
    archive = TranscriptArchive(db_path, bucket_minutes=30)
    session_id = "phase23-demo-session"
    archive.create_session(
        session_id,
        started_at="2026-07-11T19:00:00+09:00",
        source="microphone",
        engine="demo",
        metadata={"mode": "phase23_demo", "synthetic": True},
    )
    archive.update_session_meta(
        session_id,
        title="Phase 2·3 기능 데모 방송",
        vod_start_offset_sec=12,
    )
    for offset, text in DEMO_CAPTIONS:
        archive.add_caption(
            session_id,
            text=text,
            offset_sec=offset,
            confidence=0.92,
            engine="demo",
            created_at="2026-07-11T19:00:00+09:00",
            metadata={"synthetic": True},
        )
    existing_slang_events = {
        (item["action"], item["source"], item["target"])
        for item in archive.list_slang_history(limit=1000)
    }
    for action, source, target in (
        ("added", "샹도웨쭈", "샌드위치"),
        ("updated", "산도웨쭈", "샌드위치"),
    ):
        if (action, source, target) not in existing_slang_events:
            archive.record_slang_change(
                action=action,
                source=source,
                target=target,
                metadata={"synthetic": True},
            )
    archive.close_session(
        session_id,
        ended_at="2026-07-11T20:28:00+09:00",
        metadata={"demo_complete": True},
    )
    return archive, session_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2/3 archive prototype")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "reports" / "phase23_demo" / "transcripts.sqlite3",
    )
    args = parser.parse_args()

    db_path = args.db.resolve()
    archive, session_id = seed_demo(db_path)
    output_dir = db_path.parent
    report_paths = write_session_report(
        archive,
        session_id,
        output_dir,
        load_config(),
    )
    print(f"demo_db: {db_path}")
    print(f"demo_report: {report_paths['markdown']}")
    if not args.serve:
        return 0

    config = copy.deepcopy(load_config())
    host = "127.0.0.1"
    port = find_available_port(host, args.port)
    config["server"]["host"] = host
    config["server"]["admin_port"] = port
    config["archive"]["db_path"] = str(db_path)
    server = AdminServer(config, AdminState())
    server.start_background()
    url = f"http://{host}:{port}/history"
    print("Phase 2/3 preview only. GPU, Whisper, and microphone are not running.")
    print(f"preview_url: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        print("Preview stopped.")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

