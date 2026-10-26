import uuid
from datetime import datetime, timezone

from app.core import clients


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_by_email(email: str) -> dict | None:
    return await clients.users().find_one({"email": email.strip().lower()})


async def get_by_id(user_id: str) -> dict | None:
    return await clients.users().find_one({"_id": user_id})


async def create(email: str, password_hash: str) -> dict:
    doc = {
        "_id": str(uuid.uuid4()),
        "email": email.strip().lower(),
        "password_hash": password_hash,
        "created_at": _now(),
    }
    await clients.users().insert_one(doc)
    return doc


def public_view(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "email": doc["email"],
        "created_at": doc["created_at"].isoformat(),
    }
