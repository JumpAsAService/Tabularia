"""Who opens the reports, for the Reports tab (routes/reports.py, GET /reports/search):
the reports opened by the most people in the last 30 days come first.

An opening is the viewer saying so (POST /reports/{id}/views) once it has the report,
not a render: the viewer may not render at all (a required value still to choose), and
it renders again at every value chosen, column sorted or group closed, while the API,
the flow groups and the notebooks render without anyone looking. A person counts once a
day per report: one row per report, person and day (`report_views`), the day being the
installation's (APP__TIMEZONE), so «12 people in 30 days» means twelve people and
«opened 40 times» forty person-days, whoever reloads the page. API tokens are not
counted: a script is not a reader.

The rows serve the window only: the housekeeping removes those older than it
(services/housekeeping.py). How many there have ever been stays on the report
(`views_total`, counted as each row is written), with the moment of the latest opening
(`last_viewed_at`, at every opening). The rows go with the report and with the person (routes/users.py).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Report, ReportView, User

WINDOW_DAYS = 30


def today(at: Optional[datetime] = None) -> date:
    """The installation's day: «the same day» for a person in Rome is not UTC's."""
    return (at or datetime.now(timezone.utc)).astimezone(get_settings().app.tzinfo()).date()


def window_start(at: Optional[datetime] = None) -> date:
    """The first day the window counts: today and the 29 before it."""
    return today(at) - timedelta(days=WINDOW_DAYS - 1)


def record(session: Session, report: Report, user: User, at: Optional[datetime] = None) -> bool:
    """`user` opened `report` now. True when it is their first opening of the day, which
    counts; the latest opening is noted either way. Commits."""
    now = at or datetime.now(timezone.utc)
    day = today(now)
    seen = session.exec(select(ReportView.id).where(
        ReportView.report_id == report.id, ReportView.user_id == user.id, ReportView.day == day)).first()
    counted = False
    if seen is None:
        session.add(ReportView(report_id=report.id, user_id=user.id, day=day, at=now))
        try:
            session.flush()
            counted = True
        except IntegrityError:  # the same opening at the same moment, from another tab
            session.rollback()
    values = {"last_viewed_at": now}
    if counted:
        values["views_total"] = Report.views_total + 1  # in the database: two openings at once both count
    session.exec(update(Report).where(Report.id == report.id).values(**values))
    session.commit()
    return counted


def recent_counts():
    """A subquery: per report opened in the window, the people (`people`) and the
    openings, a person once a day (`opens`)."""
    return (select(ReportView.report_id.label("report_id"),
                   func.count(func.distinct(ReportView.user_id)).label("people"),
                   func.count(ReportView.id).label("opens"))
            .where(ReportView.day >= window_start())
            .group_by(ReportView.report_id)
            .subquery())


def forget_report(session: Session, report_id: int) -> None:
    """Its rows, before the report goes (SQLite runs the tests without the cascade). In
    the caller's transaction."""
    session.exec(delete(ReportView).where(ReportView.report_id == report_id))

