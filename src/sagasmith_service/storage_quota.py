"""Transactional upload allowance shared by Pack and Module source uploads."""

from fastapi import HTTPException
from sqlalchemy import func, select, update

from sagasmith_service.models import ModuleProject, ModuleSource, PrivatePack, User


def upload_allowance(session, user_id: str, *, quota: int, file_limit: int) -> int:
    # Acquire a writer/row lock before the sum. Both upload paths hold this through commit.
    # A no-op UPDATE also serializes SQLite writers, where FOR UPDATE is ignored.
    session.execute(update(User).where(User.id == user_id).values(id=User.id))
    packs = session.scalar(
        select(func.coalesce(func.sum(PrivatePack.size_bytes), 0)).where(
            PrivatePack.owner_user_id == user_id
        )
    )
    sources = session.scalar(
        select(func.coalesce(func.sum(ModuleSource.size_bytes), 0))
        .join(ModuleProject, ModuleProject.id == ModuleSource.project_id)
        .where(ModuleProject.owner_user_id == user_id)
    )
    remaining = quota - int(packs) - int(sources)
    if remaining <= 0:
        raise HTTPException(413, "private upload storage quota exhausted")
    return min(file_limit, remaining)
