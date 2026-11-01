"""One gateway, as a process of its own, for test_ai_comment.py: readers of one comment at once, let go
by a line on stdin, whose states it prints. It shares nothing with the others but the database."""
import asyncio
import json
import sys
from collections import Counter

from sqlmodel import Session

from app.db.session import engine
from app.models import User
from app.services import ai_comment
from tests.test_ai_comment import definition, numbers


async def reader(user_id: int) -> str:
    with Session(engine) as s:
        said = await ai_comment.comment(s, s.get(User, user_id), definition(), "story", numbers(), where=("dashboard", 7), wait=60)
    return said.state


async def main(user_id: int, readers: int) -> None:
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    states = await asyncio.gather(*(reader(user_id) for _ in range(readers)))
    print(json.dumps(Counter(states)), flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
