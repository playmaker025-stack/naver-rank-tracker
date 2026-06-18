from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.database import Base


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    mall_name: Mapped[str] = mapped_column(String, nullable=False, comment="네이버 쇼핑 mallName (검색결과 매칭용)")
    store_url: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    products: Mapped[list["TrackedProduct"]] = relationship(back_populates="store")


class TrackedProduct(Base):
    __tablename__ = "tracked_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    store_id: Mapped[int] = mapped_column(Integer, ForeignKey("stores.id"), nullable=False)
    naver_product_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    product_url: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    store: Mapped["Store"] = relationship(back_populates="products")
    keywords: Mapped[list["ProductKeyword"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    rank_history: Mapped[list["ProductRankHistory"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductKeyword(Base):
    __tablename__ = "product_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("tracked_products.id"), nullable=False)
    keyword: Mapped[str] = mapped_column(String, nullable=False)

    product: Mapped["TrackedProduct"] = relationship(back_populates="keywords")


class WatchKeyword(Base):
    __tablename__ = "watch_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    keyword: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    top10_history: Mapped[list["KeywordTop10History"]] = relationship(back_populates="watch_keyword", cascade="all, delete-orphan")


class ProductRankHistory(Base):
    __tablename__ = "product_rank_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("tracked_products.id"), nullable=False)
    keyword: Mapped[str] = mapped_column(String, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="순위 (None=100위 밖)")
    collected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    product: Mapped["TrackedProduct"] = relationship(back_populates="rank_history")


class KeywordTop10History(Base):
    __tablename__ = "keyword_top10_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_keyword_id: Mapped[int] = mapped_column(Integer, ForeignKey("watch_keywords.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    naver_product_id: Mapped[str] = mapped_column(String, nullable=False)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    mall_name: Mapped[str] = mapped_column(String, nullable=False)
    product_url: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    watch_keyword: Mapped["WatchKeyword"] = relationship(back_populates="top10_history")
