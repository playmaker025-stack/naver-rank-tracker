import json
import random
import re
import threading
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

# 모바일 통합검색 쇼핑모듈 상한. 페이지 파라미터(page/pagingIndex/start/pageSize/
# shopPage)와 where=m_shop, ssc=tab.m_shop.all 모두 무시되어 확장 불가.
# 여기서 나온 순위는 PC 네이버쇼핑 순위와 일치함을 육안 대조로 확인했다
# (2026-08-06, '크로스미니 팟' 11위까지 / '제우스 코일' 일치).
# 따라서 1~25위는 PC 기준 실제 순위이고, 미관측은 'PC 26위 이하'까지만 말할 수 있다.
# PC 1페이지가 40개라 26~40위와 41위 이하는 구분되지 않는다.
SEARCH_DISPLAY = 25

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


def _card_to_item(data: dict) -> dict | None:
    """쇼핑 카드 하나를 기존 API 응답 형식으로 변환. 검증 실패 시 None.

    productId에는 카탈로그 ID가 아니라 스마트스토어 상품 ID(channelProductId)를
    넣는다 — 추적 대상 상품과 정확히 일치 비교하기 위함.
    """
    # 네 필드를 모두 확인한다. cardType만 보면 다른 종류의 오가닉 카드가
    # 같은 형태로 섞여 들어와도 걸러내지 못한다.
    if (
        data.get("cardType") != "ORGANIC_CARD"
        or data.get("sourceType") != "SAS"
        or data.get("sasType") != "SHOPPING"
    ):
        return None

    channel_product_id = str(data.get("channelProductId") or "")
    if not channel_product_id.isdigit():
        return None

    # rank는 네이버가 주는 값만 쓴다. 없으면 그 카드는 무효 — 배열 위치로
    # 순위를 지어내면 안 된다(틀린 순위가 맞는 순위처럼 저장된다).
    rank = data.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
        return None

    price = data.get("discountedSalePrice") or data.get("salePrice") or 0

    # productUrl은 문자열이 아니라 {"pcUrl": ..., "mobileUrl": ...} 객체로 온다.
    # (최상위 pcUrl/mobileUrl 키는 항상 null이라 쓸 수 없다)
    url_obj = data.get("productUrl")
    link = url_obj.get("pcUrl") or url_obj.get("mobileUrl") if isinstance(url_obj, dict) else url_obj
    if not isinstance(link, str) or not link:
        link = f"https://smartstore.naver.com/main/products/{channel_product_id}"

    return {
        "productId": channel_product_id,
        "link": link,
        # 검색어 부분이 <mark>로 감싸여 오므로 태그를 제거한다
        "title": re.sub(r"<[^>]+>", "", str(data.get("productName") or "")),
        "mallName": str(data.get("mallName") or ""),
        "lprice": str(price),
        "rank": rank,
        "nvMid": str(data.get("nvMid") or ""),
        "isAdultRestricted": bool(data.get("isAdultContentRestricted")),
    }


def _parse_shopping_cards(html: str) -> list[dict] | None:
    """통합검색 HTML에 임베드된 쇼핑 모듈 JSON에서 오가닉 상품 목록을 뽑아낸다.

    반환값 구분:
      list  — 파싱 성공 (빈 리스트 = 쇼핑 모듈 자체가 없음 = 정상적인 0건)
      None  — 쇼핑 모듈은 있는데 유효 카드를 못 뽑음 = 구조 변경 의심, 수집 중단

    이 구분이 중요한 이유: 구조가 바뀌어 0건이 되면 전 상품이 '순위 없음'으로
    기록돼 순위가 통째로 사라진 것처럼 보인다. 조용히 틀린 데이터를 쌓느니
    이번 회차를 버리는 게 낫다.
    """
    items: list[dict] = []
    seen_ranks: set[int] = set()
    module_present = False

    for m in re.finditer(r'"cardType"\s*:\s*"ORGANIC_CARD"', html):
        module_present = True
        start = html.rfind("{", 0, m.start())
        # 카드 객체 시작점을 찾을 때까지 바깥으로 넓힌다
        for _ in range(6):
            if start < 0:
                break
            raw = _extract_json_object(html, start)
            if raw and m.start() < start + len(raw):
                # JS 리터럴이라 순수 JSON이 아니다: new Date(...)와 undefined를 정규화
                normalized = re.sub(r'new Date\((\"[^\"]*\")\)', r"\1", raw)
                normalized = re.sub(r":\s*undefined", ": null", normalized)
                try:
                    obj = json.loads(normalized)
                except Exception:
                    start = html.rfind("{", 0, start)
                    continue
                data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
                item = _card_to_item(data if isinstance(data, dict) else {})
                if item and item["rank"] not in seen_ranks:
                    seen_ranks.add(item["rank"])
                    items.append(item)
                break
            start = html.rfind("{", 0, start)

    if module_present and not items:
        return None

    items.sort(key=lambda x: x["rank"])
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
    if items is None:
        return {
            "status_code": 200,
            "ok": False,
            "items": [],
            "error": "parse invalid — 쇼핑 모듈은 있는데 유효 카드 0건 (JSON 구조 변경 의심)",
        }
    return {
        "status_code": 200,
        "ok": True,
        "items": items,
        "raw": None if items else "쇼핑 모듈 없음 (검색 결과 0건)",
    }


