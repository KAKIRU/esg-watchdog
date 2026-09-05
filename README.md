# ESGWatchdog — 공시와 현실 사이 · ESG 조기경보

지속가능경영보고서의 공약(commitments)과 그 뒤에 보도된 사건(events)을 같은 기업·같은 분류 안에서 교차검증해
관계(위반 · 후퇴 · 이행지연 · 이행긍정 · 무관)를 판정하고, 점수와 등급(주의 · 경고 · 심각)을 붙여 경보(alerts)로 발행한다.
대상은 3사(KT · SPC삼립 · 오뚜기[대조군]) · 2026 금융 AI Challenge 제출물.

> 이 서비스는 투자 판단을 대신하지 않으며 공개된 공시와 보도만을 근거로 합니다. 원문 링크에서 직접 확인하시기 바랍니다.

---

## 실행 경로 3개

### ① 제출 웹서비스 — Streamlit (`app/`)

```powershell
uv sync --locked --group ui
uv run --env-file .env streamlit run app/streamlit_app.py
```

- `app/` 은 pydantic-settings 를 쓰지 않아 `.env` 를 자동으로 읽지 않는다. 그래서 `--env-file .env` 가 필요하다.
- `DATABASE_URL` 이 **없으면 fixture 모드**(`app/fixtures/*.json`), **있으면 DB 모드**. DB 모드는 읽기 전용 계정 `app_ro`
  (`scripts/db/create_app_ro.sql`)로 접속하고, 스킴은 반드시 `postgresql+psycopg://` 다. 상단 배지가 "DB 연결 · 경보 N건" 이면 정상.
- 화면은 테이블만 읽는다. 요청 시점 LLM 호출·배치 실행은 없다.
- 배포(Render · Docker · 롤백 · Auto-Deploy Off · 외부 핑)는 [docs/deploy.md](docs/deploy.md).

### ② 파이프라인 — A 노트북에서 수동 실행, 클라우드 DB 에 적재 (`esg-watchdog` CLI)

```powershell
uv sync --locked --dev
Copy-Item .env.example .env        # POSTGRES_* · DART_API_KEY · NAVER_* · LLM_* 채우기 (아래 "외부 데이터 출처")
uv run alembic upgrade head
uv run esg-watchdog seed-companies
uv run esg-watchdog collect --stage news --company 030200
uv run esg-watchdog collect --stage filings --company 030200
uv run esg-watchdog collect --stage reports --company 030200 --pdf data/reports/030200_SR_2025.pdf --pages 17,31,33,35 --title "2025 KT ESG보고서" --fiscal-year 2024 --published-at 2025-06-30
uv run esg-watchdog extract --company 030200
uv run esg-watchdog detect  --company 030200 --limit 300
uv run esg-watchdog match   --company 030200
uv run esg-watchdog score   --company 030200
uv run esg-watchdog publish --company 030200
```

| 명령 | 하는 일 | 주요 옵션 |
|---|---|---|
| `seed-companies` | knowledge.COMPANIES 3사를 stock_code 기준 upsert | `--deactivate-others` |
| `load-fixtures` | `app/fixtures/*.json` 을 계약 테이블에 적재 (보험 · 씨앗) | `--dir` · `--truncate` · `--merge` |
| `collect --stage news` | 네이버 뉴스 검색 → `articles` (별칭×키워드 44 · 제외어 · 12개월 · URL 정규화) | `--company` · `--max-pages` |
| `collect --stage filings` | OpenDART 후속공시 목록 → `filings` (원문 미수집) | `--company` |
| `collect --stage reports` | 로컬 PDF 의 지정 페이지만 → `documents` · `document_pages` | `--company --pdf --pages --title --fiscal-year --published-at` (전부 필수) · `--source-url` |
| `extract` | `document_pages` → `commitments` (LLM · 인용 검사) | `--company` 또는 `--document ID` |
| `detect` | pending `articles` → `events` (사전 필터 → 15건 배치 LLM → 인용 검사 → 중복 병합) | `--limit N` · `--since YYYY-MM-DD` · `--until YYYY-MM-DD` · `--all` · `--yes` |
| `match` | 후보 SQL(같은 기업·같은 category·0<gap≤24 → `sub_tags` 교집합 → 공약당 상한) → LLM 관계 판정 → `matches` | `--company` · `--per-commitment N` (기본 5) · `--limit N` · `--dry-run` (LLM 없이 3단계 후보 수만 표로, `--company` 생략 시 전체) |
| `score` | `matches.scores` 전체 재계산 (LLM 없음) | `--company` (없으면 전체) |
| `publish` | accepted 매칭 → 등급 · 4단락 설명문 · 금지어 필터 → `alerts` | `--company` (없으면 전체) |
| `run-all` | collect news · filings → extract → detect → match → score → publish. 한 단계 실패 시 중단 | `--company` · `--detect-limit N` (기본 300) |

