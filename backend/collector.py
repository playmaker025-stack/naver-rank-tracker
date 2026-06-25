import json
import os
import re
import httpx
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.models import (
    KeywordCompetitorSnapshot,
    KeywordTop10History,
    ProductPageMetrics,
    ProductRankHistory,
    ProductTagHistory,
    ProductTitleHistory,
    TrackedProduct,
    WatchKeyword,
)

NAVER_API_URL = "https://openapi.naver.com/v1/search/shop.json"
SEARCH_DISPLAY = 100  # 최대 100개까지 조회


def _naver_headers() -> dict:
    return {
        "X-Naver-Client-Id": os.environ["NAVER_CLIENT_ID"],
        "X-Naver-Client-Secret": os.environ["NAVER_CLIENT_SECRET"],
    }


def _extract_product_id_from_url(url: str) -> str | None:
    """네이버 스마트스토어 상품 URL에서 productId를 추출한다."""
    # https://smartstore.naver.com/.../products/1234567890
    m = re.search(r"/products/(\d+)", url)
    if m:
        return m.group(1)
    # https://search.shopping.naver.com/catalog/...?nvMid=1234567890
    m = re.search(r"[?&]nvMid=(\d+)", url)
    if m:
        return m.group(1)
    # 마지막 경로 세그먼트가 숫자인 경우
    m = re.search(r"/(\d{8,})(?:[?#]|$)", url)
    if m:
        return m.group(1)
    return None


def fetch_product_info(product_url: str) -> dict | None:
    """상품 URL로 상품명과 productId를 가져온다.
    1순위: 커머스 API (가장 정확)
    2순위: 네이버 쇼핑 검색 API + 링크 URL 매칭
    """
    product_id = _extract_product_id_from_url(product_url)
    if not product_id:
        return None

    # 1순위: 커머스 API
    try:
        from backend.commerce import fetch_product_name
        name = fetch_product_name(product_id)
        if name:
            return {
                "naver_product_id": product_id,
                "product_name": name,
                "product_url": product_url,
            }
    except Exception:
        pass

    # 2순위: 쇼핑 검색 API — 링크 URL에서 product_id 매칭
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                NAVER_API_URL,
                headers=_naver_headers(),
                params={"query": product_id, "display": 20},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])

        for item in items:
            link = item.get("link", "")
            if re.search(rf"(?<!\d){re.escape(product_id)}(?!\d)", link):
                return {
                    "naver_product_id": product_id,
                    "product_name": re.sub(r"<[^>]+>", "", item.get("title", "")),
                    "product_url": link or product_url,
                }
    except Exception:
        pass

    return {"naver_product_id": product_id, "product_name": "", "product_url": product_url}


def _search_keyword(keyword: str) -> list[dict]:
    """키워드로 네이버 쇼핑을 검색하고 결과 목록을 반환한다."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                NAVER_API_URL,
                headers=_naver_headers(),
                params={"query": keyword, "display": SEARCH_DISPLAY, "sort": "sim"},
            )
            resp.raise_for_status()
            return resp.json().get("items", [])
    except Exception:
        return []


def search_keyword_with_error(keyword: str) -> dict:
    """디버깅용: 에러 메시지까지 포함해서 반환한다."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                NAVER_API_URL,
                headers=_naver_headers(),
                params={"query": keyword, "display": SEARCH_DISPLAY, "sort": "sim"},
            )
            return {
                "status_code": resp.status_code,
                "ok": resp.status_code == 200,
                "items": resp.json().get("items", []) if resp.status_code == 200 else [],
                "raw": resp.json() if resp.status_code != 200 else None,
            }
    except Exception as e:
        return {"status_code": None, "ok": False, "items": [], "error": str(e)}


def _item_matches_product(item: dict, product: "TrackedProduct") -> bool:
    """API 결과 한 건이 추적 상품과 일치하는지 판별한다.

    Naver Shopping API의 productId는 카탈로그 ID라서 SmartStore URL의
    product ID와 다를 수 있다. link URL에서 숫자 경계 기반으로 정확히 매칭한다.
    """
    pid = product.naver_product_id

    # 1) 카탈로그 productId 직접 일치
    if item.get("productId") == pid:
        return True

    # 2) link URL에 상품 ID가 독립된 숫자 세그먼트로 포함 (부분 매칭 방지)
    # (?<!\d)pid(?!\d) → 앞뒤에 다른 숫자가 붙으면 매칭 안 됨
    # 예: pid="12345678"이 "123456789" 링크에 오탐되지 않음
    if pid and len(pid) >= 8:
        link = item.get("link", "")
        if re.search(rf"(?<!\d){re.escape(pid)}(?!\d)", link):
            return True

    return False


def _get_keyword_items(keyword: str) -> list[dict]:
    """키워드로 네이버 쇼핑 검색 결과 반환."""
    return _search_keyword(keyword)


def _fetch_page_metrics(product_url: str) -> dict:
    """SmartStore 상품 페이지에서 리뷰수·평점·찜수 추출."""
    if not product_url or "smartstore.naver.com" not in product_url:
        return {}
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(product_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9",
            })
        if resp.status_code != 200:
            return {}
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        if not m:
            return {}
        nd = json.loads(m.group(1))
        detail = nd["props"]["pageProps"]["initialState"]["product"]["productDetail"]
        ra = detail.get("reviewAmount", {})
        review_count = int(ra.get("totalReviewCount", 0)) or None
        score = ra.get("averageReviewScore", "")
        rating = round(float(score), 1) if score else None
        wc = detail.get("benefitSection", {}).get("wishCount") or detail.get("wishCount")
        wishlist_count = int(wc) if wc else None
        return {"review_count": review_count, "rating": rating, "wishlist_count": wishlist_count}
    except Exception:
        return {}


