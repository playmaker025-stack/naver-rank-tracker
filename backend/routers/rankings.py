import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.collector import collect_all, _search_keyword, _item_matches_product, search_keyword_with_error
from backend.database import get_db
from backend.telegram import send_rank_alert, send_collection_summary
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
                    "history": [{"rank": r.rank, "collected_at": r.collected_at.isoformat()} for r in reversed(rows)],
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
            "link": item.get("link", "")[:80],
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


@router.get("/debug/scraper")
def debug_scraper(keyword: str):
    """스크래퍼 상세 디버그."""
    from playwright.sync_api import sync_playwright
    result = {
        "page_title": "",
        "final_url": "",
        "captured_xhr_urls": [],
        "captured_xhr_count": 0,
        "next_data_keys": [],
        "dom_counts": {},
        "items": [],
        "error": None,
    }
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="ko-KR",
            )
            page = context.new_page()

            captured = []
            xhr_urls = []

            def _on_resp(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    xhr_urls.append(f"{resp.status} {resp.url[:100]}")
                    if resp.status == 200 and "json" in ct:
                        data = resp.json()
                        lst = (
                            data.get("shoppingResult", {}).get("products", {}).get("list")
                            or data.get("products", {}).get("list")
                            or data.get("list") or []
                        )
                        if lst:
                            captured.extend(lst)
                except Exception:
                    pass

            page.on("response", _on_resp)
            page.goto(
                f"https://search.shopping.naver.com/search/all?query={keyword}&sort=RANK",
                timeout=40000, wait_until="load",
            )
            page.wait_for_timeout(5000)

            result["page_title"] = page.title()
            result["final_url"] = page.url
            result["captured_xhr_urls"] = xhr_urls[-30:]
            result["captured_xhr_count"] = len(captured)

            # __NEXT_DATA__ 키 확인
            nd = page.evaluate("() => { try { const e=document.getElementById('__NEXT_DATA__'); return e?JSON.parse(e.textContent):null; } catch(e){return null;} }")
            if nd:
                result["next_data_keys"] = list(nd.get("props", {}).get("pageProps", {}).keys())[:20]

            # DOM 선택자별 개수 확인
            for sel in ['[class*="product_item"]', '[class*="basicList"] li',
                        '[class*="productList"] li', 'li[data-id]', 'ul li']:
                count = page.evaluate(f'() => document.querySelectorAll({repr(sel)}).length')
                result["dom_counts"][sel] = count

            result["items"] = captured[:3]
            browser.close()
    except Exception as e:
        result["error"] = str(e)[:300]
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
