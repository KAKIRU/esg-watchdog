from sqlalchemy import URL, create_engine
from sqlalchemy.orm import sessionmaker

from esg_watchdog.config import settings

database_url = URL.create(
    drivername="postgresql+psycopg",
    username=settings.postgres_user,
    password=settings.postgres_password,
    host=settings.postgres_host,
    port=settings.postgres_port,
    database=settings.postgres_db,
    query={"sslmode": settings.postgres_sslmode},
)

engine = create_engine(database_url)

SessionLocal = sessionmaker(bind=engine)