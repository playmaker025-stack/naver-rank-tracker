from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import WatchKeyword

router = APIRouter(prefix="/keywords", tags=["keywords"])


class KeywordCreate(BaseModel):
    keyword: str


class KeywordOut(BaseModel):
    id: int
    keyword: str
    is_active: bool

    model_config = {"from_attributes": True}


@router.get("", response_model=list[KeywordOut])
def list_keywords(db: Session = Depends(get_db)):
    return db.query(WatchKeyword).all()


@router.post("", response_model=KeywordOut, status_code=201)
def create_keyword(body: KeywordCreate, db: Session = Depends(get_db)):
    existing = db.query(WatchKeyword).filter(WatchKeyword.keyword == body.keyword).first()
    if existing:
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return existing
    kw = WatchKeyword(keyword=body.keyword)
    db.add(kw)
    db.commit()
    db.refresh(kw)
    return kw


@router.patch("/{keyword_id}/toggle", response_model=KeywordOut)
def toggle_keyword(keyword_id: int, db: Session = Depends(get_db)):
    kw = db.get(WatchKeyword, keyword_id)
    if not kw:
        raise HTTPException(status_code=404, detail="Keyword not found")
    kw.is_active = not kw.is_active
    db.commit()
    db.refresh(kw)
    return kw


@router.delete("/{keyword_id}", status_code=204)
def delete_keyword(keyword_id: int, db: Session = Depends(get_db)):
    kw = db.get(WatchKeyword, keyword_id)
    if not kw:
        raise HTTPException(status_code=404, detail="Keyword not found")
    db.delete(kw)
    db.commit()
