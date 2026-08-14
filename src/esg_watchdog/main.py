from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class HealthResponse(BaseModel):
    status: str

@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="UP")