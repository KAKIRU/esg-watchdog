"""금지 표현 목록 v1(D-17) 로더. 스캔·치환 로직은 여기 두지 않는다(F-06)."""

from functools import cache
from importlib import resources

import yaml


@cache
def load_banned_terms() -> dict:
    """banned_terms.yaml 을 패키지 리소스에서 읽어 dict 로 돌려준다 (최초 1회만 파싱)."""
    text = (
        resources.files("esg_watchdog.knowledge")
        .joinpath("banned_terms.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)
