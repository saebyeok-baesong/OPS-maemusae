"""Alembic 실행 환경. 접속 주소는 app.db.migrate가 attributes로 넘긴다."""
from alembic import context
from sqlalchemy import create_engine

from app.db import sqlalchemy_url

with create_engine(sqlalchemy_url(context.config.attributes["database_url"])).connect() as conn:
    context.configure(connection=conn)
    with context.begin_transaction():
        context.run_migrations()
