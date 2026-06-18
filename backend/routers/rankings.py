from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.collector import collect_all
from backend.database import get_db
from backend.kakao import send_rank_alert, send_collection_summary
from backend.models import KeywordTop10History, ProductRankHistory, TrackedProduct, WatchKeyword

router = APIRouter(prefix="/rankings", tags=["rankings"])


class ProductRankOut(BaseModel):
    product_id: int
    product_name: str
    store_name: str
    keyword: str
    rank: int | None
    prev_rank: int | None
    collected_at: str


class KeywordTop10Out(BaseModel):
    keyword: str
    rank: int
    product_name: str
    mall_name: str
    product_url: str
    price: int | None
    collected_at: str


@router.get("/products", response_model=list[ProductRankOut])
def get_product_rankings(db: Session = Depends(get_db)):
    products = db.query(TrackedProduct).filter(TrackedProduct.is_active == True).all()  # noqa: E712
    result = []
    for product in products:
        for pk in product.keywords:
            latest_two = (
                db.query(ProductRankHistory)
                .filter(
                    ProductRankHistory.product_id == product.id,
                    ProductRankHistory.keyword == pk.keyword,
                )
                .order_by(desc(ProductRankHistory.collected_at))
                .limit(2)
                .all()
            )
            if not latest_two:
                continue
            curr = latest_two[0]
            prev_rank = latest_two[1].rank if len(latest_two) > 1 else None
            result.append(
                ProductRankOut(
                    product_id=product.id,
                    product_name=product.product_name,
                    store_name=product.store.name,
                    keyword=pk.keyword,
                    rank=curr.rank,
                    prev_rank=prev_rank,
                    collected_at=curr.collected_at.isoformat(),
                )
            )
    return result


@router.get("/products/{product_id}/history", response_model=list[dict])
def get_product_rank_history(product_id: int, keyword: str, limit: int = 30, db: Session = Depends(get_db)):
    rows = (
        db.query(ProductRankHistory)
        .filter(
            ProductRankHistory.product_id == product_id,
            ProductRankHistory.keyword == keyword,
        )
        .order_by(desc(ProductRankHistory.collected_at))
        .limit(limit)
        .all()
    )
    return [{"rank": r.rank, "collected_at": r.collected_at.isoformat()} for r in reversed(rows)]


@router.get("/keywords", response_model=list[KeywordTop10Out])
def get_keyword_top10(db: Session = Depends(get_db)):
    keywords = db.query(WatchKeyword).filter(WatchKeyword.is_active == True).all()  # noqa: E712
    result = []
    for wk in keywords:
        latest_ts = (
            db.query(KeywordTop10History.collected_at)
            .filter(KeywordTop10History.watch_keyword_id == wk.id)
            .order_by(desc(KeywordTop10History.collected_at))
            .first()
        )
        if not latest_ts:
            continue
        rows = (
            db.query(KeywordTop10History)
            .filter(
                KeywordTop10History.watch_keyword_id == wk.id,
                KeywordTop10History.collected_at == latest_ts[0],
            )
            .order_by(KeywordTop10History.rank)
            .all()
        )
        for row in rows:
            result.append(
                KeywordTop10Out(
                    keyword=wk.keyword,
                    rank=row.rank,
                    product_name=row.product_name,
                    mall_name=row.mall_name,
                    product_url=row.product_url,
                    price=row.price,
                    collected_at=row.collected_at.isoformat(),
                )
            )
    return result


@router.post("/collect")
def manual_collect(db: Session = Depends(get_db)):
    """수동 수집 트리거."""
    prev_ranks: dict[tuple, int | None] = {}
    products = db.query(TrackedProduct).filter(TrackedProduct.is_active == True).all()  # noqa: E712
    for p in products:
        for pk in p.keywords:
            latest = (
                db.query(ProductRankHistory)
                .filter(ProductRankHistory.product_id == p.id, ProductRankHistory.keyword == pk.keyword)
                .order_by(desc(ProductRankHistory.collected_at))
                .first()
            )
            prev_ranks[(p.id, pk.keyword)] = latest.rank if latest else None

    result = collect_all(db)

    alerts = []
    for p in products:
        for pk in p.keywords:
            latest = (
                db.query(ProductRankHistory)
                .filter(ProductRankHistory.product_id == p.id, ProductRankHistory.keyword == pk.keyword)
                .order_by(desc(ProductRankHistory.collected_at))
                .first()
            )
            curr_rank = latest.rank if latest else None
            prev_rank = prev_ranks.get((p.id, pk.keyword))
            if curr_rank != prev_rank:
                alerts.append({"product": p.product_name, "keyword": pk.keyword, "prev": prev_rank, "curr": curr_rank})

    if alerts:
        send_rank_alert(alerts)
    send_collection_summary(result)

    return {**result, "alerts_sent": len(alerts)}
