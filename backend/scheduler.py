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
                is_notable = (
                    (prev_rank is None and curr_rank is not None)  # 신규 진입
                    or (prev_rank is not None and curr_rank is not None and abs(prev_rank - curr_rank) >= 5)  # 5위 이상 급변동
                )
                if is_notable:
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
    # CronTrigger에 timezone 명시 — Railway 서버는 UTC이므로 KST(UTC+9) 변환
    # 10:00 KST = 01:00 UTC / 19:00 KST = 10:00 UTC / 03:00 KST(일) = 18:00 UTC(토)
    kst = "Asia/Seoul"
    scheduler.add_job(_run_collection, CronTrigger(hour=10, minute=0, timezone=kst), id="collect_morning", replace_existing=True)
    scheduler.add_job(_run_collection, CronTrigger(hour=19, minute=0, timezone=kst), id="collect_evening", replace_existing=True)
    scheduler.add_job(_cleanup_old_snapshots, CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=kst), id="cleanup_snapshots", replace_existing=True)
    scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