def _item_matches_product(item: dict, product: "TrackedProduct") -> bool:
    """검색 결과 한 건이 추적 상품과 일치하는지 판별한다.

    통합검색 파싱에서는 productId에 channelProductId(스마트스토어 상품ID)를
    넣으므로 1)에서 바로 일치한다. 2)는 값 형태가 바뀌었을 때를 위한 보루다.
    """
    pid = product.naver_product_id

    # 1) 상품 ID 직접 일치
    if item.get("productId") == pid:
        return True

    # 2) link URL에 상품 ID가 독립된 숫자 세그먼트로 포함 (부분 매칭 방지)
    # (?<!\d)pid(?!\d) → 앞뒤에 다른 숫자가 붙으면 매칭 안 됨
    # 예: pid="12345678"이 "123456789" 링크에 오탐되지 않음
    if pid and len(pid) >= 8:
        # 네이버 JSON은 필드 형태가 예고 없이 바뀐다(productUrl이 문자열에서
        # dict로 바뀐 적 있음). str이 아니면 매칭을 건너뛴다 — 여기서 예외가
        # 나면 수집 전체가 죽는다.
        link = item.get("link", "")
        if isinstance(link, str) and re.search(rf"(?<!\d){re.escape(pid)}(?!\d)", link):
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


# 수집이 겹쳐 돌면 두 실행이 각자 옛 naver_title을 읽고 같은 변경을 두 번 기록한다.
# (2026-08-05 03:22/03:27에 4개 상품이 동일한 이력을 중복 저장한 원인)
# 전체 수집은 키워드 244개 × 대기 1.5~3.5초라 10분 넘게 걸려서, 스케줄러 실행 중에
# 수동 수집 버튼을 누르면 쉽게 겹친다. Railway는 단일 인스턴스라 프로세스 락으로 충분하다.
collect_lock = threading.Lock()


def _norm(text: str | None) -> str:
    """비교용 정규화. 커머스 API와 검색 카드가 같은 제목을 공백 개수만 다르게
    내려주는 경우가 있어, 그대로 비교하면 없는 제목 변경이 기록된다."""
    return re.sub(r"\s+", " ", text or "").strip()


def _same_title_change_recorded(db: Session, product_id: int, old_title: str, new_title: str) -> bool:
    """직전 이력이 지금 쓰려는 것과 같은 전환인지 (공백 무시)."""
    last = (
        db.query(ProductTitleHistory)
        .filter(ProductTitleHistory.product_id == product_id)
        .order_by(ProductTitleHistory.changed_at.desc())
        .first()
    )
    if not last:
        return False
    return _norm(last.old_title) == _norm(old_title) and _norm(last.new_title) == _norm(new_title)


