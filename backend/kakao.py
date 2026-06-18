import json
import os
import httpx

KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_FRIEND_MSG_URL = "https://kapi.kakao.com/v1/api/talk/friends/message/default/send"


def _build_text_template(text: str, web_url: str | None = None) -> str:
    web_url = web_url or os.environ.get("APP_URL", "http://localhost:8000")
    return json.dumps(
        {
            "object_type": "text",
            "text": text[:200],  # 카카오 최대 200자
            "link": {"web_url": web_url, "mobile_web_url": web_url},
        },
        ensure_ascii=False,
    )


def send_to_me(message: str) -> bool:
    """나에게 보내기."""
    token = os.environ.get("KAKAO_ACCESS_TOKEN_ME", "")
    if not token:
        return False
    try:
        resp = httpx.post(
            KAKAO_MEMO_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"template_object": _build_text_template(message)},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_to_extra(message: str) -> bool:
    """추가 수신자(액세스 토큰 방식)에게 보내기."""
    token = os.environ.get("KAKAO_ACCESS_TOKEN_EXTRA", "")
    if not token:
        return False
    try:
        resp = httpx.post(
            KAKAO_MEMO_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"template_object": _build_text_template(message)},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_rank_alert(alerts: list[dict]) -> None:
    """랭킹 변동 알림 전송. alerts = [{"product": str, "keyword": str, "prev": int|None, "curr": int|None}]"""
    if not alerts:
        return

    lines = ["[네이버 랭킹 알림] 순위 변동"]
    for a in alerts[:10]:  # 최대 10개
        prev = a.get("prev")
        curr = a.get("curr")
        if prev is None:
            arrow = "🆕 신규진입"
            change = f"→ {curr}위"
        elif curr is None:
            arrow = "❌ 이탈"
            change = f"{prev}위 → 100위 밖"
        elif curr < prev:
            arrow = f"▲ {prev - curr}"
            change = f"{prev}위 → {curr}위"
        else:
            arrow = f"▼ {curr - prev}"
            change = f"{prev}위 → {curr}위"

        lines.append(f"{arrow} {a['product'][:20]} / {a['keyword']}: {change}")

    message = "\n".join(lines)
    send_to_me(message)
    send_to_extra(message)


def send_collection_summary(result: dict) -> None:
    """수집 완료 요약 알림."""
    from datetime import datetime, timezone

    kst_now = datetime.now(timezone.utc).strftime("%m/%d %H:%M")
    msg = (
        f"[랭킹 수집 완료] {kst_now}\n"
        f"상품 키워드 수집: {result.get('products', 0)}건\n"
        f"키워드 TOP10 수집: {result.get('keywords', 0)}건"
    )
    send_to_me(msg)
    send_to_extra(msg)
