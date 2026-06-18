"""
카카오 액세스 토큰 발급 헬퍼
- 나에게 보내기용 토큰 또는 추가 수신자 토큰을 발급합니다.
- 발급된 ACCESS_TOKEN과 REFRESH_TOKEN을 Railway 환경변수에 등록하세요.
"""
import webbrowser
import httpx
from urllib.parse import urlparse, parse_qs

REST_API_KEY = input("카카오 REST API 키 입력: ").strip()

# 카카오 개발자 콘솔에 이 주소를 Redirect URI로 등록해야 합니다
REDIRECT_URI = "https://example.com"

auth_url = (
    f"https://kauth.kakao.com/oauth/authorize"
    f"?client_id={REST_API_KEY}"
    f"&redirect_uri={REDIRECT_URI}"
    f"&response_type=code"
    f"&scope=talk_message"
)

print("\n[1단계] 브라우저가 열립니다. 카카오 계정으로 로그인하세요.")
print("       로그인 후 주소창의 URL 전체를 복사해 붙여넣기 하세요.\n")
webbrowser.open(auth_url)

redirect_url = input("리다이렉트된 URL 붙여넣기: ").strip()

parsed = urlparse(redirect_url)
code = parse_qs(parsed.query).get("code", [None])[0]
if not code:
    print("URL에서 code를 찾을 수 없습니다. URL을 다시 확인해주세요.")
    exit(1)

print("\n[2단계] 토큰 발급 중...")
resp = httpx.post(
    "https://kauth.kakao.com/oauth/token",
    data={
        "grant_type": "authorization_code",
        "client_id": REST_API_KEY,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    },
    timeout=10,
)
data = resp.json()

if "access_token" not in data:
    print(f"오류: {data}")
    exit(1)

print("\n✅ 토큰 발급 완료! 아래 값을 Railway 환경변수에 등록하세요.\n")
print(f"KAKAO_REST_API_KEY={REST_API_KEY}")
print(f"KAKAO_ACCESS_TOKEN_ME (또는 EXTRA)={data['access_token']}")
print(f"KAKAO_REFRESH_TOKEN_ME (또는 EXTRA)={data['refresh_token']}")
print(f"\n* 액세스 토큰 만료: 6시간 후")
print(f"* 리프레시 토큰 만료: {data.get('refresh_token_expires_in', '?')}초 후 (약 30일)")
