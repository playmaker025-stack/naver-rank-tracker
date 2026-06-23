import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


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

            # XHR 응답 가로채기
            captured_items: list[dict] = []

            def _on_response(resp):
                try:
                    if "api/search" in resp.url and resp.status == 200:
                        data = resp.json()
                        lst = (
                            data.get("shoppingResult", {}).get("products", {}).get("list")
                            or data.get("products", {}).get("list")
                            or data.get("list")
                            or []
                        )
                        if lst:
                            captured_items.extend(lst)
                except Exception:
                    pass

            page.on("response", _on_response)

            url = f"https://search.shopping.naver.com/search/all?query={keyword}&sort=RANK"
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                raise ScraperError(f"navigation_failed: {str(e)[:120]}")

            # 봇 차단 감지
            if any(x in page.url for x in ["robot", "block", "captcha"]):
                raise ScraperError("bot_detected: 네이버 봇 차단 감지")

            page.wait_for_timeout(3000)

            # XHR로 데이터가 잡혔으면 바로 반환
            if captured_items:
                return _format_api_items(captured_items[:max_rank])

            # __NEXT_DATA__ 시도
            next_data = page.evaluate("""
                () => {
                    try {
                        const el = document.getElementById('__NEXT_DATA__');
                        return el ? JSON.parse(el.textContent) : null;
                    } catch(e) { return null; }
                }
            """)
            if next_data:
                lst = _extract_from_next_data(next_data)
                if lst:
                    return lst[:max_rank]

            # DOM 폴백: 상품 목록 대기
            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('[class*=\"product_item\"]').length > 0 || "
                    "document.querySelectorAll('[class*=\"basicList\"] li').length > 0",
                    timeout=12000,
                )
            except PlaywrightTimeout:
                raise ScraperError("timeout: 상품 목록 로딩 실패 - 네이버 쇼핑 HTML 구조가 변경되었을 수 있습니다")

            items = page.evaluate(f"""
                () => {{
                    const MAX = {max_rank};
                    const selectors = [
                        '[class*="product_item"]',
                        '[class*="basicList"] > li',
                        '[class*="productList"] > li',
                        'ul[class*="list_"] > li',
                    ];
                    let els = [];
                    for (const sel of selectors) {{
                        els = [...document.querySelectorAll(sel)];
                        if (els.length > 3) break;
                    }}
                    if (!els.length) return [];

                    return els.slice(0, MAX).map((el, i) => {{
                        const nameEl = (
                            el.querySelector('[class*="name_"] a') ||
                            el.querySelector('[class*="product_name"] a') ||
                            el.querySelector('a[class*="name"]') ||
                            el.querySelector('a[href*="smartstore.naver.com"]') ||
                            el.querySelector('a[href*="shopping.naver.com"]')
                        );
                        const mallEl = (
                            el.querySelector('[class*="mall_name"]') ||
                            el.querySelector('[class*="mallName"]') ||
                            el.querySelector('[class*="seller_name"]') ||
                            el.querySelector('[class*="store_name"]')
                        );
                        const title = nameEl?.textContent?.trim() || '';
                        const link = nameEl?.getAttribute('href') || '';
                        const mallName = mallEl?.textContent?.trim() || '';
                        if (!title && !link) return null;
                        return {{ rank: i + 1, title, mallName, link, productId: '' }};
                    }}).filter(Boolean);
                }}
            """)

            if not items:
                raise ScraperError("parse_error: 상품 파싱 실패 - 네이버 쇼핑 HTML/JS 구조 변경 가능성")

            return items

        finally:
            browser.close()


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


def _extract_from_next_data(data: dict) -> list[dict]:
    try:
        props = data.get("props", {}).get("pageProps", {})
        lst = (
            props.get("initialState", {}).get("products", {}).get("list")
            or props.get("searchResult", {}).get("products", {}).get("list")
            or props.get("products", {}).get("list")
            or []
        )
        if lst:
            return _format_api_items(lst)
    except Exception:
        pass
    return []
