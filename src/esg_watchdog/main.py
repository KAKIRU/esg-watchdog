"""
Optional FastAPI adapter for ESGWatchdog.

The primary execution path is the batch pipeline run by the `esg-watchdog` CLI (src/esg_watchdog/cli.py).
Keep this module only as an optional HTTP adapter and health-check endpoint.

Do not add business or pipeline routes here.
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class HealthResponse(BaseModel):
    status: str


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="UP")