def collect_product_rankings(db: Session, collected_at: datetime | None = None) -> int:
    """활성화된 모든 추적 상품의 키워드별 순위를 수집한다."""
    if collected_at is None:
        collected_at = datetime.now(timezone.utc)

    products = (
        db.query(TrackedProduct)
        .filter(TrackedProduct.is_active == True)  # noqa: E712
        .all()
    )

    from backend.commerce import fetch_product_tags

    keyword_cache: dict[str, list[dict]] = {}
    competitor_saved: set[str] = set()   # 키워드당 1회만 저장
    metrics_saved: set[int] = set()      # 제품당 1회만 저장
    saved = 0

    for product in products:
        # SmartStore 페이지 메트릭 수집
        if product.id not in metrics_saved:
            m = _fetch_page_metrics(product.product_url)
            if m and any(v is not None for v in m.values()):
                db.add(ProductPageMetrics(
                    product_id=product.id,
                    collected_at=collected_at,
                    **m,
                ))
            metrics_saved.add(product.id)

        found_title: str | None = None  # 이번 수집에서 API로 확인된 실제 제목

        for pk in product.keywords:
            if pk.keyword not in keyword_cache:
                keyword_cache[pk.keyword] = _get_keyword_items(pk.keyword)

            items = keyword_cache[pk.keyword]
            rank = None
            for i, item in enumerate(items, start=1):
                if _item_matches_product(item, product):
                    rank = i
                    # 처음 발견된 제목을 기록
                    if found_title is None:
                        found_title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                    break

            db.add(ProductRankHistory(
                product_id=product.id,
                keyword=pk.keyword,
                rank=rank,
                collected_at=collected_at,
            ))
            saved += 1

            # 경쟁사 스냅샷 저장 (키워드당 1회)
            if pk.keyword not in competitor_saved:
                for i, item in enumerate(items[:20], start=1):
                    try:
                        price = int(item.get("lprice", "0") or "0") or None
                    except (ValueError, TypeError):
                        price = None
                    db.add(KeywordCompetitorSnapshot(
                        keyword=pk.keyword,
                        collected_at=collected_at,
                        search_rank=i,
                        naver_product_id=item.get("productId"),
                        title=re.sub(r"<[^>]+>", "", item.get("title", "")),
                        mall_name=item.get("mallName", ""),
                        price=price,
                    ))
                competitor_saved.add(pk.keyword)

        # 제목 변경 감지: naver_title(마지막 수집 제목)과 비교
        if found_title:
            last_naver_title = product.naver_title or product.product_name
            if found_title != last_naver_title:
                has_prior = product.naver_title is not None or db.query(ProductRankHistory).filter(
                    ProductRankHistory.product_id == product.id,
                    ProductRankHistory.collected_at < collected_at,
                ).first() is not None
                if has_prior:
                    db.add(ProductTitleHistory(
                        product_id=product.id,
                        old_title=last_naver_title,
                        new_title=found_title,
                        changed_at=collected_at,
                    ))
            product.naver_title = found_title

        # 태그 변경 감지: 커머스 API로 현재 태그 조회
        current_tags = fetch_product_tags(product.naver_product_id)
        if current_tags is not None:
            current_tags_str = ",".join(sorted(current_tags))
            last_tag_row = (
                db.query(ProductTagHistory)
                .filter(ProductTagHistory.product_id == product.id)
                .order_by(ProductTagHistory.changed_at.desc())
                .first()
            )
            last_tags_str = last_tag_row.new_tags if last_tag_row else None
            if last_tags_str is None:
                # 최초 수집: 이력 없이 기준값만 기록
                db.add(ProductTagHistory(
                    product_id=product.id,
                    old_tags="",
                    new_tags=current_tags_str,
                    changed_at=collected_at,
                ))
            elif current_tags_str != last_tags_str:
                db.add(ProductTagHistory(
                    product_id=product.id,
                    old_tags=last_tags_str,
                    new_tags=current_tags_str,
                    changed_at=collected_at,
                ))

    db.commit()
    return saved


def collect_keyword_top10(db: Session, collected_at: datetime | None = None) -> int:
    """지정 키워드의 상위 10개 상품을 수집한다."""
    if collected_at is None:
        collected_at = datetime.now(timezone.utc)

    watch_keywords = (
        db.query(WatchKeyword)
        .filter(WatchKeyword.is_active == True)  # noqa: E712
        .all()
    )

    saved = 0
    for wk in watch_keywords:
        items = _search_keyword(wk.keyword)
        for rank, item in enumerate(items[:10], start=1):
            price_str = item.get("lprice", "0") or "0"
            try:
                price = int(price_str)
            except ValueError:
                price = None

            db.add(
                KeywordTop10History(
                    watch_keyword_id=wk.id,
                    rank=rank,
                    naver_product_id=item.get("productId", ""),
                    product_name=re.sub(r"<[^>]+>", "", item.get("title", "")),
                    mall_name=item.get("mallName", ""),
                    product_url=item.get("link", ""),
                    price=price,
                    collected_at=collected_at,
                )
            )
            saved += 1

    db.commit()
    return saved


def collect_all(db: Session) -> dict:
    """전체 수집 실행 (스케줄러에서 호출)."""
    now = datetime.now(timezone.utc)
    product_count = collect_product_rankings(db, now)
    keyword_count = collect_keyword_top10(db, now)
    return {"products": product_count, "keywords": keyword_count, "collected_at": now.isoformat()}
