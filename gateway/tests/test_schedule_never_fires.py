"""A valid cron that never fires ('0 0 31 2 *': February has no 31st) is refused when it
is saved, and one saved earlier no longer breaks the schedule load nor shares a lock."""
from datetime import datetime, timezone

import pytest

from app.services import orchestrator, saved_script_runs, sql_script_runs
from app.services.schedule import ScheduleError, next_fire, validate_schedule


@pytest.mark.parametrize("cron", ["0 0 31 2 *", "0 0 30 2 *", "0 0 31 4,6,9,11 *"])
def test_a_cron_that_never_fires_is_refused_with_words(cron):
    with pytest.raises(ScheduleError, match="never fires"):
        validate_schedule(cron)


def test_a_cron_that_fires_is_still_accepted():
    assert validate_schedule(" 0 0 29 2 * ") == "0 0 29 2 *"  # every leap year
    assert validate_schedule("0 0 31 * *") == "0 0 31 * *"


def test_next_fire_says_why_instead_of_failing_inside_croniter():
    # the scheduler disables a schedule whose next_fire raises a ValueError, with its reason
    with pytest.raises(ValueError, match="never fires"):
        next_fire("0 0 31 2 *", datetime.now(timezone.utc))


def test_the_schedule_load_skips_one_saved_before_the_refusal(session):
    from app.services.schedule_load import compute_schedule_load
    from tests.conftest import make_flow, make_project, make_user

    boss = make_user(session, email="capo@x.it", is_superuser=True)
    folder = make_project(session, name="Sales")
    make_flow(session, name="Never", project_id=folder.id, run_schedule="0 0 31 2 *")
    make_flow(session, name="Daily", project_id=folder.id, run_schedule="0 3 * * *")
    load = compute_schedule_load(session, boss, 7)  # it answered 500 before
    assert load["total_schedules"] == 2 and load["total_firings"] == 7


def test_every_advisory_lock_space_is_its_own():
    spaces = [orchestrator._QUEUE_LOCK, orchestrator._GROUP_QUEUE_LOCK, saved_script_runs._SCRIPT_QUEUE_LOCK,
              sql_script_runs._SQL_SCRIPT_LOCK]
    from app.routes import runs
    spaces.append(runs._REFRESH_LOCK)
    assert len(spaces) == len(set(spaces)), spaces
