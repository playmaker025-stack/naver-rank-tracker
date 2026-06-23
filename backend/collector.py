import os
import re
import httpx
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.models import (
    KeywordTop10History,
    ProductRankHistory,
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
    """상품 URL로 네이버 쇼핑에서 상품명과 productId를 가져온다."""
    product_id = _extract_product_id_from_url(product_url)
    if not product_id:
        return None

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                NAVER_API_URL,
                headers=_naver_headers(),
                params={"query": product_id, "display": 10},
            )
            resp.raise_for_status()
            data = resp.json()

        for item in data.get("items", []):
            if item.get("productId") == product_id:
                return {
                    "naver_product_id": product_id,
                    "product_name": re.sub(r"<[^>]+>", "", item.get("title", "")),
                    "product_url": item.get("link", product_url),
                }
        # productId 매칭 실패 시 URL 기반으로 기본값 반환
        return {"naver_product_id": product_id, "product_name": "", "product_url": product_url}
    except Exception:
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
    product ID와 다를 수 있다. link URL 포함 여부와 mallName으로 보완한다.
    """
    pid = product.naver_product_id

    # 1) productId 직접 일치
    if item.get("productId") == pid:
        return True

    # 2) link URL 안에 상품 ID 포함 (SmartStore URL 형태 매칭)
    link = item.get("link", "")
    if pid and len(pid) >= 8 and pid in link:
        return True

    # 3) mallName 일치 + link 안에 ID 포함
    mall_name = product.store.mall_name if product.store else ""
    if mall_name and item.get("mallName") == mall_name and pid in link:
        return True

    return False


def _get_keyword_items(keyword: str) -> list[dict]:
    """키워드로 네이버 쇼핑 검색 결과 반환."""
    return _search_keyword(keyword)


def collect_product_rankings(db: Session, collected_at: datetime | None = None) -> int:
    """활성화된 모든 추적 상품의 키워드별 순위를 수집한다."""
    if collected_at is None:
        collected_at = datetime.now(timezone.utc)

    products = (
        db.query(TrackedProduct)
        .filter(TrackedProduct.is_active == True)  # noqa: E712
        .all()
    )

    keyword_cache: dict[str, tuple[list[dict], str]] = {}
    saved = 0
    for product in products:
        for pk in product.keywords:
            if pk.keyword not in keyword_cache:
                keyword_cache[pk.keyword] = _get_keyword_items(pk.keyword)

            items = keyword_cache[pk.keyword]
            rank = None
            for i, item in enumerate(items, start=1):
                if _item_matches_product(item, product):
                    rank = i
                    break

            db.add(
                ProductRankHistory(
                    product_id=product.id,
                    keyword=pk.keyword,
                    rank=rank,
                    collected_at=collected_at,
                )
            )
            saved += 1

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
