from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.collector import collect_all
from backend.database import SessionLocal
from backend.telegram import send_rank_alert, send_collection_summary
from backend.models import KeywordCompetitorSnapshot, ProductRankHistory, TrackedProduct
from sqlalchemy import desc

scheduler = BackgroundScheduler(timezone="Asia/Seoul")


def _run_collection():
    db = SessionLocal()
    try:
        products = db.query(TrackedProduct).filter(TrackedProduct.is_active == True).all()  # noqa: E712
        prev_ranks: Dict[Tuple, Optional[int]] = {}
        for p in products:
            for pk in p.keywords:
                latest = (
                    db.query(ProductRankHistory)
                    .filter(ProductRankHistory.product_id == p.id, ProductRankHistory.keyword == pk.keyword)
                    .order_by(desc(ProductRankHistory.collected_at))
                    .first()
                )
                prev_ranks[(p.id, pk.keyword)] = latest.rank if latest else None

        result = collect_all(db)

        alerts = []
        for p in products:
            for pk in p.keywords:
                latest = (
                    db.query(ProductRankHistory)
                    .filter(ProductRankHistory.product_id == p.id, ProductRankHistory.keyword == pk.keyword)
                    .order_by(desc(ProductRankHistory.collected_at))
                    .first()
                )
                curr_rank = latest.rank if latest else None
                prev_rank = prev_ranks.get((p.id, pk.keyword))
                if curr_rank != prev_rank:
                    alerts.append({"product": p.product_name, "keyword": pk.keyword, "prev": prev_rank, "curr": curr_rank})

        if alerts:
            send_rank_alert(alerts)
        send_collection_summary(result)
    finally:
        db.close()


def _cleanup_old_snapshots(keep_days: int = 50):
    """50일 이상 된 경쟁사 스냅샷 자동 삭제."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        deleted = (
            db.query(KeywordCompetitorSnapshot)
            .filter(KeywordCompetitorSnapshot.collected_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            import logging
            logging.info("경쟁사 스냅샷 자동 삭제: %d건 (50일 초과)", deleted)
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(_run_collection, CronTrigger(hour=10, minute=0), id="collect_morning", replace_existing=True)
    scheduler.add_job(_run_collection, CronTrigger(hour=19, minute=0), id="collect_evening", replace_existing=True)
    # 매주 일요일 새벽 3시에 오래된 경쟁사 스냅샷 정리
    scheduler.add_job(_cleanup_old_snapshots, CronTrigger(day_of_week="sun", hour=3, minute=0), id="cleanup_snapshots", replace_existing=True)
    scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
