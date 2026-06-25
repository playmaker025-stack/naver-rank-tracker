"""네이버 커머스 API 클라이언트 — 판매자 상품 태그 조회."""
import base64
import os
import time
from datetime import datetime, timezone

import bcrypt
import httpx

_COMMERCE_BASE = "https://api.commerce.naver.com/external"

# 토큰 캐시 (1시간 유효)
_token_cache: dict = {"token": None, "expires_at": 0.0}


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
        resp = httpx.post(
            f"{_COMMERCE_BASE}/v1/oauth2/token",
            params={
                "client_id": client_id,
                "timestamp": timestamp,
                "client_secret_sign": client_secret_sign,
                "grant_type": "client_credentials",
                "type": "SELF",
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=10,
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


def fetch_product_tags(channel_product_no: str) -> list[str] | None:
    """커머스 API로 상품의 검색태그 목록을 가져온다.
    반환: ["태그1", "태그2", ...] 또는 None(API 실패 시)
    """
    token = _get_access_token()
    if not token:
        return None
    try:
        resp = httpx.get(
            f"{_COMMERCE_BASE}/v2/products/channel-products/{channel_product_no}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 401:
            # 토큰 만료 → 캐시 무효화 후 재시도
            _token_cache["token"] = None
            token = _get_access_token()
            if not token:
                return None
            resp = httpx.get(
                f"{_COMMERCE_BASE}/v2/products/channel-products/{channel_product_no}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        seller_tags = (
            data.get("originProduct", {})
            .get("detailAttribute", {})
            .get("seoInfo", {})
            .get("sellerTags", [])
        )
        return [t.get("text", "").strip() for t in seller_tags if t.get("text")]
    except Exception:
        return None


def check_ip_registered() -> bool:
    """현재 서버 IP가 커머스 API에 등록되어 있는지 토큰 발급으로 확인."""
    return _get_access_token() is not None
