# 배포 — Render · Docker (D-32 · D-33)

화면(`app/`)만 배포한다. 이미지는 `uv sync --locked --only-group ui` 로 ui 그룹만 설치하므로 파이프라인 패키지(`src/`)와
그 의존성(boto3 · pypdf · fastapi …)이 들어가지 않는다 — D-31 경계를 이미지 레벨에서 강제한다. 배치(`esg-watchdog` CLI)는
배포 이미지에서 돌지 않고, 화면은 읽기 전용 계정 `app_ro` 로 테이블만 읽는다 (D-35, `scripts/db/create_app_ro.sql`).

## Render 설정 (Web Service)

| 항목 | 값 |
|---|---|
| Region | Singapore (DB 와 같은 리전) |
| Runtime | Docker |
| Dockerfile Path | `app/Dockerfile` |
| Docker Build Context Directory | `.` (레포 루트) |
| Branch | `main` |
| Health Check Path | `/_stcore/health` |
| Instance Type | Starter |
| Auto-Deploy | On → **9/6 밤 Off** (아래 항목) |

### 환경변수 (Environment)

| 키 | 값 |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://app_ro:<pw>@<internal-host>:5432/esgwatchdog` — Render Postgres 의 **Internal** 호스트 · 읽기 전용 계정 |
| `PYTHONUNBUFFERED` | `1` |
| `PORT` | 넣지 않는다. Render 가 주입하고 Dockerfile CMD 가 `${PORT:-8501}` 로 읽는다 |

- Render 가 보여주는 Internal Database URL 은 `postgres://` 로 시작한다. **`postgresql+psycopg://`** 로 바꿔 넣는다
  (`app/lib/data.py` 가 `postgres://` 도 받아주지만 문서 기준은 `+psycopg`). 내부 호스트는 `sslmode` 를 붙이지 않는다.
- 비밀번호는 Render 환경변수에만 둔다. 레포 · 문서 · 로그에 쓰지 않는다.
- DB 가 느리게 응답할 때 화면이 오래 매달리면 URL 뒤에 `?connect_timeout=5` 를 붙인다 (libpq 옵션, 코드 변경 없음).

## 로컬 테스트

- fixture 모드: `uv run streamlit run app/streamlit_app.py` (DATABASE_URL 없음) → 상단 배지 "fixture 모드".
- 실DB 모드: `.env` 의 `DATABASE_URL` 에 **External** 호스트 + `?sslmode=require` 를 두고
  `uv run --env-file .env streamlit run app/streamlit_app.py` → 상단 배지 "DB 연결 · 경보 N건".
  (`.env` 는 값 뒤 인라인 주석 금지. 주석은 변수 윗줄에.)
- 이미지: `docker build -f app/Dockerfile -t esg-app .` → `docker run --rm -e PORT=8501 -p 8501:8501 esg-app`
  → `curl -s localhost:8501/_stcore/health` 가 `ok`. `docker run --rm esg-app python -c "import esg_watchdog"` 는 **실패해야** 정상.

## 롤백

Render 대시보드 → 서비스 → **Manual Deploy** → **Rollback** 에서 이전 성공 배포를 고른다 (이미지 재사용, 빌드 없음).
특정 커밋으로 가려면 Manual Deploy → "Deploy a specific commit".

## 9/6 밤 Auto-Deploy Off (D-32)

Settings → Build & Deploy → **Auto-Deploy: Off** → Save. 이후 `main` 푸시는 배포되지 않고, 배포는 Manual Deploy →
"Deploy latest commit" 으로만 한다 (심사 기간에 화면이 바뀌지 않게). 되돌릴 때는 같은 자리에서 On.

## 외부 5분 핑 (UptimeRobot)

| 항목 | 값 |
|---|---|
| Monitor Type | HTTP(s) — Keyword |
| URL | `https://<service>.onrender.com/_stcore/health` |
| Keyword | `ok` (exists → up) |
| Interval | 5분 |
| Alert Contacts | 3인 (이메일) |

## 배포 후 확인 절차

1. `curl -s https://<service>.onrender.com/_stcore/health` → `ok`
2. 브라우저에서 상단 배지가 **"DB 연결 · 경보 N건"** 인지. N 은 `SELECT count(*) FROM alerts WHERE status = 'published'` 와 같아야 한다.
   "fixture 모드" 가 보이면 `DATABASE_URL` 이 안 들어간 것, "DB 오류 · …" 한 줄이 보이면 URL 스킴 · 계정 · 리전(내부 호스트) 확인.
3. 동선: 피드 → 카드 "자세히" → 경보 상세(근거 카드 2매 · 점수 · 면책 고지) → "기업 상세" → 공약 이행 현황판 → "← 피드로".
   `?codes=030200,005610` 을 붙이면 "보유 종목 필터 적용중: KT, SPC삼립".
4. Render Logs 에 `Traceback` 이 없는지. 화면 오류는 한 줄만 보이고 스택트레이스는 로그에 남는다.
