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


def _check_commerce_ip_and_alert() -> None:
    """Commerce API 연동 실패(IP 차단, 프록시 오류, 자격증명 만료 등) 시 텔레그램으로 알림.
    이전에는 'IP 차단' 문구가 정확히 일치할 때만 알림이 갔고, 프록시 연결 실패
    같은 다른 종류의 실패는 조용히 넘어가 며칠간 감지가 죽어있어도 아무도 몰랐음."""
    from backend.commerce import check_commerce_ip
    from backend.telegram import _send
    import logging

    result = check_commerce_ip()
    if result["ok"]:
        return

    ip = result.get("ip", "unknown")
    reason = result.get("reason", "unknown error")
    if result.get("ip_blocked"):
        msg = (
            f"⚠️ <b>[커머스 API] IP 변경 감지</b>\n"
            f"현재 서버 IP: <code>{ip}</code>\n\n"
            f"네이버 스마트스토어 센터 → 외부 서비스 연동 → API 설정에서\n"
            f"위 IP를 허용 IP로 등록해 주세요.\n"
            f"등록 전까지 제목·태그 변경 감지가 작동하지 않습니다."
        )
    else:
        msg = (
            f"⚠️ <b>[커머스 API] 연동 실패</b>\n"
            f"현재 서버 IP: <code>{ip}</code>\n"
            f"사유: <code>{reason}</code>\n\n"
            f"제목·태그 변경 감지가 작동하지 않고 있습니다."
        )
    _send(msg)
    logging.warning("Commerce API check failed. current_ip=%s reason=%s", ip, reason)


def _run_collection():
    """수집 실행 래퍼. 예외가 나도 반드시 알림이 가도록 감싼다.

    2026-08-01~08-05에 네이버 쇼핑검색 API 종료로 collect_all이 매번 예외를 던졌는데,
    여기서 잡지 않아 아래 알림 코드까지 도달하지 못했고 5일간 아무도 모른 채
    데이터가 비어 있었다. 조용히 죽는 것만은 막는다."""
    import logging
    try:
        _run_collection_inner()
    except Exception as e:
        logging.exception("수집 실패")
        try:
            from backend.telegram import _send
            _send(
                f"🚨 <b>순위 수집 실패</b>\n"
                f"<code>{type(e).__name__}: {e}</code>\n\n"
                f"순위 데이터가 쌓이지 않고 있습니다. 서버 로그를 확인해 주세요."
            )
        except Exception:
            logging.exception("실패 알림 발송도 실패")


def _run_collection_inner():
    _check_commerce_ip_and_alert()
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
        if result.get("skipped"):
            # 앞 회차가 아직 돌고 있다. 알림까지 진행하면 변동 없는 요약이 한 번 더 나간다.
            import logging as _logging
            _logging.warning("수집 건너뜀 — %s", result.get("reason"))
            return

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
    # 10:00 KST / 19:00 KST — 1일 2회 수집.
    # 쇼핑검색 API 종료 후 통합검색 파싱으로 전환하면서 3회에서 2회로 줄였다.
    # 키워드 244개 × 회당 1요청이라 3회면 하루 732요청이 되는데, 공식 API가 아니라
    # 봇으로 판정되면 유일하게 남은 수집 경로가 막힌다.
    kst = "Asia/Seoul"
    scheduler.add_job(_run_collection, CronTrigger(hour=10, minute=0, timezone=kst), id="collect_morning", replace_existing=True)
    scheduler.add_job(_run_collection, CronTrigger(hour=19, minute=0, timezone=kst), id="collect_evening", replace_existing=True)
    scheduler.add_job(_cleanup_old_snapshots, CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=kst), id="cleanup_snapshots", replace_existing=True)
    scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
