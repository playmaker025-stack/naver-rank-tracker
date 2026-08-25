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


def _dedupe_title_history() -> None:
    """같은 상품에 같은 (이전제목 → 새제목)이 중복 저장된 행 정리 (가장 이른 것만 남김).

    수집이 겹쳐 돌면 두 실행이 각자 옛 naver_title을 읽고 동일한 이력을 두 번 썼다
    (2026-08-05 03:22/03:27에 4개 상품). 재발은 collector.collect_lock으로 막았고,
    여기서는 이미 쌓인 행만 치운다. 공백 개수만 다른 것도 같은 전환으로 본다.
    """
    import re
    from backend.database import SessionLocal
    from backend.models import ProductTitleHistory

    def norm(t: str | None) -> str:
        return re.sub(r"\s+", " ", t or "").strip()

    try:
        db = SessionLocal()
        rows = db.query(ProductTitleHistory).order_by(
            ProductTitleHistory.product_id, ProductTitleHistory.changed_at
        ).all()
        # 바로 앞 이력과 같은 전환일 때만 지운다. A→B ... B→A ... A→B 처럼
        # 사이에 다른 변경이 낀 건 실제로 제목을 되돌린 것이므로 남겨야 한다.
        prev_key: tuple | None = None
        prev_product: int | None = None
        dup_ids = []
        for r in rows:
            key = (r.product_id, norm(r.old_title), norm(r.new_title))
            if r.product_id == prev_product and key == prev_key:
                dup_ids.append(r.id)
                continue
            prev_product, prev_key = r.product_id, key
        if dup_ids:
            db.query(ProductTitleHistory).filter(
                ProductTitleHistory.id.in_(dup_ids)
            ).delete(synchronize_session=False)
            db.commit()
            logging.info("중복 제목 이력 %d건 정리 완료", len(dup_ids))
        db.close()
    except Exception as exc:
        logging.error("중복 이력 정리 실패: %s", exc)


async def _init_db_bg() -> None:
    """init_db()를 스레드풀에서 실행 — 이벤트 루프 블로킹 방지."""
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.run_in_executor(None, init_db), timeout=60)
        logging.info("DB initialized")
        _purge_false_title_history()
        _dedupe_title_history()
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
