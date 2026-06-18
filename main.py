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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
