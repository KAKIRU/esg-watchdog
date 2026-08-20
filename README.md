# ESGWatchdog

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
uv sync --locked --dev
```

Python 버전 관련 오류가 발생하면:

```powershell
uv python install
uv sync --locked --dev
```

> 필요한 라이브러리는 `pyproject.toml`과 `uv.lock`으로 관리합니다.  
> 개별적으로 `pip install`을 사용하지 않습니다.

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

> `.env` 파일은 Git에 commit하지 않습니다.

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

### 8. FastAPI 실행

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
FastAPI /health             ✅
Swagger /docs               ✅
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
