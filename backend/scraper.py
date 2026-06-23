import re
from playwright.sync_api import sync_playwright


class ScraperError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def scrape_naver_shopping(keyword: str, max_rank: int = 100) -> list[dict]:
    """Playwright로 네이버 쇼핑 랭킹순 검색 결과를 스크래핑한다."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="ko-KR",
                extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
            )
            page = context.new_page()

            # 모든 JSON 응답 가로채기
            captured: list[dict] = []

            def _on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    if resp.status == 200 and "json" in ct:
                        data = resp.json()
                        lst = (
                            data.get("shoppingResult", {}).get("products", {}).get("list")
                            or data.get("products", {}).get("list")
                            or data.get("list")
                            or []
                        )
                        if lst and len(lst) > 5:
                            captured.extend(lst)
                except Exception:
                    pass

            page.on("response", _on_response)

            url = f"https://search.shopping.naver.com/search/all?query={keyword}&sort=RANK"
            try:
                page.goto(url, timeout=40000, wait_until="load")
            except Exception as e:
                raise ScraperError(f"navigation_failed: {str(e)[:120]}")

            # 봇 차단 감지
            if any(x in page.url for x in ["robot", "block", "captcha"]):
                raise ScraperError("bot_detected: 네이버 봇 차단 감지")

            # XHR 완료 대기
            page.wait_for_timeout(5000)

            # 1순위: XHR로 잡은 데이터
            if captured:
                return _format_api_items(captured[:max_rank])

            # 2순위: __NEXT_DATA__
            next_items = _try_next_data(page, max_rank)
            if next_items:
                return next_items

            # 3순위: DOM 파싱 (여러 선택자 시도)
            dom_items = _try_dom(page, max_rank)
            if dom_items:
                return dom_items

            raise ScraperError("parse_error: 상품 파싱 실패 - 네이버 쇼핑 구조 변경 가능성")

        finally:
            browser.close()


def _try_next_data(page, max_rank: int) -> list[dict]:
    try:
        data = page.evaluate("""
            () => {
                try {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? JSON.parse(el.textContent) : null;
                } catch(e) { return null; }
            }
        """)
        if not data:
            return []
        props = data.get("props", {}).get("pageProps", {})
        lst = (
            props.get("initialState", {}).get("products", {}).get("list")
            or props.get("searchResult", {}).get("products", {}).get("list")
            or props.get("products", {}).get("list")
            or []
        )
        if lst:
            return _format_api_items(lst[:max_rank])
    except Exception:
        pass
    return []


def _try_dom(page, max_rank: int) -> list[dict]:
    try:
        items = page.evaluate(f"""
            () => {{
                const MAX = {max_rank};
                // 다양한 선택자 시도
                const candidates = [
                    '[class*="product_item"]',
                    '[class*="basicList"] li',
                    '[class*="productList"] li',
                    '[class*="goods_list"] li',
                    'ul[class*="list"] > li',
                    'div[class*="item_"]',
                    'li[data-id]',
                ];
                let els = [];
                for (const sel of candidates) {{
                    const found = [...document.querySelectorAll(sel)];
                    if (found.length > 3) {{ els = found; break; }}
                }}
                if (!els.length) return [];

                return els.slice(0, MAX).map((el, i) => {{
                    // 상품명/링크
                    const nameEl = (
                        el.querySelector('a[class*="name"]') ||
                        el.querySelector('[class*="name_"] a') ||
                        el.querySelector('[class*="title"] a') ||
                        el.querySelector('a[href*="smartstore.naver.com"]') ||
                        el.querySelector('a[href*="shopping.naver.com"]')
                    );
                    // 쇼핑몰명
                    const mallEl = (
                        el.querySelector('[class*="mall_name"]') ||
                        el.querySelector('[class*="mallName"]') ||
                        el.querySelector('[class*="seller_"]') ||
                        el.querySelector('[class*="store_name"]')
                    );
                    const title = nameEl?.textContent?.trim() || '';
                    const link = nameEl?.getAttribute('href') || '';
                    if (!title && !link) return null;
                    return {{
                        rank: i + 1,
                        title,
                        mallName: mallEl?.textContent?.trim() || '',
                        link,
                        productId: '',
                    }};
                }}).filter(Boolean);
            }}
        """)
        return items or []
    except Exception:
        return []


def _format_api_items(items: list[dict]) -> list[dict]:
    return [
        {
            "rank": i + 1,
            "title": re.sub(r"<[^>]+>", "", item.get("productName", item.get("name", ""))),
            "mallName": item.get("mallName", item.get("storeName", "")),
            "link": item.get("mallProductUrl", item.get("productUrl", item.get("link", ""))),
            "productId": str(item.get("productId", item.get("id", ""))),
        }
        for i, item in enumerate(items)
    ]
