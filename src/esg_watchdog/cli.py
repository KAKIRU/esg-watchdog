"""esg-watchdog CLI — 배치는 여기서만 돈다 (D-31). 웹 프로세스 안에서 실행하지 않는다.

이번 단계는 seed-companies · load-fixtures 만 구현한다 (D-26 · D-27 · D-33 · D-34).
collect · extract · detect · match · score · publish · run-all 은 등록만 하고 P4~P6 에서 채운다.
DB 엔진·boto3 등 무거운 import 는 서브커맨드 함수 안에서만 한다 (--help 는 .env 없이 동작).
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

PENDING_COMMANDS = ("collect", "extract", "detect", "match", "score", "publish", "run-all")

# D-38: 옛 시드(scripts/seed_companies.py)의 industry_key 표기. 보이면 knowledge 값으로 덮어쓴다
LEGACY_INDUSTRY_KEYS = {"telecommunications": "telecom", "food_manufacturing": "food"}

# fixture 적재 순서 (FK 방향). companies.json 은 직접 넣지 않고 id 매핑에만 쓴다
FIXTURE_ORDER = ("documents", "articles", "commitments", "events", "matches", "alerts")
# --truncate 대상. companies · filings · pipeline_runs 는 건드리지 않는다
TRUNCATE_TABLES = (
    "alerts",
    "matches",
    "events",
    "commitments",
    "document_pages",
    "documents",
    "articles",
    "article_companies",
)
# alerts.json 에 prompt_version 이 없다 (README 계약). NOT NULL 이므로 로더가 채운다
FIXTURE_PROMPT_VERSION = "fixture-v0"


# --------------------------------------------------------------------------- 공용
def _print_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    cells = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in cells:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _read_json(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"fixture 파일이 없다: {path}")
    with path.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise TypeError(f"{path}: 최상위는 리스트여야 한다")
    return rows


def _to_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _url_hash(url: str) -> str:
    # services/polling/naver.py 의 make_url_hash 와 같은 규칙
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- seed-companies
def cmd_seed_companies(args: argparse.Namespace) -> int:
    from sqlalchemy import select, update

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.knowledge.keywords import COMPANIES
    from esg_watchdog.models import Company

    report: list[tuple[object, ...]] = []
    deactivated: list[str] = []

    with SessionLocal() as session:
        for spec in COMPANIES:
            company = session.scalar(select(Company).where(Company.stock_code == spec["stock_code"]))
            if company is None:
                company = Company(stock_code=spec["stock_code"])
                session.add(company)
                action = "insert"
                previous_key = "-"
            else:
                action = "update"
                previous_key = company.industry_key

            # extra_keywords 는 DB 컬럼이 아니라 뉴스 쿼리 생성용 상수 — 적재하지 않는다
            company.name = spec["name"]
            company.corp_code = spec["corp_code"]
            company.aliases = list(spec["aliases"])
            company.exclude_terms = list(spec["exclude_terms"])
            # D-38: telecommunications → telecom · food_manufacturing → food 는 knowledge 값으로 덮어쓴다
            company.industry_key = spec["industry_key"]
            company.is_control_group = spec["is_control_group"]
            company.is_active = True

            industry = spec["industry_key"]
            if previous_key not in ("-", industry):
                industry = f"{previous_key} → {industry}"
            report.append((action, spec["stock_code"], spec["name"], industry, spec["is_control_group"], True))

        if args.deactivate_others:
            target_codes = [spec["stock_code"] for spec in COMPANIES]
            others = session.scalars(
                select(Company).where(Company.stock_code.not_in(target_codes), Company.is_active.is_(True))
            ).all()
            deactivated = [f"{other.name}({other.stock_code})" for other in others]
            if others:
                session.execute(
                    update(Company)
                    .where(Company.id.in_([other.id for other in others]))
                    .values(is_active=False)
                )

        session.commit()

    _print_table(("action", "stock_code", "name", "industry_key", "is_control_group", "is_active"), report)
    print(f"\n{len(report)}건 upsert (knowledge.COMPANIES 기준)")
    if args.deactivate_others:
        print(f"--deactivate-others: {len(deactivated)}건 is_active=False" + (f" — {', '.join(deactivated)}" if deactivated else ""))
    return EXIT_OK


# --------------------------------------------------------------------------- load-fixtures
def _company_id_map(session, companies: list[dict]) -> dict[int, int]:
    """fixture companies.json 의 id → 실제 companies.id. stock_code 로 찾는다."""
    from sqlalchemy import select

    from esg_watchdog.models import Company

    code_to_fixture_id = {row["stock_code"]: row["id"] for row in companies}
    found = dict(
        session.execute(
            select(Company.stock_code, Company.id).where(Company.stock_code.in_(list(code_to_fixture_id)))
        ).all()
    )
    missing = sorted(set(code_to_fixture_id) - set(found))
    if missing:
        raise LookupError(
            f"companies 에 없는 stock_code: {', '.join(missing)} — 먼저 `esg-watchdog seed-companies` 를 실행하라"
        )
    return {fixture_id: found[code] for code, fixture_id in code_to_fixture_id.items()}


def _document_row(row: dict, company_map: dict[int, int]) -> dict:
    result = dict(row)
    result["company_id"] = company_map[row["company_id"]]
    result["published_at"] = _to_date(row.get("published_at"))
    # fixture 에는 storage_path 가 없다 (S3 객체 없음)
    result.setdefault("storage_path", f"fixture://{row['id']}")
    return result


def _article_row(row: dict) -> dict:
    result = dict(row)
    result["url_hash"] = _url_hash(row["url"])
    result["published_at"] = _to_datetime(row["published_at"])
    return result


def _commitment_row(row: dict, company_map: dict[int, int]) -> dict:
    result = dict(row)
    result["company_id"] = company_map[row["company_id"]]
    result["filed_at"] = _to_date(row["filed_at"])
    return result


def _event_row(row: dict, company_map: dict[int, int]) -> dict:
    result = dict(row)
    result["company_id"] = company_map[row["company_id"]]
    result["event_date"] = _to_date(row["event_date"])
    result["reported_at"] = _to_date(row.get("reported_at"))
    return result


def _match_row(row: dict) -> dict:
    return dict(row)


def _alert_row(row: dict, company_map: dict[int, int]) -> dict:
    result = dict(row)
    result["company_id"] = company_map[row["company_id"]]
    result["published_at"] = _to_datetime(row["published_at"])
    result.setdefault("prompt_version", FIXTURE_PROMPT_VERSION)
    return result


def _sync_identity(session, table: str) -> None:
    """Identity 시퀀스를 max(id) 다음으로 맞춘다. 비어 있으면 1 부터."""
    from sqlalchemy import text

    # table 은 FIXTURE_ORDER 상수에서만 온다
    session.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT max(id) FROM {table}), 1), "
            f"(SELECT max(id) FROM {table}) IS NOT NULL)"
        )
    )


def cmd_load_fixtures(args: argparse.Namespace) -> int:
    from sqlalchemy import insert, select, text

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Alert, Article, Commitment, Document, Event, Match

    fixture_dir = Path(args.dir)
    models = {
        "documents": Document,
        "articles": Article,
        "commitments": Commitment,
        "events": Event,
        "matches": Match,
        "alerts": Alert,
    }

    try:
        raw = {name: _read_json(fixture_dir / f"{name}.json") for name in ("companies", *FIXTURE_ORDER)}
    except (FileNotFoundError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    mode = "truncate" if args.truncate else ("merge" if args.merge else "skip-existing")
    report: list[tuple[object, ...]] = []

    with SessionLocal() as session:
        try:
            company_map = _company_id_map(session, raw["companies"])
        except LookupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_ERROR

        rows_by_table = {
            "documents": [_document_row(row, company_map) for row in raw["documents"]],
            "articles": [_article_row(row) for row in raw["articles"]],
            "commitments": [_commitment_row(row, company_map) for row in raw["commitments"]],
            "events": [_event_row(row, company_map) for row in raw["events"]],
            "matches": [_match_row(row) for row in raw["matches"]],
            "alerts": [_alert_row(row, company_map) for row in raw["alerts"]],
        }

        if args.truncate:
            session.execute(text(f"TRUNCATE TABLE {', '.join(TRUNCATE_TABLES)} RESTART IDENTITY CASCADE"))

        for name in FIXTURE_ORDER:
            model = models[name]
            rows = rows_by_table[name]
            existing_ids = set(session.scalars(select(model.id).where(model.id.in_([row["id"] for row in rows]))))
            existing_hashes: set[str] = set()
            if name == "articles":
                # url_hash UNIQUE — 파이프라인이 같은 기사를 먼저 수집했으면 그 행도 건너뛴다
                existing_hashes = set(
                    session.scalars(select(Article.url_hash).where(Article.url_hash.in_([row["url_hash"] for row in rows])))
                )

            inserted = 0
            skipped = 0
            for row in rows:
                if row["id"] in existing_ids or row.get("url_hash") in existing_hashes:
                    skipped += 1
                    continue
                session.execute(insert(model).values(**row))
                inserted += 1

            _sync_identity(session, name)
            report.append((name, len(rows), inserted, skipped))

        session.commit()

    print(f"load-fixtures ({mode}) — {fixture_dir}")
    _print_table(("table", "fixture", "inserted", "skipped"), report)
    total_skipped = sum(int(row[3]) for row in report)
    if total_skipped:
        print(f"\n건너뜀 {total_skipped}건 (같은 id 또는 같은 url_hash 가 이미 있음)")
    return EXIT_OK


# --------------------------------------------------------------------------- 미구현 단계
def cmd_not_implemented(args: argparse.Namespace) -> int:
    print(f"{args.command}: P4~P6에서 구현", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="esg-watchdog", description="ESG 공시·실제 사건 교차검증 배치 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed-companies", help="knowledge.COMPANIES 3사를 stock_code 기준 upsert")
    seed.add_argument("--deactivate-others", action="store_true", help="3사 외 companies 를 is_active=False 로")
    seed.set_defaults(func=cmd_seed_companies)

    load = subparsers.add_parser("load-fixtures", help="app/fixtures/*.json 을 계약 테이블에 적재 (보험 · 씨앗 병행 적재)")
    load.add_argument("--dir", default="app/fixtures", help="fixture 디렉터리 (기본 app/fixtures)")
    mode = load.add_mutually_exclusive_group()
    mode.add_argument("--truncate", action="store_true", help="계약 테이블·documents·articles 를 비우고 적재")
    mode.add_argument("--merge", action="store_true", help="비우지 않고 적재, 같은 id 는 건너뛰고 수를 보고")
    load.set_defaults(func=cmd_load_fixtures)

    for name in PENDING_COMMANDS:
        pending = subparsers.add_parser(name, help="P4~P6에서 구현")
        pending.set_defaults(func=cmd_not_implemented)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
