"""Centralized account deletion for admin, SCIM, and compatibility callers.

Canonical user deletion must outlive derivative memory vectors. This service
revokes credentials first, then deletes the user row in one SQL transaction
while inserting an independent memory cleanup marker that is not FK-bound to
the user.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_db_context
from open_webui.models.auths import Auth
from open_webui.models.chats import Chats
from open_webui.models.groups import Groups
from open_webui.models.memories import Memories
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import ApiKey, User, Users
from open_webui.utils.auth import revoke_user_tokens
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


class AccountDeleteError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _http(error: AccountDeleteError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


async def _delete_account_tx(db: AsyncSession, user_id: str) -> None:
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalars().first()
    if not user:
        raise AccountDeleteError(status.HTTP_404_NOT_FOUND, ERROR_MESSAGES.USER_NOT_FOUND)

    await Memories.insert_account_cleanup_tx(db, user_id)
    await Memories.cancel_user_jobs_tx(db, user_id)
    await Groups.remove_user_from_all_groups_tx(db, user_id)
    await Chats.delete_chats_by_user_id_tx(db, user_id)
    await db.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
    await OAuthSessions.delete_sessions_by_user_id_tx(db, user_id)
    await db.execute(delete(Auth).where(Auth.id == user_id))
    await db.execute(delete(User).where(User.id == user_id))


async def delete_account(
    user_id: str,
    *,
    request: Request | None = None,
    actor=None,
    source: str = 'admin',
    protect_primary_admin: bool = True,
    forbid_self_delete: bool = False,
    require_scim_user: bool = False,
    db: AsyncSession | None = None,
) -> bool:
    if require_scim_user:
        scim_user = await Users.get_scim_user_by_id(user_id, db=db)
        if not scim_user:
            raise AccountDeleteError(status.HTTP_404_NOT_FOUND, f'User {user_id} not found')

    if protect_primary_admin:
        first_user = await Users.get_first_user(db=db)
        if first_user and first_user.id == user_id:
            raise AccountDeleteError(status.HTTP_403_FORBIDDEN, ERROR_MESSAGES.ACTION_PROHIBITED)

    if forbid_self_delete and actor is not None and getattr(actor, 'id', None) == user_id:
        raise AccountDeleteError(status.HTTP_403_FORBIDDEN, ERROR_MESSAGES.ACTION_PROHIBITED)

    target = await Users.get_user_by_id(user_id, db=db)
    if not target:
        raise AccountDeleteError(status.HTTP_404_NOT_FOUND, ERROR_MESSAGES.USER_NOT_FOUND)

    if request is not None:
        try:
            await revoke_user_tokens(request, user_id)
        except Exception:
            log.exception('Failed to revoke tokens for user %s', user_id)
            raise AccountDeleteError(status.HTTP_500_INTERNAL_SERVER_ERROR, ERROR_MESSAGES.DELETE_USER_ERROR)

    try:
        if db is not None:
            await _delete_account_tx(db, user_id)
            await db.commit()
        else:
            async with get_async_db_context() as session:
                await _delete_account_tx(session, user_id)
                await session.commit()
    except AccountDeleteError:
        if db is not None:
            await db.rollback()
        raise
    except Exception:
        log.exception('Account deletion failed for user %s', user_id)
        if db is not None:
            await db.rollback()
        raise AccountDeleteError(status.HTTP_500_INTERNAL_SERVER_ERROR, ERROR_MESSAGES.DELETE_USER_ERROR)

    if request is not None:
        await publish_event(
            request,
            EVENTS.USER_DELETED,
            actor=actor,
            subject_id=user_id,
            source=source,
        )
    return True


async def delete_account_or_raise(
    user_id: str,
    *,
    request: Request | None = None,
    actor=None,
    source: str = 'admin',
    protect_primary_admin: bool = True,
    forbid_self_delete: bool = False,
    require_scim_user: bool = False,
    db: AsyncSession | None = None,
) -> bool:
    try:
        return await delete_account(
            user_id,
            request=request,
            actor=actor,
            source=source,
            protect_primary_admin=protect_primary_admin,
            forbid_self_delete=forbid_self_delete,
            require_scim_user=require_scim_user,
            db=db,
        )
    except AccountDeleteError as error:
        raise _http(error) from error
