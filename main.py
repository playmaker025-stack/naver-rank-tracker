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
from backend.routers import stores, products, keywords, rankings


async def _init_db_bg() -> None:
    """init_db()를 스레드풀에서 실행 — 이벤트 루프 블로킹 방지."""
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.run_in_executor(None, init_db), timeout=60)
        logging.info("DB initialized")
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

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
