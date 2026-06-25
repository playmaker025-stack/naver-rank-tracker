import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.collector import collect_all, _search_keyword, _item_matches_product, search_keyword_with_error
from backend.database import get_db
from backend.telegram import send_rank_alert, send_collection_summary
from backend.models import KeywordTop10History, ProductRankHistory, ProductTitleHistory, Store, TrackedProduct, WatchKeyword

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
    prev_rank: int | None = None
    prev_price: int | None = None
    our_store_name: str | None = None


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
                    collected_at=curr.collected_at.isoformat() + "Z",
                )
            )
    return result


@router.get("/history/all", response_model=list[dict])
def get_all_rankings_history(limit: int = 60, db: Session = Depends(get_db)):
    """모든 추적 상품×키워드 조합의 히스토리를 한번에 반환."""
    products = db.query(TrackedProduct).filter(TrackedProduct.is_active == True).all()  # noqa: E712
    result = []
    for product in products:
        for pk in product.keywords:
            rows = (
                db.query(ProductRankHistory)
                .filter(ProductRankHistory.product_id == product.id, ProductRankHistory.keyword == pk.keyword)
                .order_by(desc(ProductRankHistory.collected_at))
                .limit(limit)
                .all()
            )
            if rows:
                result.append({
                    "product_id": product.id,
                    "product_name": product.product_name,
                    "store_name": product.store.name,
                    "keyword": pk.keyword,
                    "history": [{"rank": r.rank, "collected_at": r.collected_at.isoformat() + "Z"} for r in reversed(rows)],
                })
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
    return [{"rank": r.rank, "collected_at": r.collected_at.isoformat() + "Z"} for r in reversed(rows)]


@router.get("/keywords", response_model=list[KeywordTop10Out])
def get_keyword_top10(db: Session = Depends(get_db)):
    keywords = db.query(WatchKeyword).filter(WatchKeyword.is_active == True).all()  # noqa: E712
    our_mall_map = {s.mall_name.replace(' ', '').lower(): s.name for s in db.query(Store).all()}
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

        # 이전 수집 데이터 (순위·가격 변동 감지용)
        prev_ts = (
            db.query(KeywordTop10History.collected_at)
            .filter(
                KeywordTop10History.watch_keyword_id == wk.id,
                KeywordTop10History.collected_at < latest_ts[0],
            )
            .order_by(desc(KeywordTop10History.collected_at))
            .first()
        )
        prev_data: dict[str, dict] = {}
        if prev_ts:
            for r in (
                db.query(KeywordTop10History)
                .filter(
                    KeywordTop10History.watch_keyword_id == wk.id,
                    KeywordTop10History.collected_at == prev_ts[0],
                )
                .all()
            ):
                prev_data[r.naver_product_id] = {"rank": r.rank, "price": r.price}

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
            prev = prev_data.get(row.naver_product_id, {})
            result.append(
                KeywordTop10Out(
                    keyword=wk.keyword,
                    rank=row.rank,
                    product_name=row.product_name,
                    mall_name=row.mall_name,
                    product_url=row.product_url,
                    price=row.price,
                    collected_at=row.collected_at.isoformat() + "Z",
                    prev_rank=prev.get("rank"),
                    prev_price=prev.get("price"),
                    our_store_name=our_mall_map.get(row.mall_name.replace(' ', '').lower()),
                )
            )
    return result


@router.get("/products/{product_id}/title-history")
def get_title_history(product_id: int, db: Session = Depends(get_db)):
    """상품 제목 변경 이력 (최근 30건)."""
    rows = (
        db.query(ProductTitleHistory)
        .filter(ProductTitleHistory.product_id == product_id)
        .order_by(desc(ProductTitleHistory.changed_at))
        .limit(30)
        .all()
    )
    return [
        {
            "old_title": r.old_title,
            "new_title": r.new_title,
            "changed_at": r.changed_at.isoformat() + "Z",
        }
        for r in rows
    ]


