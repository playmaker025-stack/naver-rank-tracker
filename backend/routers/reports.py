from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    KeywordCompetitorSnapshot,
    ProductPageMetrics,
    ProductRankHistory,
    Store,
    TrackedProduct,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{product_id}")
def get_report(product_id: int, keyword: str, db: Session = Depends(get_db)):
    product = db.query(TrackedProduct).filter(TrackedProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # ── 순위 히스토리 (최근 30회) ──
    rank_rows = (
        db.query(ProductRankHistory)
        .filter(ProductRankHistory.product_id == product_id, ProductRankHistory.keyword == keyword)
        .order_by(desc(ProductRankHistory.collected_at))
        .limit(30)
        .all()
    )
    current_rank = rank_rows[0].rank if rank_rows else None
    prev_rank = rank_rows[1].rank if len(rank_rows) > 1 else None

    # ── 최신 경쟁사 스냅샷 ──
    latest_ts = (
        db.query(KeywordCompetitorSnapshot.collected_at)
        .filter(KeywordCompetitorSnapshot.keyword == keyword)
        .order_by(desc(KeywordCompetitorSnapshot.collected_at))
        .first()
    )
    current_competitors = []
    if latest_ts:
        current_competitors = (
            db.query(KeywordCompetitorSnapshot)
            .filter(
                KeywordCompetitorSnapshot.keyword == keyword,
                KeywordCompetitorSnapshot.collected_at == latest_ts[0],
            )
            .order_by(KeywordCompetitorSnapshot.search_rank)
            .all()
        )

    # ── 이전 경쟁사 스냅샷 (신규 진입·가격 변동 감지) ──
    prev_product_ids: set[str] = set()
    prev_prices: dict[str, int | None] = {}
    if latest_ts:
        prev_ts = (
            db.query(KeywordCompetitorSnapshot.collected_at)
            .filter(
                KeywordCompetitorSnapshot.keyword == keyword,
                KeywordCompetitorSnapshot.collected_at < latest_ts[0],
            )
            .order_by(desc(KeywordCompetitorSnapshot.collected_at))
            .first()
        )
        if prev_ts:
            prev_snap_rows = (
                db.query(KeywordCompetitorSnapshot)
                .filter(
                    KeywordCompetitorSnapshot.keyword == keyword,
                    KeywordCompetitorSnapshot.collected_at == prev_ts[0],
                )
                .all()
            )
            for r in prev_snap_rows:
                if r.naver_product_id:
                    prev_product_ids.add(r.naver_product_id)
                    prev_prices[r.naver_product_id] = r.price

    # ── 등록된 모든 스토어 맵 (mall_name → 스토어명) ──
    all_stores = db.query(Store).order_by(Store.id).all()
    our_mall_map = {s.mall_name.replace(" ", "").lower(): s.name for s in all_stores}
    pid = product.naver_product_id

    def _our_store_name(c) -> str | None:
        return our_mall_map.get(c.mall_name.replace(" ", "").lower())

    def _is_ours(c) -> bool:
        return bool(_our_store_name(c)) or bool(c.naver_product_id and c.naver_product_id == pid)

    # naver_product_id 정확 매칭 우선, 없으면 mall_name 폴백
    our_entry = next((c for c in current_competitors if c.naver_product_id == pid), None)
    if our_entry is None:
        our_entry = next((c for c in current_competitors if _is_ours(c)), None)
    our_title = our_entry.title if our_entry else (product.product_name or "")

    # ── 가격 분석 (우리 상품 제외한 경쟁사 가격으로 통계) ──
    competitor_prices = [c.price for c in current_competitors if c.price and not (c.naver_product_id == pid)]
    our_price = our_entry.price if our_entry and our_entry.naver_product_id == pid else None

    # ── 키워드 최적화 분석 ──
    top10 = [c for c in current_competitors if c.search_rank <= 10]
    kw_lower = keyword.lower()
    top10_with_kw = sum(1 for c in top10 if kw_lower in (c.title or "").lower())
    keyword_in_our_title = kw_lower in our_title.lower()

    # ── 신규 진입 경쟁사 (TOP10) ──
    new_competitors = [
        c for c in top10
        if c.naver_product_id
        and c.naver_product_id not in prev_product_ids
        and not _is_ours(c)
    ]

    # ── 페이지 메트릭 (최근 2회) ──
    metric_rows = (
        db.query(ProductPageMetrics)
        .filter(ProductPageMetrics.product_id == product_id)
        .order_by(desc(ProductPageMetrics.collected_at))
        .limit(2)
        .all()
    )

    def _m(row):
        if not row:
            return None
        return {
            "review_count": row.review_count,
            "rating": row.rating,
            "wishlist_count": row.wishlist_count,
            "collected_at": row.collected_at.isoformat(),
        }

    return {
        "product_id": product_id,
        "product_name": product.product_name,
        "store_name": product.store.name if product.store else "",
        "keyword": keyword,
        "product_url": product.product_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registered_stores": [{"id": s.id, "name": s.name} for s in all_stores],
        "rank": {
            "current": current_rank,
            "prev": prev_rank,
            "history": [
                {"rank": r.rank, "collected_at": r.collected_at.isoformat()}
                for r in reversed(rank_rows)
            ],
        },
        "price": {
            "ours": our_price,
            "top20_avg": int(sum(competitor_prices) / len(competitor_prices)) if competitor_prices else None,
            "top20_min": min(competitor_prices) if competitor_prices else None,
            "top20_max": max(competitor_prices) if competitor_prices else None,
        },
        "keyword_optimization": {
            "in_our_title": keyword_in_our_title,
            "our_title": our_title,
            "top10_with_keyword": top10_with_kw,
            "top10_total": len(top10),
        },
        "competitors": [
            {
                "rank": c.search_rank,
                "title": c.title,
                "mall_name": c.mall_name,
                "price": c.price,
                "prev_price": prev_prices.get(c.naver_product_id) if c.naver_product_id else None,
                "price_changed": bool(
                    c.naver_product_id
                    and c.naver_product_id in prev_prices
                    and prev_prices[c.naver_product_id] != c.price
                ),
                "is_ours": _is_ours(c),
                "our_store_name": _our_store_name(c),
                "is_new": bool(
                    c.naver_product_id
                    and c.naver_product_id not in prev_product_ids
                    and c.naver_product_id != pid
                ),
            }
            for c in current_competitors
        ],
        "new_competitors": [
            {"rank": c.search_rank, "title": c.title, "mall_name": c.mall_name, "price": c.price}
            for c in new_competitors
        ],
        "page_metrics": {
            "current": _m(metric_rows[0] if metric_rows else None),
            "prev": _m(metric_rows[1] if len(metric_rows) > 1 else None),
        },
    }
