import json
import os
from typing import Optional
import httpx

KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"


def _refresh_access_token(refresh_token: str) -> Optional[str]:
    """리프레시 토큰으로 새 액세스 토큰을 발급한다."""
    rest_api_key = os.environ.get("KAKAO_REST_API_KEY", "")
    if not rest_api_key or not refresh_token:
        return None
    try:
        resp = httpx.post(
            KAKAO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": rest_api_key,
                "refresh_token": refresh_token,
            },
            timeout=10,
        )
        data = resp.json()
        return data.get("access_token")
    except Exception:
        return None


def _build_text_template(text: str, web_url: Optional[str] = None) -> str:
    web_url = web_url or os.environ.get("APP_URL", "http://localhost:8000")
    return json.dumps(
        {
            "object_type": "text",
            "text": text[:200],
            "link": {"web_url": web_url, "mobile_web_url": web_url},
        },
        ensure_ascii=False,
    )


def _send_memo(access_token: str, refresh_token: str, message: str) -> bool:
    """메모 전송. 토큰 만료 시 자동 갱신 후 재시도."""
    template = _build_text_template(message)

    def _post(token: str) -> int:
        resp = httpx.post(
            KAKAO_MEMO_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"template_object": template},
            timeout=10,
        )
        return resp.status_code

    try:
        status = _post(access_token)
        if status == 401 and refresh_token:
            new_token = _refresh_access_token(refresh_token)
            if new_token:
                status = _post(new_token)
        return status == 200
    except Exception:
        return False


def send_to_me(message: str) -> bool:
    """나에게 보내기."""
    access = os.environ.get("KAKAO_ACCESS_TOKEN_ME", "")
    refresh = os.environ.get("KAKAO_REFRESH_TOKEN_ME", "")
    if not access:
        return False
    return _send_memo(access, refresh, message)


def send_to_extra(message: str) -> bool:
    """추가 수신자에게 보내기."""
    access = os.environ.get("KAKAO_ACCESS_TOKEN_EXTRA", "")
    refresh = os.environ.get("KAKAO_REFRESH_TOKEN_EXTRA", "")
    if not access:
        return False
    return _send_memo(access, refresh, message)


def send_rank_alert(alerts: list[dict]) -> None:
    """랭킹 변동 알림 전송."""
    if not alerts:
        return

    surges = [a for a in alerts if a.get("prev") is None or (a.get("curr") or 999) < (a.get("prev") or 999)]
    drops  = [a for a in alerts if a.get("prev") is not None and (a.get("curr") or 999) > (a.get("prev") or 999)]
    header = "🚀 급상승" if surges and not drops else ("📉 급하락" if drops and not surges else "⚡ 순위 급변동")
    lines = [f"[네이버 랭킹] {header}"]
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

    message = "\n".join(lines)
    send_to_me(message)
    send_to_extra(message)


def send_scraper_error(keyword: str, reason: str) -> None:
    """스크래퍼 오류 알림 전송."""
    msg = (
        f"[랭킹 수집 오류] 스크래퍼 실패\n"
        f"키워드: {keyword}\n"
        f"사유: {reason[:120]}\n"
        f"→ 비로그인 API로 대체 수집합니다"
    )
    send_to_me(msg)
    send_to_extra(msg)


def send_collection_summary(result: dict) -> None:
    """수집 완료 요약 알림."""
    from datetime import datetime, timezone, timedelta
    kst_now = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m/%d %H:%M")
    msg = (
        f"[랭킹 수집 완료] {kst_now} KST\n"
        f"상품 키워드 수집: {result.get('products', 0)}건\n"
        f"키워드 TOP10 수집: {result.get('keywords', 0)}건"
    )
    send_to_me(msg)
    send_to_extra(msg)
