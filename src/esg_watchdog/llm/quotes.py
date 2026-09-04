"""인용 검사 — evidence_quote · commitment_text 가 원문에 부분 문자열로 있는지 (D-04 · D-05).

- normalize(s): 공백 연속 → 1개 · 따옴표(“ ” ‘ ’ 「」 『』 등) → " ' · 중점(ㆍ • ・ ‧) → · · 폭 없는 문자 제거 · strip.
- quote_in(quote, source): 정규화한 quote 가 정규화한 source 의 부분 문자열인가. 빈 quote 는 False.
- find_span(quote, source): 원문 기준 (start, end) 오프셋. source[start:end] 가 인용 구간(원문 표기 그대로). 없으면 None.
"""

_QUOTE_MAP = {
    "“": '"',
    "”": '"',
    "„": '"',
    "″": '"',
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "′": "'",
    "ㆍ": "·",
    "•": "·",
    "・": "·",
    "‧": "·",
    "∙": "·",
}
# 보이지 않는 문자 — PDF 추출 텍스트에 섞이지만 인용문에는 없다
_DROP = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad", "\u2060"}


def _normalize_with_map(source: str) -> tuple[str, list[int]]:
    """정규화 문자열과, 정규화 문자열의 각 글자가 원문에서 시작하는 인덱스 목록."""
    chars: list[str] = []
    index_map: list[int] = []
    pending_space = False
    for position, char in enumerate(source):
        if char in _DROP:
            continue
        if char.isspace():
            if chars and not pending_space:
                pending_space = True
                chars.append(" ")
                index_map.append(position)
            continue
        pending_space = False
        chars.append(_QUOTE_MAP.get(char, char))
        index_map.append(position)
    if chars and chars[-1] == " ":
        chars.pop()
        index_map.pop()
    return "".join(chars), index_map


def normalize(text: str) -> str:
    return _normalize_with_map(text)[0]


def quote_in(quote: str, source: str) -> bool:
    needle = normalize(quote)
    return bool(needle) and needle in normalize(source)


def find_span(quote: str, source: str) -> tuple[int, int] | None:
    needle = normalize(quote)
    if not needle:
        return None
    haystack, index_map = _normalize_with_map(source)
    start = haystack.find(needle)
    if start < 0:
        return None
    end = start + len(needle) - 1
    return index_map[start], index_map[end] + 1
