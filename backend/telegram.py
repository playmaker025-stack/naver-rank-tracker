import os
import httpx

TELEGRAM_API = "https://api.telegram.org"


def _send(message: str, chat_id: str | None = None, bot_token: str | None = None) -> bool:
    token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    target = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not target:
        return False
    try:
        resp = httpx.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": target, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_rank_alert(alerts: list[dict], chat_id: str | None = None, bot_token: str | None = None) -> None:
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
    _send("\n".join(lines), chat_id=chat_id, bot_token=bot_token)


def send_collection_summary(
    result: dict,
    changes: list[dict] | None = None,
    chat_id: str | None = None,
    bot_token: str | None = None,
) -> None:
    """변동 목록(2위 이상)을 포함한 수집 완료 메시지 전송.
    changes 항목: {"product": str, "keyword": str, "prev": int, "curr": int, "diff": int}
    diff = prev - curr (양수=상승, 음수=하락)
    """
    from datetime import datetime, timezone, timedelta
    kst_now = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m/%d %H:%M")
    lines = [f"<b>[랭킹 수집]</b> {kst_now} KST"]

    if changes:
        surges = sorted([c for c in changes if c["diff"] > 0], key=lambda x: -x["diff"])
        drops  = sorted([c for c in changes if c["diff"] < 0], key=lambda x:  x["diff"])
        if surges:
            lines.append("\n🚀 <b>순위 상승</b>")
            for c in surges:
                lines.append(f"  ▲{c['diff']} {c['product'][:16]} / {c['keyword']}: {c['prev']}위→{c['curr']}위")
        if drops:
            lines.append("\n📉 <b>순위 하락</b>")
            for c in drops:
                lines.append(f"  ▼{abs(c['diff'])} {c['product'][:16]} / {c['keyword']}: {c['prev']}위→{c['curr']}위")
    else:
        lines.append("2위 이상 변동 없음")

    lines.append(f"\n총 {result.get('products', 0)}개 키워드 수집")
    _send("\n".join(lines), chat_id=chat_id, bot_token=bot_token)


def send_scraper_error(keyword: str, reason: str) -> None:
    msg = (
        f"<b>[랭킹 수집 오류] 스크래퍼 실패</b>\n"
        f"키워드: {keyword}\n"
        f"사유: {reason[:150]}\n"
        f"→ 비로그인 API로 대체 수집합니다"
    )
    _send(msg)
