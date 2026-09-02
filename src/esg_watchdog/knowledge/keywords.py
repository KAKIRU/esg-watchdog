"""ESG 키워드 44개(D-01)와 대상 3사(D-26). 원본은 이 파일, Notion 'ESG 키워드 사전 v1'은 사본."""

ESG_KEYWORDS = {
    "공통": ["제재", "과징금", "시정명령", "고발", "압수수색", "소송", "논란", "사과", "리콜", "과태료"],
    "안전보건": ["중대재해", "산업재해", "사망사고", "끼임", "화재", "폭발", "작업중지", "고용노동부", "산업안전보건법", "안전점검"],
    "정보보호": ["개인정보", "유출", "해킹", "침해사고", "개인정보보호위원회", "보안사고", "랜섬웨어", "정보보호"],
    "준법지배구조": ["공정거래위원회", "담합", "배임", "횡령", "내부통제", "갑질", "은폐", "조사방해"],
    "환경식품": ["온실가스", "탄소배출", "폐수", "환경부", "이물질", "식약처", "회수", "위생"],
}
# 44개 (10+10+8+8+8). 결정 로그 D-01·키워드 사전의 "40개"는 오기 — 목록은 확정본 그대로
ALL_KEYWORDS = [k for g in ESG_KEYWORDS.values() for k in g]

# 본선 확대 후보(옛 시드 9곳 중 3사 외): POSCO홀딩스 005490 · GS건설 006360 · HDC현대산업개발 294870 · 한국타이어앤테크놀로지 161390 · NAVER 035420 · DL이앤씨 375500
COMPANIES = [
    {
        "stock_code": "030200",
        "corp_code": "00190321",
        "name": "KT",
        "industry_key": "telecom",
        "is_control_group": False,
        "aliases": ["KT", "케이티"],
        "exclude_terms": ["KT&G", "kt wiz", "KT 위즈", "KTX", "KT알파", "KT 롤스터", "KT 스카이라이프"],
        # 회사별 추가 키워드 — 뉴스 창 초반(2025.9~12) 사건이 1,000건 상한에 밀리지 않게. 값은 B 확정
        "extra_keywords": ["펨토셀", "소액결제", "서버 감염", "조사 방해"],
    },
    {
        "stock_code": "005610",
        "corp_code": "00125530",
        "name": "SPC삼립",
        "industry_key": "food",
        "is_control_group": False,
        "aliases": ["SPC삼립", "삼립"],
        "exclude_terms": [],
        "extra_keywords": ["시화공장", "시흥공장", "구속영장"],
    },
    {
        "stock_code": "007310",
        "corp_code": "00141529",
        "name": "오뚜기",
        "industry_key": "food",
        "is_control_group": True,
        "aliases": ["오뚜기", "OTOKI"],
        "exclude_terms": [],
        "extra_keywords": [],
    },
]
