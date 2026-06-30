"""네이버 커머스 API 클라이언트 — 판매자 상품 태그 조회."""
import base64
import os
import time

import bcrypt
import httpx

_COMMERCE_BASE = "https://api.commerce.naver.com/external"

# 토큰 캐시 (1시간 유효)
_token_cache: dict = {"token": None, "expires_at": 0.0}


def _commerce_client() -> httpx.Client:
    """FIXIE_URL 환경변수가 있으면 고정 IP 프록시를 통해 요청."""
    proxy = os.environ.get("FIXIE_URL")
    return httpx.Client(proxy=proxy, timeout=10) if proxy else httpx.Client(timeout=10)


def _get_access_token() -> str | None:
    client_id = os.environ.get("NAVER_COMMERCE_CLIENT_ID")
    client_secret = os.environ.get("NAVER_COMMERCE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    timestamp = str(int(now * 1000))
    password = f"{client_id}_{timestamp}".encode("utf-8")
    hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
    client_secret_sign = base64.b64encode(hashed).decode("utf-8")

    try:
        with _commerce_client() as client:
            resp = client.post(
                f"{_COMMERCE_BASE}/v1/oauth2/token",
                params={
                    "client_id": client_id,
                    "timestamp": timestamp,
                    "client_secret_sign": client_secret_sign,
                    "grant_type": "client_credentials",
                    "type": "SELF",
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        data = resp.json()
        token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        if token:
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + expires_in
        return token
    except Exception:
        return None


def _get(channel_product_no: str, token: str) -> httpx.Response:
    with _commerce_client() as client:
        return client.get(
            f"{_COMMERCE_BASE}/v2/products/channel-products/{channel_product_no}",
            headers={"Authorization": f"Bearer {token}"},
        )


def fetch_product_commerce_info(channel_product_no: str) -> dict | None:
    """커머스 API로 상품명·검색태그를 한 번에 가져온다.
    반환: {"name": str | None, "tags": list[str]} 또는 None(API 실패 시)
    """
    token = _get_access_token()
    if not token:
        return None
    try:
        resp = _get(channel_product_no, token)
        if resp.status_code == 401:
            _token_cache["token"] = None
            token = _get_access_token()
            if not token:
                return None
            resp = _get(channel_product_no, token)
        if resp.status_code != 200:
            return None
        data = resp.json()
        name = (
            data.get("originProduct", {}).get("name")
            or data.get("channelProduct", {}).get("channelProductDisplayName")
        )
        seller_tags = (
            data.get("originProduct", {})
            .get("detailAttribute", {})
            .get("seoInfo", {})
            .get("sellerTags", [])
        )
        tags = [t.get("text", "").strip() for t in seller_tags if t.get("text")]
        return {"name": name, "tags": tags}
    except Exception:
        return None


def fetch_product_tags(channel_product_no: str) -> list[str] | None:
    """커머스 API로 상품의 검색태그 목록을 가져온다."""
    info = fetch_product_commerce_info(channel_product_no)
    return info["tags"] if info else None


def fetch_product_name(channel_product_no: str) -> str | None:
    """커머스 API로 상품명을 가져온다."""
    info = fetch_product_commerce_info(channel_product_no)
    return info["name"] if info else None


def check_commerce_ip() -> dict:
    """Commerce API IP 허용 여부 + 현재 서버 IP 반환."""
    import httpx as _httpx
    try:
        r = _httpx.get("https://api.ipify.org?format=json", timeout=5)
        current_ip = r.json().get("ip", "unknown")
    except Exception:
        current_ip = "unknown"

    client_id = os.environ.get("NAVER_COMMERCE_CLIENT_ID")
    client_secret = os.environ.get("NAVER_COMMERCE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return {"ok": False, "reason": "env_missing", "ip": current_ip}

    # 토큰 캐시 무시하고 직접 발급 시도
    now = time.time()
    timestamp = str(int(now * 1000))
    password = f"{client_id}_{timestamp}".encode("utf-8")
    hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
    client_secret_sign = base64.b64encode(hashed).decode("utf-8")
    try:
        with _commerce_client() as client:
            resp = client.post(
                f"{_COMMERCE_BASE}/v1/oauth2/token",
                params={
                    "client_id": client_id,
                    "timestamp": timestamp,
                    "client_secret_sign": client_secret_sign,
                    "grant_type": "client_credentials",
                    "type": "SELF",
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        body = resp.json()
        if body.get("access_token"):
            return {"ok": True, "ip": current_ip}
        reason = body.get("message") or body.get("error") or str(body)
        ip_blocked = "허용되지 않은 IP" in reason or "IP" in reason
        return {"ok": False, "reason": reason, "ip": current_ip, "ip_blocked": ip_blocked}
    except Exception as e:
        return {"ok": False, "reason": str(e), "ip": current_ip}
