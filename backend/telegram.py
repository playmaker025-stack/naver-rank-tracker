import os
import httpx

TELEGRAM_API = "https://api.telegram.org"


def _send(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        resp = httpx.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_rank_alert(alerts: list[dict]) -> None:
    if not alerts:
        return
    surges = [a for a in alerts if a.get("prev") is None or (a.get("curr") or 999) < (a.get("prev") or 999)]
    drops  = [a for a in alerts if a.get("prev") is not None and (a.get("curr") or 999) > (a.get("prev") or 999)]
    header = "🚀 급상승" if surges and not drops else ("📉 급하락" if drops and not surges else "⚡ 순위 급변동")
    lines = [f"<b>[네이버 랭킹] {header}</b>"]
    for a in alerts[:10]:
        prev = a.get("prev")
        curr = a.get("curr")
        if prev is None:
            arrow, change = "🆕 신규진입", f"→ {curr}위"
        elif curr < prev:
            arrow, change = f"🚀 ▲{prev - curr}계단", f"{prev}위 → {curr}위"
        else:
            arrow, change = f"📉 ▼{curr - prev}계단", f"{prev}위 → {curr}위"
        lines.append(f"{arrow} {a['product'][:20]} / {a['keyword']}: {change}")
    _send("\n".join(lines))


def send_collection_summary(result: dict) -> None:
    from datetime import datetime, timezone, timedelta
    kst_now = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m/%d %H:%M")
    msg = (
        f"<b>[랭킹 수집 완료]</b> {kst_now} KST\n"
        f"상품 키워드 수집: {result.get('products', 0)}건\n"
        f"키워드 TOP10 수집: {result.get('keywords', 0)}건"
    )
    _send(msg)


def send_scraper_error(keyword: str, reason: str) -> None:
    msg = (
        f"<b>[랭킹 수집 오류] 스크래퍼 실패</b>\n"
        f"키워드: {keyword}\n"
        f"사유: {reason[:150]}\n"
        f"→ 비로그인 API로 대체 수집합니다"
    )
    _send(msg)
