"""네이버 커머스 API 클라이언트 — 판매자 상품 태그 조회."""
import base64
import os
import time

import bcrypt
import httpx

_COMMERCE_BASE = "https://api.commerce.naver.com/external"

# 기본 자격증명 env 변수명 (스토어 지정이 없을 때 사용)
DEFAULT_ID_KEY = "NAVER_COMMERCE_CLIENT_ID"
DEFAULT_SECRET_KEY = "NAVER_COMMERCE_CLIENT_SECRET"

# 토큰 캐시 (1시간 유효). 스토어마다 자격증명이 다르므로 client_id별로 따로 캐싱한다.
# 예전엔 캐시가 하나뿐이라 여러 스토어를 섞어 쓰면 남의 토큰을 재사용하게 된다.
_token_cache: dict[str, dict] = {}


def resolve_credentials(id_key: str | None, secret_key: str | None) -> tuple[str | None, str | None]:
    """스토어에 지정된 env 변수명으로 자격증명을 읽는다. 지정이 없으면 기본값."""
    client_id = os.environ.get(id_key or DEFAULT_ID_KEY)
    client_secret = os.environ.get(secret_key or DEFAULT_SECRET_KEY)
    return client_id, client_secret


def _commerce_client() -> httpx.Client:
    """FIXIE_URL 환경변수가 있으면 고정 IP 프록시를 통해 요청."""
    proxy = os.environ.get("FIXIE_URL")
    return httpx.Client(proxy=proxy, timeout=10) if proxy else httpx.Client(timeout=10)


def _get_access_token(client_id: str | None = None, client_secret: str | None = None) -> str | None:
    if client_id is None or client_secret is None:
        client_id, client_secret = resolve_credentials(None, None)
    if not client_id or not client_secret:
        return None

    now = time.time()
    cached = _token_cache.get(client_id)
    if cached and cached["token"] and now < cached["expires_at"] - 60:
        return cached["token"]

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
            _token_cache[client_id] = {"token": token, "expires_at": now + expires_in}
        return token
    except Exception:
        return None


def _get(channel_product_no: str, token: str) -> httpx.Response:
    with _commerce_client() as client:
        return client.get(
            f"{_COMMERCE_BASE}/v2/products/channel-products/{channel_product_no}",
            headers={"Authorization": f"Bearer {token}"},
        )


def _pick_channel_name(data: dict) -> tuple[str | None, str | None]:
    """채널 노출명과 그 출처 경로를 찾는다.

    채널 상품 블록 이름이 응답마다 달라서(channelProduct /
    smartstoreChannelProduct) 후보를 순서대로 본다. 2026-08-25에
    `channelProduct.channelProductDisplayName` 하나만 보다가 전부 None을 받고
    원상품명으로 조용히 폴백하고 있던 걸 발견해서 후보를 넓혔다.
    """
    candidates = [
        ("smartstoreChannelProduct", "channelProductName"),
        ("smartstoreChannelProduct", "channelProductDisplayName"),
        ("channelProduct", "channelProductDisplayName"),
        ("channelProduct", "channelProductName"),
    ]
    for block, key in candidates:
        val = (data.get(block) or {}).get(key)
        if isinstance(val, str) and val.strip():
            return val.strip(), f"{block}.{key}"
    return None, None


def fetch_product_commerce_debug(
    channel_product_no: str,
    id_key: str | None = None,
    secret_key: str | None = None,
) -> dict | None:
    """응답 구조 확인용 — 최상위 키와 '이름'류 필드만 추려서 반환.

    전체 payload를 그대로 노출하면 주문·판매자 정보까지 딸려 나오므로
    키 이름에 name이 들어간 문자열 필드만 경로와 함께 뽑는다.
    """
    client_id, client_secret = resolve_credentials(id_key, secret_key)
    token = _get_access_token(client_id, client_secret)
    if not token:
        return None
    try:
        resp = _get(channel_product_no, token)
        if resp.status_code != 200:
            return {"http_status": resp.status_code}
        data = resp.json()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    found: dict[str, str] = {}

    def walk(node, path="", depth=0):
        if depth > 3 or len(found) >= 40:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, str) and "name" in k.lower() and v.strip():
                    found[p] = v
                elif isinstance(v, (dict, list)):
                    walk(v, p, depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node[:3]):
                walk(v, f"{path}[{i}]", depth + 1)

    walk(data)
    channel_name, channel_path = _pick_channel_name(data)
    return {
        "top_level_keys": list(data.keys()),
        "name_fields": found,
        "picked_channel_name": channel_name,
        "picked_from": channel_path,
    }


def fetch_product_commerce_info(
    channel_product_no: str,
    id_key: str | None = None,
    secret_key: str | None = None,
) -> dict | None:
    """커머스 API로 상품명·검색태그를 한 번에 가져온다.
    반환: {"name", "channel_name", "origin_name", "tags"} 또는 None(API 실패 시)

    id_key/secret_key는 해당 상품이 속한 스토어의 자격증명 env 변수명이다.
    커머스 API 앱은 판매자 계정 단위라, 다른 스토어 상품을 조회하면 403이 난다.
    """
    client_id, client_secret = resolve_credentials(id_key, secret_key)
    token = _get_access_token(client_id, client_secret)
    if not token:
        return None
    try:
        resp = _get(channel_product_no, token)
        if resp.status_code == 401:
            _token_cache.pop(client_id, None)
            token = _get_access_token(client_id, client_secret)
            if not token:
                return None
            resp = _get(channel_product_no, token)
        if resp.status_code != 200:
            return None
        data = resp.json()
        # 네이버 쇼핑 검색·스토어 페이지에 실제로 노출되는 건 채널 노출명이다.
        # 원상품명(originProduct.name)을 우선하면 채널 노출명만 바뀐 수정을
        # 영영 못 잡는다 — 검색 결과에는 새 제목이 뜨는데 이력엔 안 남는다.
        origin_name = (data.get("originProduct") or {}).get("name")
        channel_name, channel_path = _pick_channel_name(data)
        name = channel_name or origin_name
        seller_tags = (
            data.get("originProduct", {})
            .get("detailAttribute", {})
            .get("seoInfo", {})
            .get("sellerTags", [])
        )
        tags = [t.get("text", "").strip() for t in seller_tags if t.get("text")]
        return {
            "name": name,
            "channel_name": channel_name,
            "channel_name_path": channel_path,
            "origin_name": origin_name,
            "tags": tags,
        }
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