- `detect` 는 실행 첫 줄에 `pending N건 → 사전 필터 통과 M건 → 배치 K회 · 처리 구간: YYYY-MM-DD ~ YYYY-MM-DD` 를 출력한다.
  `--limit` 없이 M 이 200건을 넘으면 `--yes` 없이는 실행하지 않는다. `--since/--until` 은 `articles.published_at`(KST 날짜, until 포함)
  기준이고 기본은 최근 12개월 전체다. 창의 앞쪽만 처리하고 뒤가 비지 않도록 구간을 나눠 돌린다.
- `match` 는 실행 첫 줄에 `후보 N건(카테고리 일치 A → sub_tags 교집합 B → 상한 절단 C) → LLM 호출 N회` 를 출력한다. 후보는
  ① 카테고리 일치 → ② `commitments.sub_tags` 와 `events.sub_tags` 가 한 개 이상 겹침(한쪽이 빈 배열이면 통과, SQL `&&`) → ③ 공약당
  상한(`--per-commitment`, 기본 5: confirmed=true → source_count 내림차순 → 기준일 최신순으로 남김) · `--limit` 전체 상한 순으로 좁힌다.
  실측(9/2)에서 카테고리 일치만으로 3사 2,563건이 나와 도입했다. 잘린 후보는 `pipeline_runs.stage_stats.truncated/limited` 에 남고
  다음 실행에서 다시 후보가 된다(판정된 쌍만 `matches` 로 제외). `--dry-run` 으로 회사·카테고리별 3단계 수와 살아남은 쌍을 먼저 본다.
- **재실행 안전(멱등)**: 모든 단계는 다시 실행해도 같은 행을 두 번 만들지 않는다 — articles 는 url_hash, commitments 는
  (company_id, normalized_text), events 는 병합 키(category · event_type · 연-월), matches 는 (commitment_id, event_id), alerts 는 match_id.
  score 는 부분 재계산 없이 전량 다시 계산한다.
- **9/3 보험**: 파이프라인이 완주하지 못하면 씨앗 데이터를 계약 테이블에 직접 넣는다 — `uv run esg-watchdog load-fixtures --dir app/fixtures --truncate`.
- **`load-fixtures --merge` 는 반드시 파이프라인보다 먼저 돌린다.** fixture 의 documents(1~3) · articles(101~108) 는 작은 id 대역이라,
  파이프라인이 먼저 같은 id 를 만들면 merge 가 그 행을 skip 하고 `commitments.source.doc_id` 가 엉뚱한 문서를 가리킨다.
- **수집이 이미 끝난 뒤에는 `run-all` 을 쓰지 마라.** collect 부터 다시 돌아 1~2시간이 걸린다. 단계별로 실행한다.
- **LLM 캐시**(`LLM_CACHE_DIR`, 기본 `.llm_cache/`)는 스키마만 통과하면 금지어가 든 응답도 저장한다. 폐기된 경보를 다시 시도하려면
  해당 캐시 파일을 지우거나 `prompts/*.py` 의 `PROMPT_VERSION` 을 올려야 한다.
- `LLM_PROVIDER=fake` 는 `{LLM_CACHE_DIR}/fake_responses.json` 의 응답을 순서대로 돌려준다(오프라인 배관 검증용).
- 모든 실행은 `pipeline_runs`(trigger=manual) 에 남는다. cron 은 없다.

### ③ (옵션) FastAPI 헬스 엔드포인트

```powershell
uv run uvicorn esg_watchdog.main:app
```

`GET /health` → `{"status": "UP"}`. 미배포 · 라우트 추가 금지 (`src/esg_watchdog/main.py` 한 파일뿐이다).

---

## 외부 데이터 출처

| 출처 | 쓰는 곳 | 비고 |
|---|---|---|
| OpenDART 공시검색 API | `collect --stage filings` | `DART_API_KEY`. 후속공시 목록(pblntf_ty=I · 12개월)만, 원문 미수집 |
| 네이버 뉴스 검색 API (API HUB) | `collect --stage news` | `NAVER_CLIENT_ID` · `NAVER_CLIENT_SECRET`. 쿼리당 start≤1000 상한 |
| 각사 지속가능경영보고서 PDF | `collect --stage reports` | 회사 홈페이지에서 **수동 수집**해 로컬(`data/reports/`)에 둔다. 지정 페이지만 적재 |

시크릿은 `.env` 에서만 읽는다. `.env` · 비밀번호 · API 키는 파일·로그·커밋에 쓰지 않는다. `.env.example` 에는 플레이스홀더만.
`.env` 는 값 뒤 인라인 주석 금지(python-dotenv 가 주석을 값으로 읽는다) — 주석은 변수 윗줄에.

## 코드 경계 (D-31)