@router.get("/rank-changes")
def get_rank_changes(threshold: int = 5, db: Session = Depends(get_db)):
    """최근 24시간 이내 수집분 중 threshold 이상 순위 급변동 항목 반환."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
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
            if len(latest_two) < 2 or latest_two[0].rank is None or latest_two[1].rank is None:
                continue
            curr, prev = latest_two[0], latest_two[1]
            # 가장 최근 수집이 24시간을 초과하면 배너에 표시하지 않음
            collected_utc = curr.collected_at.replace(tzinfo=timezone.utc) if curr.collected_at.tzinfo is None else curr.collected_at
            if collected_utc < cutoff:
                continue
            diff = prev.rank - curr.rank  # 양수=상승, 음수=하락
            if abs(diff) >= threshold:
                result.append({
                    "product_name": product.product_name,
                    "keyword": pk.keyword,
                    "curr_rank": curr.rank,
                    "prev_rank": prev.rank,
                    "diff": diff,
                    "type": "surge" if diff > 0 else "drop",
                    "collected_at": curr.collected_at.isoformat() + "Z",
                })
    result.sort(key=lambda x: -abs(x["diff"]))
    return result


@router.get("/debug/env")
def debug_env():
    """환경변수 주입 확인용 (키 이름만 반환)."""
    import os
    keys = sorted(os.environ.keys())
    naver_id = os.environ.get("NAVER_CLIENT_ID", "NOT_SET")
    naver_secret = os.environ.get("NAVER_CLIENT_SECRET", "NOT_SET")
    return {
        "NAVER_CLIENT_ID": naver_id[:6] + "..." if naver_id != "NOT_SET" else "NOT_SET",
        "NAVER_CLIENT_SECRET": naver_secret[:4] + "..." if naver_secret != "NOT_SET" else "NOT_SET",
        "all_env_keys": [k for k in keys if not k.startswith("RAILWAY_") and k not in ("PATH", "HOME", "USER")],
    }


@router.get("/debug/search")
def debug_search(keyword: str, db: Session = Depends(get_db)):
    """키워드 검색 결과 원본 확인 (에러 포함)."""
    result = search_keyword_with_error(keyword)
    products = db.query(TrackedProduct).filter(TrackedProduct.is_active == True).all()  # noqa: E712

    result_items = []
    for i, item in enumerate(result["items"][:50], start=1):
        matched_product = None
        for p in products:
            if _item_matches_product(item, p):
                matched_product = p.product_name
                break
        result_items.append({
            "rank": i,
            "productId": item.get("productId"),
            "title": re.sub(r"<[^>]+>", "", item.get("title", "")),
            "mallName": item.get("mallName"),
            "link": item.get("link", ""),
            "matched": matched_product,
        })

    tracked = [{"id": p.id, "name": p.product_name, "naver_product_id": p.naver_product_id} for p in products]
    return {
        "keyword": keyword,
        "api_status": result.get("status_code"),
        "api_ok": result.get("ok"),
        "api_error": result.get("error"),
        "api_raw_error": result.get("raw"),
        "total_results": len(result["items"]),
        "tracked_products": tracked,
        "items": result_items,
    }



@router.post("/collect")
def manual_collect(db: Session = Depends(get_db)):
    """수동 수집 트리거."""
    import os
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

    # 스토어별 알림 분리
    store_alerts: dict[int, dict] = {}
    for p in products:
        if p.store_id not in store_alerts:
            token_key = (p.store.telegram_token_key if p.store else None) or "TELEGRAM_BOT_TOKEN"
            store_alerts[p.store_id] = {
                "alerts": [],
                "chat_id": p.store.telegram_chat_id if p.store else None,
                "bot_token": os.environ.get(token_key),
            }
        for pk in p.keywords:
            latest = (
                db.query(ProductRankHistory)
                .filter(ProductRankHistory.product_id == p.id, ProductRankHistory.keyword == pk.keyword)
                .order_by(desc(ProductRankHistory.collected_at))
                .first()
            )
            curr_rank = latest.rank if latest else None
            prev_rank = prev_ranks.get((p.id, pk.keyword))
            is_notable = (
                (prev_rank is None and curr_rank is not None)
                or (prev_rank is not None and curr_rank is not None and abs(prev_rank - curr_rank) >= 5)
            )
            if is_notable:
                store_alerts[p.store_id]["alerts"].append(
                    {"product": p.product_name, "keyword": pk.keyword, "prev": prev_rank, "curr": curr_rank}
                )

    total_alerts = 0
    for info in store_alerts.values():
        if info["alerts"]:
            send_rank_alert(info["alerts"], chat_id=info["chat_id"], bot_token=info["bot_token"])
            total_alerts += len(info["alerts"])

    store_channels = [
        {"chat_id": info["chat_id"], "bot_token": info["bot_token"]}
        for info in store_alerts.values()
        if info["chat_id"] or info["bot_token"]
    ]
    send_collection_summary(result, store_channels=store_channels or None)

    return {**result, "alerts_sent": total_alerts}
