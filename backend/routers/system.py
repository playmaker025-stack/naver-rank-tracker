from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import SystemAlert

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/alerts")
def get_alerts(db: Session = Depends(get_db)):
    alerts = (
        db.query(SystemAlert)
        .filter(SystemAlert.is_dismissed == False)  # noqa: E712
        .order_by(SystemAlert.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id": a.id,
            "type": a.type,
            "reason": a.reason,
            "keyword": a.keyword,
            "created_at": a.created_at.isoformat(),
        }
        for a in alerts
    ]


@router.post("/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: int, db: Session = Depends(get_db)):
    db.query(SystemAlert).filter(SystemAlert.id == alert_id).update({"is_dismissed": True})
    db.commit()
    return {"ok": True}


@router.post("/alerts/dismiss-all")
def dismiss_all_alerts(db: Session = Depends(get_db)):
    db.query(SystemAlert).filter(SystemAlert.is_dismissed == False).update({"is_dismissed": True})  # noqa: E712
    db.commit()
    return {"ok": True}
