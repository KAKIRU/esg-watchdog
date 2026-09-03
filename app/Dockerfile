# 화면(app/) 전용 이미지. 빌드 컨텍스트는 레포 루트: docker build -f app/Dockerfile -t esg-app .
# --only-group ui 는 의도다: 프로젝트(src/)와 파이프라인 의존성(boto3 · pypdf · fastapi …)이 이미지에 들어가지 않아
# D-31 경계를 이미지 레벨에서 강제한다. (uv 0.8: --only-group 은 프로젝트 자체도 설치하지 않는다 — 확인 완료)
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 STREAMLIT_SERVER_HEADLESS=true STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --only-group ui
COPY app/ ./app/
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8501
CMD ["sh","-c","streamlit run app/streamlit_app.py --server.port ${PORT:-8501} --server.address 0.0.0.0"]
