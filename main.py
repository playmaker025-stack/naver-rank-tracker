import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

# SQLite 로컬 실행 시에만 data 디렉토리 생성
if not os.environ.get("DATABASE_URL"):
    Path("data").mkdir(exist_ok=True)

from backend.database import init_db
from backend.scheduler import start_scheduler, stop_scheduler
from backend.routers import stores, products, keywords, rankings, system, reports


def _purge_false_title_history() -> None:
    """첫 수집 오탐으로 생긴 제목 변경 이력 일괄 삭제 (2026-06-25 이전 기록)."""
    from datetime import datetime, timezone
    from backend.database import SessionLocal
    from backend.models import ProductTitleHistory
    cutoff = datetime(2026, 6, 25, 0, 0, 0, tzinfo=timezone.utc)
    try:
        db = SessionLocal()
        deleted = db.query(ProductTitleHistory).filter(
            ProductTitleHistory.changed_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()
        db.close()
        if deleted:
            logging.info("오탐 제목 이력 %d건 삭제 완료", deleted)
    except Exception as exc:
        logging.error("오탐 이력 삭제 실패: %s", exc)


async def _init_db_bg() -> None:
    """init_db()를 스레드풀에서 실행 — 이벤트 루프 블로킹 방지."""
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.run_in_executor(None, init_db), timeout=60)
        logging.info("DB initialized")
        _purge_false_title_history()
    except Exception as exc:
        logging.error("DB init failed (non-fatal): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_init_db_bg())
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="네이버 랭킹 트래커", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stores.router, prefix="/api")
app.include_router(products.router, prefix="/api")
app.include_router(keywords.router, prefix="/api")
app.include_router(rankings.router, prefix="/api")
app.include_router(system.router, prefix="/api")
app.include_router(reports.router, prefix="/api")

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/report.html")
def report_page():
    return FileResponse("static/report.html")
