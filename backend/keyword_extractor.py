import re


_STOPWORDS = {
    "무료", "배송", "당일", "출고", "할인", "특가", "정품", "국내", "공식",
    "판매", "상품", "신상", "최신", "최저", "이상", "이하", "추천", "인기",
    "고품질", "대용량", "소용량", "사은품", "증정", "세트", "묶음",
}


def extract_keywords_from_title(title: str) -> list[str]:
    """상품명에서 추적에 쓸 키워드 조합을 자동 추출한다."""
    # HTML 태그 제거
    clean = re.sub(r"<[^>]+>", "", title)
    # 특수문자 → 공백 (한글/영문/숫자만 유지)
    clean = re.sub(r"[^\w\s가-힣]", " ", clean)

    words = [w for w in clean.split() if len(w) >= 2 and w not in _STOPWORDS]

    keywords: list[str] = []

    # 단어 단독
    for w in words:
        if w not in keywords:
            keywords.append(w)

    # 인접 2-gram
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i + 1]}"
        if bigram not in keywords:
            keywords.append(bigram)

    # 인접 3-gram (핵심 구문 포착)
    for i in range(len(words) - 2):
        trigram = f"{words[i]} {words[i + 1]} {words[i + 2]}"
        if trigram not in keywords:
            keywords.append(trigram)

    return keywords[:30]