def record_title_and_tag_changes(
    db: Session,
    product: TrackedProduct,
    collected_at: datetime,
    scraped_title: str | None,
    scraped_tags: list[str] | None,
    commerce_info: dict | None,
    found_title: str | None,
) -> None:
    """제목·태그 변경을 감지해 이력에 남긴다. 전체 수집과 단건 수집이 함께 쓴다.

    양쪽에 복붙해두면 한쪽만 고쳐지고 다른 쪽이 조용히 어긋난다 — 디버그
    엔드포인트가 프록시를 안 타서 3일을 헤맸던 것과 같은 종류의 사고다.
    """
    commerce_title = commerce_info["name"] if commerce_info else None
    commerce_tags = commerce_info["tags"] if commerce_info else None

    # 제목 감지 우선순위:
    # 1) 페이지 크롤링 (네이버 봇차단 429시 None — 현재 전 상품 차단 상태)
    # 2) 커머스 API 채널 노출명 (429 우회 가능, 판매자 인증 필요)
    # 3) 검색 결과 제목 (인덱스 반영 수일 소요 — 최후 폴백, 25위 밖이면 없음)
    title_for_detection = scraped_title or commerce_title or found_title
    if title_for_detection:
        last_title = product.naver_title or product.product_name
        if _norm(title_for_detection) != _norm(last_title):
            has_prior = product.naver_title is not None or db.query(ProductRankHistory).filter(
                ProductRankHistory.product_id == product.id,
                ProductRankHistory.collected_at < collected_at,
            ).first() is not None
            if has_prior and not _same_title_change_recorded(db, product.id, last_title, title_for_detection):
                db.add(ProductTitleHistory(
                    product_id=product.id,
                    old_title=last_title,
                    new_title=title_for_detection,
                    changed_at=collected_at,
                ))
        product.naver_title = title_for_detection

    # 태그 감지 우선순위는 제목과 동일 (커머스 API가 유일한 실질 소스인 상태)
    tags_for_detection = scraped_tags if scraped_tags is not None else commerce_tags
    if tags_for_detection is not None:
        current_tags_str = ",".join(sorted(_norm(t) for t in tags_for_detection if _norm(t)))
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
        elif _norm(current_tags_str) != _norm(last_tags_str):
            db.add(ProductTagHistory(
                product_id=product.id,
                old_tags=last_tags_str,
                new_tags=current_tags_str,
                changed_at=collected_at,
            ))


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
                # 조회 실패 또는 구조 변경 — '순위 없음'으로 오기록하지 않고 건너뜀
                continue
            rank = None
            for item in items:
                if _item_matches_product(item, product):
                    # 네이버가 내려준 rank만 쓴다. 파서가 rank 없는 카드를 이미
                    # 걸러내므로 여기서 순서로 대체할 일은 없어야 한다.
                    rank = item["rank"]
                    # 처음 발견된 제목을 기록
                    if found_title is None:
                        found_title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                    break

            db.add(ProductRankHistory(
                product_id=product.id,
                keyword=pk.keyword,
                rank=rank,
                collected_at=collected_at,
                observation_status="OBSERVED" if rank is not None else "NOT_OBSERVED_WITHIN_LIMIT",
                max_observed_rank=SEARCH_DISPLAY,
            ))
            saved += 1

            # 경쟁사 스냅샷 저장 (키워드당 1회)
            if pk.keyword not in competitor_saved:
                for item in items[:20]:
                    try:
                        price = int(item.get("lprice", "0") or "0") or None
                    except (ValueError, TypeError):
                        price = None
                    db.add(KeywordCompetitorSnapshot(
                        keyword=pk.keyword,
                        collected_at=collected_at,
                        # 배열 위치가 아니라 네이버가 준 실제 순위를 저장한다.
                        # 위치를 쓰면 원본에 공백·누락이 생겼을 때 이력이 조용히 어긋난다.
                        search_rank=item["rank"],
                        naver_product_id=item.get("productId"),
                        title=re.sub(r"<[^>]+>", "", item.get("title", "")),
                        mall_name=item.get("mallName", ""),
                        price=price,
                    ))
                competitor_saved.add(pk.keyword)

        # 제목·태그 변경 감지용 커머스 API 조회.
        # 커머스 API 앱은 판매자 계정 단위라 스토어마다 자격증명이 다르다.
        # (예전엔 전 스토어가 하나의 자격증명을 써서 다른 스토어 상품은 전부 403이었다)
        commerce_info = fetch_product_commerce_info(
            product.naver_product_id,
            product.store.commerce_id_key if product.store else None,
            product.store.commerce_secret_key if product.store else None,
        )

        record_title_and_tag_changes(
            db, product, collected_at,
            scraped_title=scraped_title,
            scraped_tags=scraped_tags,
            commerce_info=commerce_info,
            found_title=found_title,
        )

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
        for item in items[:10]:
            price_str = item.get("lprice", "0") or "0"
            try:
                price = int(price_str)
            except ValueError:
                price = None

            db.add(
                KeywordTop10History(
                    watch_keyword_id=wk.id,
                    # 배열 위치가 아니라 네이버가 준 실제 순위
                    rank=item["rank"],
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
    """전체 수집 실행 (스케줄러에서 호출).

    이미 수집이 돌고 있으면 {"skipped": True}를 돌려주고 그냥 빠진다 —
    겹쳐 돌면 제목 변경 이력이 중복 기록된다(collect_lock 주석 참고).
    """
    if not collect_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "이미 수집이 진행 중입니다"}
    try:
        now = datetime.now(timezone.utc)
        product_count = collect_product_rankings(db, now)
        keyword_count = collect_keyword_top10(db, now)
        return {"products": product_count, "keywords": keyword_count, "collected_at": now.isoformat()}
    finally:
        collect_lock.release()
