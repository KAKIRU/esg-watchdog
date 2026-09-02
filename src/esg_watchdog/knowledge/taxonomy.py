"""택소노미 v1 — 상수 튜플과 같은 값의 Literal 타입 (D-05 · D-08 · D-12 · D-36)."""

from typing import Literal

CATEGORIES = ("E", "S", "G")
Category = Literal["E", "S", "G"]

SUB_TAGS = (
    "온실가스·에너지",
    "폐기물·자원순환",
    "용수·오염",
    "산업안전보건",
    "정보보호·프라이버시",
    "제품안전·품질",
    "공급망·협력사",
    "인권·노동",
    "지역사회",
    "준법·윤리",
    "이사회·지배구조",
    "공시·투명성",
    "기타",
)
SubTag = Literal[
    "온실가스·에너지",
    "폐기물·자원순환",
    "용수·오염",
    "산업안전보건",
    "정보보호·프라이버시",
    "제품안전·품질",
    "공급망·협력사",
    "인권·노동",
    "지역사회",
    "준법·윤리",
    "이사회·지배구조",
    "공시·투명성",
    "기타",
]

# 택소노미 v1 기본값 — B 검토
EVENT_TYPES = (
    "산업재해",
    "정보유출",
    "제재",
    "규제위반",
    "소송·수사",
    "환경오염",
    "제품안전·품질",
    "지배구조·준법",
    "공급망·협력사",
    "재무영향",
    "이행조치",
    "기타",
)
EventType = Literal[
    "산업재해",
    "정보유출",
    "제재",
    "규제위반",
    "소송·수사",
    "환경오염",
    "제품안전·품질",
    "지배구조·준법",
    "공급망·협력사",
    "재무영향",
    "이행조치",
    "기타",
]

RELATIONS = ("위반", "후퇴", "이행지연", "이행긍정", "무관")
Relation = Literal["위반", "후퇴", "이행지연", "이행긍정", "무관"]
# 경보를 만드는 관계. 이행긍정은 배지(D-12), 무관은 저장만
ALERT_RELATIONS = ("위반", "후퇴", "이행지연")
AlertRelation = Literal["위반", "후퇴", "이행지연"]

GRADES = ("주의", "경고", "심각")
Grade = Literal["주의", "경고", "심각"]

COMMITMENT_TYPES = ("정량", "정성")
CommitmentType = Literal["정량", "정성"]

COMMITMENT_STATUS = ("active", "quarantined")
CommitmentStatus = Literal["active", "quarantined"]

MATCH_STATUS = ("pending", "accepted", "rejected")
MatchStatus = Literal["pending", "accepted", "rejected"]

ALERT_STATUS = ("published", "withdrawn")
AlertStatus = Literal["published", "withdrawn"]

DATE_PRECISIONS = ("day", "month")
DatePrecision = Literal["day", "month"]

# D-08
CONFIRMED_BASES = ("규제기관 처분", "판결", "회사 공시", "회사 인정", "없음")
ConfirmedBasis = Literal["규제기관 처분", "판결", "회사 공시", "회사 인정", "없음"]
