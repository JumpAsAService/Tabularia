"""Names for duplicated objects, as a file manager gives them: «Sales» copied is
«Sales (1)», copied again «Sales (2)»; a copy of «Sales (1)» is the next free
«Sales (n)», never «Sales (1) (1)». Names are unique per folder for every kind of
object but flows, which take the same rule all the same."""
import re
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

_NUMBERED = re.compile(r"^(.*\S) \((\d+)\)$")


def base_name(name: str) -> str:
    """The name without the copy number it may already carry."""
    match = _NUMBERED.match(name)
    return match.group(1) if match else name


def copy_name(name: str, taken: Iterable[str]) -> str:
    """The first «base (i)» not among the names already in the folder."""
    base, used = base_name(name), set(taken)
    i = 1
    while f"{base} ({i})" in used:
        i += 1
    return f"{base} ({i})"


def free_copy_name(session: Session, model, project_id: int, name: str) -> str:
    """The name a copy of `name` takes in the folder, reading only the names that
    could clash with it."""
    prefix = f"{base_name(name)} ("
    taken = session.exec(
        select(model.name).where(model.project_id == project_id, model.name.startswith(prefix, autoescape=True))
    ).all()
    return copy_name(name, taken)


def commit_copy(session: Session) -> None:
    """Two copies made at the same moment may pick the same name: the folder's unique
    name refuses the second one, which gets a clear answer instead of a server error."""
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Another copy took that name at the same moment: try again")
