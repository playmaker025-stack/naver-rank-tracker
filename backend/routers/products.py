from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.collector import fetch_product_info
from backend.database import get_db
from backend.keyword_extractor import extract_keywords_from_title
from backend.models import ProductKeyword, Store, TrackedProduct

router = APIRouter(prefix="/products", tags=["products"])


class ProductAdd(BaseModel):
    store_id: int
    product_url: str
    product_name: str = ""  # 빈 문자열이면 API로 자동 조회


class ProductOut(BaseModel):
    id: int
    store_id: int
    store_name: str
    naver_product_id: str
    product_name: str
    product_url: str
    is_active: bool
    keywords: list[str]

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ProductOut])
def list_products(store_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(TrackedProduct)
    if store_id:
        q = q.filter(TrackedProduct.store_id == store_id)
    products = q.all()
    return [
        ProductOut(
            id=p.id,
            store_id=p.store_id,
            store_name=p.store.name,
            naver_product_id=p.naver_product_id,
            product_name=p.product_name,
            product_url=p.product_url,
            is_active=p.is_active,
            keywords=[pk.keyword for pk in p.keywords],
        )
        for p in products
    ]


@router.post("", response_model=ProductOut, status_code=201)
def add_product(body: ProductAdd, db: Session = Depends(get_db)):
    store = db.get(Store, body.store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    info = fetch_product_info(body.product_url)
    if not info:
        raise HTTPException(status_code=400, detail="상품 URL에서 productId를 추출할 수 없습니다.")

    product_name = body.product_name or info.get("product_name") or ""
    naver_product_id = info["naver_product_id"]

    existing = (
        db.query(TrackedProduct)
        .filter(
            TrackedProduct.store_id == body.store_id,
            TrackedProduct.naver_product_id == naver_product_id,
        )
        .first()
    )
    if existing:
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return ProductOut(
            id=existing.id,
            store_id=existing.store_id,
            store_name=store.name,
            naver_product_id=existing.naver_product_id,
            product_name=existing.product_name,
            product_url=existing.product_url,
            is_active=existing.is_active,
            keywords=[pk.keyword for pk in existing.keywords],
        )

    product = TrackedProduct(
        store_id=body.store_id,
        naver_product_id=naver_product_id,
        product_name=product_name,
        product_url=info.get("product_url", body.product_url),
    )
    db.add(product)
    db.flush()

    keywords = extract_keywords_from_title(product_name)
    for kw in keywords:
        db.add(ProductKeyword(product_id=product.id, keyword=kw))

    db.commit()
    db.refresh(product)
    return ProductOut(
        id=product.id,
        store_id=product.store_id,
        store_name=store.name,
        naver_product_id=product.naver_product_id,
        product_name=product.product_name,
        product_url=product.product_url,
        is_active=product.is_active,
        keywords=[pk.keyword for pk in product.keywords],
    )


@router.patch("/{product_id}/toggle", response_model=ProductOut)
def toggle_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(TrackedProduct, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_active = not product.is_active
    db.commit()
    db.refresh(product)
    return ProductOut(
        id=product.id,
        store_id=product.store_id,
        store_name=product.store.name,
        naver_product_id=product.naver_product_id,
        product_name=product.product_name,
        product_url=product.product_url,
        is_active=product.is_active,
        keywords=[pk.keyword for pk in product.keywords],
    )


@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(TrackedProduct, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(product)
    db.commit()


@router.put("/{product_id}/keywords")
def update_product_keywords(product_id: int, keywords: list[str], db: Session = Depends(get_db)):
    product = db.get(TrackedProduct, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    for pk in product.keywords:
        db.delete(pk)
    db.flush()

    for kw in keywords:
        db.add(ProductKeyword(product_id=product_id, keyword=kw.strip()))

    db.commit()
    return {"keywords": keywords}
