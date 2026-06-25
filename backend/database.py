import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_raw_url = os.environ.get("DATABASE_URL", "sqlite:///./data/rankings.db")

# Railway는 postgres:// 스킴을 주는데 SQLAlchemy는 postgresql:// 만 인식
if _raw_url.startswith("postgres://"):
    _raw_url = _raw_url.replace("postgres://", "postgresql://", 1)

DATABASE_URL = _raw_url

if DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
else:
    _connect_args = {"connect_timeout": 30}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from backend import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations():
    from sqlalchemy import text
    for col_def in [
        ("stores", "telegram_chat_id", "VARCHAR"),
        ("stores", "telegram_token_key", "VARCHAR"),
        ("tracked_products", "naver_title", "VARCHAR"),
    ]:
        table, col, typ = col_def
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"))
                conn.commit()
        except Exception:
            pass
    # product_tag_history 테이블 생성 (모델로 자동 처리)
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS product_tag_history (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES tracked_products(id),
                    old_tags VARCHAR NOT NULL,
                    new_tags VARCHAR NOT NULL,
                    changed_at TIMESTAMP NOT NULL
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_product_tag_history_product_id ON product_tag_history(product_id)"))
            conn.commit()
    except Exception:
        pass
