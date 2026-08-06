import json
import random
import re
import time
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

# 네이버 쇼핑검색 API(openapi.naver.com/v1/search/shop.json)는 2026-07-31 종료됐고
# 공식 대체 API가 없다(API HUB에도 쇼핑 검색은 없음). 대신 모바일 통합검색 HTML에
# 임베드된 쇼핑 모듈 JSON에서 순위를 파싱한다. 과거 수집값과 대조해 종료된 API와
# 동일한 랭킹 소스임을 확인했으나, 노출 깊이가 25위까지라 그 밖은 '순위 없음'이 된다.
NAVER_SEARCH_URL = "https://m.search.naver.com/search.naver"
SEARCH_DISPLAY = 25  # 모바일 통합검색 쇼핑모듈 상한 (페이지 파라미터로 확장 불가)

# 데스크톱 UA로는 같은 모듈이 8개만 실려온다. 모바일 UA여야 25개가 나온다.
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _search_headers() -> dict:
    return {
        "User-Agent": MOBILE_UA,
        "Accept-Language": "ko-KR,ko;q=0.9",
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

    # 2순위였던 쇼핑 검색 API는 종료됨. 상품 ID로 상품명을 역조회할 공개 경로가 없어
    # 커머스 API가 실패하면 상품명 없이 등록하고 이후 수집 때 채워지도록 둔다.
    return {"naver_product_id": product_id, "product_name": "", "product_url": product_url}


def _extract_json_object(text: str, start: int) -> str | None:
    """text[start]의 '{'부터 짝이 맞는 '}'까지를 잘라낸다.
    상품명에 중괄호가 들어있어도 깨지지 않도록 문자열/이스케이프 상태를 추적한다."""
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_shopping_cards(html: str) -> list[dict]:
    """통합검색 HTML에 임베드된 쇼핑 모듈 JSON에서 오가닉 상품 목록을 뽑아낸다.

    반환 형식은 기존 쇼핑검색 API 응답과 호환되게 맞춘다(productId/link/title/
    mallName/lprice). productId에는 카탈로그 ID가 아니라 스마트스토어 상품 ID
    (channelProductId)를 넣는다 — 추적 대상 상품과 정확히 일치 비교하기 위함.
    """
    items: list[dict] = []
    for m in re.finditer(r'\{"slotType":"CARD","data":\{"cardType":"ORGANIC_CARD"', html):
        raw = _extract_json_object(html, m.start())
        if not raw:
            continue
        # JS 리터럴이라 순수 JSON이 아니다: new Date(...) 와 undefined 를 정규화
        raw = re.sub(r'new Date\((\"[^\"]*\")\)', r"\1", raw)
        raw = re.sub(r":\s*undefined", ": null", raw)
        try:
            data = json.loads(raw).get("data", {})
        except Exception:
            continue

        channel_product_id = str(data.get("channelProductId") or "")
        if not channel_product_id:
            continue
        price = data.get("discountedSalePrice") or data.get("salePrice") or 0

        items.append({
            "productId": channel_product_id,
            "link": data.get("productUrl")
                    or f"https://smartstore.naver.com/main/products/{channel_product_id}",
            # 검색어 부분이 <mark>로 감싸여 오므로 태그를 제거한다
            "title": re.sub(r"<[^>]+>", "", data.get("productName") or ""),
            "mallName": data.get("mallName") or "",
            "lprice": str(price),
            "rank": data.get("rank"),
            "nvMid": str(data.get("nvMid") or ""),
            "isAdultRestricted": bool(data.get("isAdultContentRestricted")),
        })

    items.sort(key=lambda x: x["rank"] if x["rank"] is not None else 10**6)
    return items


def _fetch_search_html(keyword: str) -> str | None:
    # 키워드가 200개가 넘어 연속 요청하면 봇으로 판정돼 차단될 수 있다.
    # 수집은 백그라운드 작업이라 느려도 문제없으므로 요청 간격을 둔다.
    time.sleep(random.uniform(1.5, 3.5))
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(
                NAVER_SEARCH_URL,
                headers=_search_headers(),
                params={"query": keyword},
            )
            resp.raise_for_status()
            return resp.text
    except Exception:
        return None


def _search_keyword(keyword: str) -> list[dict] | None:
    """키워드로 네이버 쇼핑 순위를 조회하고 결과 목록을 반환한다.
    조회 자체가 실패하면 None (검색 결과가 0건인 것과 구분 — 호출자가
    '순위 없음'으로 잘못 기록하지 않도록)."""
    html = _fetch_search_html(keyword)
    if html is None:
        return None
    # 봇차단/캡차 페이지를 0건으로 오인하면 전 상품이 '순위 없음'으로 기록된다.
    if "wtm_captcha" in html or "쇼핑 서비스 접속이 일시적으로 제한" in html:
        return None
    return _parse_shopping_cards(html)


def search_keyword_with_error(keyword: str) -> dict:
    """디버깅용: 차단 여부와 원인까지 포함해서 반환한다."""
    html = _fetch_search_html(keyword)
    if html is None:
        return {"status_code": None, "ok": False, "items": [], "error": "request failed"}
    if "wtm_captcha" in html or "쇼핑 서비스 접속이 일시적으로 제한" in html:
        return {"status_code": 200, "ok": False, "items": [], "error": "blocked (captcha)"}
    items = _parse_shopping_cards(html)
    return {
        "status_code": 200,
        "ok": True,
        "items": items,
        "raw": None if items else "no ORGANIC_CARD found (구조 변경 가능성)",
    }


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


def _get_keyword_items(keyword: str) -> list[dict] | None:
    """키워드로 네이버 쇼핑 검색 결과 반환 (실패 시 None)."""
    return _search_keyword(keyword)


def _fetch_page_metrics(product_url: str, expected_product_id: str | None = None) -> dict:
    """SmartStore 상품 페이지에서 리뷰수·평점·찜수·상품명·검색태그 추출.
    상품명은 'scraped_title', 태그는 'scraped_tags' 키로 반환 — 둘 다
    ProductPageMetrics DB 저장 전에 pop해서 사용 (해당 컬럼 없음).

    expected_product_id가 주어지면 리다이렉트 이후 최종 URL이 그 상품 ID를
    가리키는지 확인한다 — 재고소진 시 유사상품으로 리다이렉트되는 등의
    이유로 엉뚱한 상품의 제목·태그를 긁어오는 걸 방지.
    """
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
        if expected_product_id:
            final_id = _extract_product_id_from_url(str(resp.url))
            if final_id and final_id != expected_product_id:
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
        scraped_title = (detail.get("name") or detail.get("channelProductDisplayName") or "").strip() or None
        seller_tags = detail.get("detailAttribute", {}).get("seoInfo", {}).get("sellerTags", [])
        # 빈 리스트(태그 0개)와 추출 실패를 구분 — []를 None으로 뭉개면
        # "판매자가 태그 다 지움"을 놓치고 오래된 커머스 API 값에 의존하게 됨
        scraped_tags = [t.get("text", "").strip() for t in seller_tags if t.get("text")]
        return {
            "review_count": review_count,
            "rating": rating,
            "wishlist_count": wishlist_count,
            "scraped_title": scraped_title,
            "scraped_tags": scraped_tags,
        }
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

    from backend.commerce import fetch_product_commerce_info

    keyword_cache: dict[str, list[dict]] = {}
    competitor_saved: set[str] = set()   # 키워드당 1회만 저장
    metrics_saved: set[int] = set()      # 제품당 1회만 저장
    saved = 0

    for product in products:
        # SmartStore 페이지 크롤링: 메트릭 + 상품명 + 태그 (한 번에)
        scraped_title: str | None = None
        scraped_tags: list[str] | None = None
        if product.id not in metrics_saved:
            m = _fetch_page_metrics(product.product_url, product.naver_product_id)
            scraped_title = m.pop("scraped_title", None)
            scraped_tags = m.pop("scraped_tags", None)
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
            if items is None:
                # 검색 API 호출 자체가 실패 — '순위 없음'으로 오기록하지 않고 이번 사이클 건너뜀
                continue
            rank = None
            for i, item in enumerate(items, start=1):
                if _item_matches_product(item, product):
                    # 네이버가 내려준 rank를 그대로 쓴다(순서 기반 추정보다 정확)
                    rank = item.get("rank") or i
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

        # 태그 변경 감지용 커머스 API 조회
        commerce_info = fetch_product_commerce_info(product.naver_product_id)
        commerce_tags = commerce_info["tags"] if commerce_info else None

        # 태그 감지 우선순위 (제목 감지와 동일한 이유):
        # 1) 페이지 크롤링 (네이버 봇차단 429시 None)
        # 2) 커머스 API (429 우회 가능, 판매자 인증 필요) — 유일한 폴백이었던 것을
        #    페이지 크롤링과 이중화. 하나가 막혀도 다른 쪽으로 계속 추적됨
        tags_for_detection = scraped_tags if scraped_tags is not None else commerce_tags

        # 제목 변경 감지 우선순위:
        # 1) 페이지 크롤링 (네이버 봇차단 429시 None)
        # 2) 커머스 API 실시간 제목 (429 우회 가능, 판매자 인증 필요)
        # 3) 검색 API 제목 (인덱스 반영 수일 소요 — 최후 폴백)
        commerce_title = commerce_info["name"] if commerce_info else None
        title_for_detection = scraped_title or commerce_title or found_title
        if title_for_detection:
            last_naver_title = product.naver_title or product.product_name
            if title_for_detection != last_naver_title:
                has_prior = product.naver_title is not None or db.query(ProductRankHistory).filter(
                    ProductRankHistory.product_id == product.id,
                    ProductRankHistory.collected_at < collected_at,
                ).first() is not None
                if has_prior:
                    db.add(ProductTitleHistory(
                        product_id=product.id,
                        old_title=last_naver_title,
                        new_title=title_for_detection,
                        changed_at=collected_at,
                    ))
            product.naver_title = title_for_detection

        # 태그 변경 감지
        if tags_for_detection is not None:
            current_tags_str = ",".join(sorted(tags_for_detection))
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
        if items is None:
            # 조회 실패 — 이번 사이클은 건너뛴다. (예전엔 여기서 None을 그대로
            # 슬라이싱해 TypeError가 나면서 collect_all 전체가 죽었다)
            continue
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
