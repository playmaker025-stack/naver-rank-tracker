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

        import os as _os
        # 스토어별 데이터: alerts(5위이상), changes(2위이상 summary용)
        store_data: Dict[int, dict] = {}
        for p in products:
            if p.store_id not in store_data:
                token_key = (p.store.telegram_token_key if p.store else None) or "TELEGRAM_BOT_TOKEN"
                store_data[p.store_id] = {
                    "alerts": [],
                    "changes": [],
                    "chat_id": p.store.telegram_chat_id if p.store else None,
                    "bot_token": _os.environ.get(token_key),
                }
            for pk in p.keywords:
                latest = (
                    db.query(ProductRankHistory)
                    .filter(ProductRankHistory.product_id == p.id, ProductRankHistory.keyword == pk.keyword)
                    .order_by(desc(ProductRankHistory.collected_at))
                    .first()
                )
                curr_rank = latest.rank if latest else None
                prev_rank = prev_ranks.get((p.id, pk.keyword))
                if prev_rank is not None and curr_rank is not None:
                    diff = prev_rank - curr_rank  # 양수=상승
                    if abs(diff) >= 2:
                        store_data[p.store_id]["changes"].append(
                            {"product": p.product_name, "keyword": pk.keyword,
                             "prev": prev_rank, "curr": curr_rank, "diff": diff}
                        )
                    if abs(diff) >= 5:
                        store_data[p.store_id]["alerts"].append(
                            {"product": p.product_name, "keyword": pk.keyword, "prev": prev_rank, "curr": curr_rank}
                        )
                elif prev_rank is None and curr_rank is not None:
                    store_data[p.store_id]["alerts"].append(
                        {"product": p.product_name, "keyword": pk.keyword, "prev": None, "curr": curr_rank}
                    )

        for info in store_data.values():
            if info["alerts"]:
                send_rank_alert(info["alerts"], chat_id=info["chat_id"], bot_token=info["bot_token"])

        for info in store_data.values():
            if info["chat_id"] or info["bot_token"]:
                send_collection_summary(
                    result,
                    changes=info["changes"],
                    chat_id=info["chat_id"],
                    bot_token=info["bot_token"],
                )
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
    # 10:00 KST / 15:00 KST / 19:00 KST — 1일 3회 수집
    kst = "Asia/Seoul"
    scheduler.add_job(_run_collection, CronTrigger(hour=10, minute=0, timezone=kst), id="collect_morning",   replace_existing=True)
    scheduler.add_job(_run_collection, CronTrigger(hour=15, minute=0, timezone=kst), id="collect_afternoon", replace_existing=True)
    scheduler.add_job(_run_collection, CronTrigger(hour=19, minute=0, timezone=kst), id="collect_evening",   replace_existing=True)
    scheduler.add_job(_cleanup_old_snapshots, CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=kst), id="cleanup_snapshots", replace_existing=True)
    scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
