from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Store

router = APIRouter(prefix="/stores", tags=["stores"])


class StoreCreate(BaseModel):
    name: str
    mall_name: str
    store_url: str


class StoreOut(BaseModel):
    id: int
    name: str
    mall_name: str
    store_url: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[StoreOut])
def list_stores(db: Session = Depends(get_db)):
    return db.query(Store).all()


@router.post("", response_model=StoreOut, status_code=201)
def create_store(body: StoreCreate, db: Session = Depends(get_db)):
    store = Store(**body.model_dump())
    db.add(store)
    db.commit()
    db.refresh(store)
    return store


@router.delete("/{store_id}", status_code=204)
def delete_store(store_id: int, db: Session = Depends(get_db)):
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    db.delete(store)
    db.commit()
