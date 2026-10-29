"""Text that Whisper-family models produce from silence, music or noise.

They learned it from YouTube subtitles: outro lines ("감사합니다.", "시청해 주셔서 감사합니다",
"다음 영상에서 만나요", "구독과 좋아요 부탁드립니다", "MBC 뉴스 ○○○입니다") and subtitle
credits ("한글자막 by 한효주", "자막 제공: 배달의 민족", "Subtitles by the Amara.org
community").

* is_subtitle_credit(): a credit line is never something a streamer says, so DefenseFilter hides
  it on text alone. It must not match ordinary sentences about captions ("지금 보시는 자막은 …",
  "자막 설정 좀 바꿔볼게요"): this app is a caption program and streamers talk about it.
* is_phantom_line(): a whole line that is only an outro phrase. Streamers say these too
  ("감사합니다!" to a donor), so DefenseFilter hides one only when the app's own voice detector
  heard little speech in that line's audio (see defense.phantom_max_speech_share). A phrase inside
  a longer line ("망치즈 감사합니다 고맙습니다") is never matched.
* strip_phantom_sentences(): drops whole phantom sentences from a gray preview hypothesis, where
  Whisper adds them to short audio windows ("오늘 날씨가. 감사합니다.").
"""
from __future__ import annotations

import re

_HANGUL_LATIN_DIGITS = re.compile(r"[^0-9a-z가-힣぀-ヿ一-鿿]")

# Matched against the whole line with spaces and punctuation removed, lowercased.
_THANKS = r"(?:정말|진짜|너무|대단히|매우)?(?:감사(?:합니다|드립니다|드려요|해요|했습니다)|고맙(?:습니다|습니당))"
_PHANTOM_PATTERNS = (
    rf"(?:{_THANKS}){{1,3}}",
    # 시청해 주셔서 감사합니다 / 오늘도 봐주셔서 고맙습니다 / 끝까지 시청해주셔서 감사합니다
    rf"(?:오늘도|오늘|끝까지|지금까지)?(?:시청|청취|봐|들어)(?:해)?(?:주셔서|줘서|주신)(?:분들)?(?:{_THANKS})?",
    # 다음 영상에서 만나요 / 다음 시간에 뵙겠습니다 / 다음에 또 만나요
    r"(?:그럼|그러면|그럼이만)?다음(?:영상|시간|방송|편|회|주)?(?:에서|에)?(?:또)?"
    r"(?:만나요|만나겠습니다|만납시다|봬요|뵈어요|뵙겠습니다|뵐게요|봐요|만나)",
    # 구독과 좋아요 부탁드립니다 / 구독 좋아요 알림설정 / 좋아요 눌러주세요
    r"(?:(?:구독|좋아요|알림(?:설정)?|댓글)(?:과|와|하고|랑|및|그리고)?){2,4}"
    r"(?:부탁(?:드립니다|드려요|해요|합니다)|눌러(?:주세요|주시면)|해(?:주세요|주시면)|해주세요)?",
    r"(?:구독|좋아요|알림설정|댓글)(?:부탁(?:드립니다|드려요|해요|합니다)|눌러주세요|해주세요)",
    # MBC 뉴스 이덕영입니다
    r"(?:mbc|kbs|sbs|ytn|jtbc|obs|tvchosun|채널a|mbn|연합)뉴스[가-힣]{2,4}(?:입니다|이었습니다|였습니다)",
    r"안녕하십니까",
    r"(?:thankyou|thanks)(?:forwatching|verymuch|somuch|forlistening)?",
    r"(?:pleasesubscribe|seeyounexttime|seeyouinthenextvideo)",
    r"ご視聴ありがとうございました",
)
_PHANTOM = re.compile("|".join(f"(?:{pattern})" for pattern in _PHANTOM_PATTERNS))

# Subtitle credits found anywhere in a line.
_CREDIT_ANYWHERE = re.compile(
    r"자막by"
    r"|subtitle[sd]?by|captions?by|captioned?by|translated?by|transcribed?by|amara\.org"
    r"|字幕(?:by|提供|制作|製作|翻译|翻譯|由)"
)
# Whole-line credits: a credit head, then at most a short name ("한효주", "배달의 민족").
_CREDIT_HEAD = re.compile(
    r"^(?:"
    r"(?:한글|한국어|영어|영문|일본어)?자막(?:제공|제작|편집|번역|협찬)(?:및(?:자막)?(?:제공|제작|편집|번역))?[:：]?"
    r"|(?:한글|한국어|영어|영문|일본어)?자막[:：]"
    r"|시청자자막(?:제공)?[:：]?"
    r"|(?:영상)?(?:편집|제작|번역|촬영|기획|연출)[:：]"
    r")"
)
_NAME = re.compile(r"^[0-9a-z가-힣.&_\-]{0,12}$")
# A tail like "기능도 있어요" or "프로그램입니다" is a sentence about captions, not a name.
_SENTENCE_END = re.compile(r"(?:요|다|까|죠|게|래|네|지|야|어|아|함|음|임|니다|세요)$")
_NOT_A_NAME = ("프로그램", "기능", "설정", "크기", "글자", "색", "위치", "화면", "창", "속도", "파일", "앱",
               "방송", "테스트", "켜", "꺼", "시간")
_PARTICLES = {"도", "을", "를", "은", "는", "이", "가", "의", "만", "랑", "과", "와"}


def compact(text: str) -> str:
    """Lowercase letters, digits and Hangul only: "감사합니다." and "감사 합니다!" compare equal."""
    return _HANGUL_LATIN_DIGITS.sub("", str(text or "").lower())


def is_phantom_line(text: str) -> bool:
    value = compact(text)
    return bool(value) and _PHANTOM.fullmatch(value) is not None


def is_subtitle_credit(text: str) -> bool:
    spaced = re.sub(r"\s+", "", str(text or "").lower())
    if not spaced:
        return False
    if _CREDIT_ANYWHERE.search(spaced):
        return True
    if spaced.startswith(("©", "ⓒ", "(c)")):
        return True
    core = spaced.strip("[](){}<>「」『』【】〈〉\"'“”‘’.,!?~-_·…")
    if core.startswith(("©", "ⓒ")):
        return True
    head = _CREDIT_HEAD.match(core)
    if head is None:
        return False
    name = core[head.end():].strip(":：.,!?~-")
    if not name:
        return True
    if not _NAME.match(name) or _SENTENCE_END.search(name) or name in _PARTICLES:
        return False
    return not name.startswith(_NOT_A_NAME)


_SENTENCE_SPLIT = re.compile(r"(?<=[.?!…。？！])\s+")


def strip_phantom_sentences(text: str) -> str:
    """The text without sentences that are only a phantom phrase or a subtitle credit."""
    value = str(text or "").strip()
    if not value:
        return ""
    kept = [
        sentence
        for sentence in _SENTENCE_SPLIT.split(value)
        if sentence.strip() and not is_phantom_line(sentence) and not is_subtitle_credit(sentence)
    ]
    return " ".join(sentence.strip() for sentence in kept)
