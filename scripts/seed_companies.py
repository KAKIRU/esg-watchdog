from esg_watchdog.db import SessionLocal
from esg_watchdog.models import Company

COMPANIES = [
    Company(
        name="POSCO홀딩스",
        stock_code="005490",
        corp_code="00155319",
        aliases=["POSCO홀딩스", "포스코홀딩스", "포스코"],
        industry_key="steel",
    ),
    Company(
        name="GS건설",
        stock_code="006360",
        corp_code="00120030",
        aliases=["GS건설"],
        industry_key="construction",
    ),
    Company(
        name="HDC현대산업개발",
        stock_code="294870",
        corp_code="01310269",
        aliases=["HDC현대산업개발", "IPARK현대산업개발", "현대산업개발", "HDC현산"],
        industry_key="construction",
    ),
    Company(
        name="SPC삼립",
        stock_code="005610",
        corp_code="00125530",
        aliases=["SPC삼립", "삼립"],
        industry_key="food_manufacturing",
    ),
    Company(
        name="한국타이어앤테크놀로지",
        stock_code="161390",
        corp_code="00937324",
        aliases=["한국타이어앤테크놀로지", "한국타이어"],
        industry_key="tire_manufacturing",
    ),
    Company(
        name="NAVER",
        stock_code="035420",
        corp_code="00266961",
        aliases=["NAVER", "네이버"],
        industry_key="internet_platform",
    ),
    Company(
        name="KT",
        stock_code="030200",
        corp_code="00190321",
        aliases=["KT", "케이티"],
        industry_key="telecommunications",
    ),
    Company(
        name="DL이앤씨",
        stock_code="375500",
        corp_code="01524093",
        aliases=["DL이앤씨"],
        industry_key="construction",
    ),
]

def seed_companies() -> None:
    with SessionLocal() as session:
        for company in COMPANIES:
            existing = (
                session.query(Company)
                .filter(Company.stock_code == company.stock_code)
                .first()
            )

            if existing:
                print(f"SKIP: {company.name}")
                continue

            session.add(company)
            print(f"ADD: {company.name}")

        session.commit()


if __name__ == "__main__":
    seed_companies()