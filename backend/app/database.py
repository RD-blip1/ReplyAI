from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url.get_secret_value(),
    connect_args={"timeout": 2},
    pool_pre_ping=True,
)