1. `src/` 에 streamlit 을 import 하지 않는다.
2. `app/` 은 `esg_watchdog` 를 import 하지 않는다 — 읽기 전용 `DATABASE_URL` 과 SQL(`SELECT *`)만 쓴다.
3. fastapi 는 `src/esg_watchdog/main.py` 한 파일에만 있고, 라우트를 추가하지 않는다.
4. 배치는 CLI(`esg-watchdog`)로만 돈다. 웹 프로세스 안에서 실행하지 않는다.
5. 만들지 않는 것(D-28): 임베딩·pgvector 컬럼·유사도 검색, llm_cache 테이블, cron, positive_signals · grade_history · feedback · chunks 테이블.

## 심사 기간 규칙

- **9/6 밤 데이터 동결** · Render **Auto-Deploy Off**(절차는 docs/deploy.md).
- **9/7 ~ 9/11 프로덕션 DB 에 배치 쓰기 금지**(collect · extract · detect · match · score · publish · load-fixtures 전부).
- 배포는 가용성 복구(헬스 실패 · 롤백)만 한다. 화면·데이터를 바꾸는 배포는 하지 않는다.

## 면책 고지

이 서비스는 투자 판단을 대신하지 않으며 공개된 공시와 보도만을 근거로 한다. 사법 판단·매매 판단을 하지 않는다.

---

## 개발환경 시작하기

처음 프로젝트를 clone 받은 경우 아래 순서대로 진행합니다.

### 1. 필수 프로그램 확인

다음 프로그램이 설치되어 있어야 합니다.

- Git
- Docker Desktop
- uv

PowerShell에서 확인:

```powershell
git --version
docker --version
docker compose version
uv --version
```

> PostgreSQL과 pgvector는 직접 설치할 필요 없습니다.  
> Docker Compose를 통해 실행합니다.

---

### 2. Repository Clone

```powershell
git clone https://github.com/KAKIRU/esg-watchdog.git
cd esg-watchdog
```

현재 브랜치 확인:

```powershell
git status
```

`main` 브랜치인지 확인합니다.

---

### 3. Python 환경 및 Dependency 설치

```powershell
uv sync --locked --dev --group ui
```

Python 버전 관련 오류가 발생하면:

```powershell
uv python install
uv sync --locked --dev --group ui
```

> 필요한 라이브러리는 `pyproject.toml`과 `uv.lock`으로 관리합니다.  
> 개별적으로 `pip install`을 사용하지 않습니다.  
> `--group ui` 는 화면(streamlit · pandas)과 `tests/app` 에 필요합니다. 파이프라인만 돌린다면 `--dev` 만으로 충분합니다.

---

### 4. 환경변수 설정

`.env.example`을 복사하여 `.env` 파일을 생성합니다.

PowerShell:

```powershell
Copy-Item .env.example .env
```

생성한 `.env`에서 로컬 DB 비밀번호를 설정합니다.

```dotenv
POSTGRES_DB=esgwatchdog
POSTGRES_USER=esgwatchdog
POSTGRES_PASSWORD=your_local_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
```

> `.env` 파일은 Git에 commit하지 않습니다. 값 뒤에 인라인 주석을 두지 않습니다.

---

### 5. PostgreSQL 실행

Docker Desktop을 실행한 상태에서:

```powershell
docker compose up -d
```

컨테이너 확인:

```powershell
docker ps
```

PostgreSQL 준비 상태 확인:

```powershell
docker compose exec postgres pg_isready -U esgwatchdog -d esgwatchdog
```

정상적으로 실행된 경우:

```text
/var/run/postgresql:5432 - accepting connections
```

이 출력됩니다.

---

### 6. DB Migration 적용

```powershell
uv run alembic upgrade head
```

현재 Migration 상태 확인:

```powershell
uv run alembic current
```

최신 상태라면 revision 옆에:

```text
(head)
```

가 표시됩니다.

---

### 7. 코드 검사

Ruff:

```powershell
uv run ruff check .
```

정상:

```text
All checks passed!
```

pytest:

```powershell
uv run pytest
```

현재 정상적으로 설정된 경우 테스트가 통과합니다.

> 현재 TestClient dependency 관련 warning이 출력될 수 있으나 테스트 실패는 아닙니다.

---

### 8. (옵션) FastAPI 실행

헬스 체크 어댑터일 뿐이며 제출·배포 대상이 아닙니다. 건너뛰어도 됩니다.

```powershell
uv run uvicorn esg_watchdog.main:app --reload
```

Health Check:

```text
http://127.0.0.1:8000/health
```

정상 응답:

```json
{
  "status": "UP"
}
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

---

## 개발환경 구성 완료 체크

아래 항목이 모두 정상이라면 개발 준비가 완료된 것입니다.

```text
uv sync                     ✅
Docker PostgreSQL           ✅
PostgreSQL accepting        ✅
Alembic migration (head)    ✅
Ruff                        ✅
pytest                      ✅
(옵션) FastAPI /health      ✅
```

## 개발 종료 후 DB 정지

PostgreSQL 컨테이너를 정지하려면:

```powershell
docker compose stop
```

다시 실행하려면:

```powershell
docker compose start
```

Docker Volume은 별도로 삭제하지 않는 한 DB 데이터가 유지됩니다.
