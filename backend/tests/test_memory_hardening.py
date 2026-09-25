import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from sqlalchemy import event, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from open_webui.internal.db import Base  # noqa: E402
from open_webui.models.auths import Auth  # noqa: E402
from open_webui.models.memories import (  # noqa: E402
    Memories,
    Memory,
    MemoryAccountCleanup,
    MemoryQuotaExceeded,
    memory_vector_doc_id,
)
from open_webui.models.users import User, Users  # noqa: E402
from open_webui.routers import memories as memories_router  # noqa: E402
from open_webui.services import account_lifecycle  # noqa: E402
from open_webui.services.account_lifecycle import delete_account  # noqa: E402
from open_webui.utils.memory_jobs import embedding_fingerprint  # noqa: E402
from open_webui.utils.memory_limits import clamp_page_size, utf8_bytes  # noqa: E402


class MemoryHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / 'memory-hardening.db'
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{database_path.as_posix()}')

        @event.listens_for(self.engine.sync_engine, 'connect')
        def enable_foreign_keys(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with self.sessions() as db:
            now = 1_700_000_000
            db.add_all(
                [
                    User(
                        id='user-1',
                        email='user-1@example.test',
                        name='User One',
                        role='admin',
                        created_at=now,
                        updated_at=now,
                    ),
                    User(
                        id='user-secret-id',
                        email='private-user@example.test',
                        name='Private User',
                        role='user',
                        created_at=now,
                        updated_at=now,
                    ),
                    Auth(id='user-1', email='user-1@example.test', password='x', active=True),
                    Auth(id='user-secret-id', email='private-user@example.test', password='x', active=True),
                ]
            )
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_sqlite_foreign_keys_are_enabled(self):
        async with self.sessions() as db:
            enabled = (await db.execute(text('PRAGMA foreign_keys'))).scalar()
        self.assertEqual(enabled, 1)

    async def test_list_page_is_bounded_and_returns_total(self):
        async with self.sessions() as db:
            await Memories.insert_new_memory('user-1', 'alpha memory', db=db)
            await Memories.insert_new_memory('user-1', 'beta memory', db=db)
            await Memories.insert_new_memory('user-1', 'gamma memory', db=db)
            items, total, next_cursor = await Memories.list_memories_page('user-1', db=db, limit=2)
            self.assertEqual(total, 3)
            self.assertEqual(len(items), 2)
            self.assertIsNotNone(next_cursor)
            self.assertEqual(clamp_page_size(500), 100)

    async def test_duplicate_live_hash_is_a_noop(self):
        async with self.sessions() as db:
            first = await Memories.insert_new_memory('user-1', 'Same fact', memory_type='user', db=db)
            second = await Memories.insert_new_memory('user-1', ' same   fact ', memory_type='user', db=db)
            self.assertEqual(first.id, second.id)
            items, total, _ = await Memories.list_memories_page('user-1', db=db, status='all')
            self.assertEqual(total, 1)
            self.assertEqual(len(items), 1)

    async def test_quota_uses_utf8_bytes_and_blocks_over_limit(self):
        content = 'cà phê ☕'
        self.assertEqual(utf8_bytes(content), len(content.encode('utf-8')))
        with patch('open_webui.models.memories.get_memory_quota_limits', new=AsyncMock(return_value=(1, 32))):
            async with self.sessions() as db:
                await Memories.insert_new_memory('user-1', content, db=db)
                with self.assertRaises(MemoryQuotaExceeded) as raised:
                    await Memories.insert_new_memory('user-1', 'second item', db=db)
                payload = raised.exception.as_dict()
                self.assertEqual(payload['code'], 'memory_quota_exceeded')
                self.assertEqual(payload['max_items'], 1)

    async def test_vector_document_id_includes_generation_and_revision(self):
        self.assertEqual(memory_vector_doc_id(4, 'mem-id', 9), 'mem:4:mem-id:r9')

    async def test_embedding_fingerprint_excludes_api_keys(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-secret-value'
        fingerprint = embedding_fingerprint()
        self.assertNotIn('sk-test-secret-value', fingerprint)
        self.assertNotIn('OPENAI_API_KEY', fingerprint)
        self.assertEqual(len(fingerprint), 32)

    async def test_job_complete_requires_matching_claim(self):
        async with self.sessions() as db:
            memory = await Memories.insert_new_memory('user-1', 'claim fence', db=db)
            job = await Memories.enqueue_job(
                user_id='user-1',
                job_type='upsert_vector',
                idempotency_key='test-claim-fence',
                memory_id=memory.id,
                db=db,
            )
            claimed = await Memories.claim_jobs('worker-a', limit=8, db=db)
            self.assertTrue(any(item.id == job.id for item in claimed))
            claimed_job = next(item for item in claimed if item.id == job.id)
            rejected = await Memories.complete_job(
                claimed_job.id,
                'worker-a',
                claim_token='wrong-token',
                lease_generation=claimed_job.lease_generation,
                db=db,
            )
            self.assertFalse(rejected)
            accepted = await Memories.complete_job(
                claimed_job.id,
                'worker-a',
                claim_token=claimed_job.claim_token,
                lease_generation=claimed_job.lease_generation,
                db=db,
            )
            self.assertTrue(accepted)

    async def test_admin_health_redacts_private_values(self):
        secret = 'private-memory-payload-do-not-leak'
        async with self.sessions() as db:
            await Memories.insert_new_memory(
                'user-secret-id',
                secret,
                path='secrets/never-share',
                db=db,
            )
            health = await Memories.get_admin_health(db=db)
        serialized = json.dumps(health)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('user-secret-id', serialized)
        self.assertNotIn('private-user@example.test', serialized)
        self.assertNotIn('secrets/never-share', serialized)
        self.assertIn('memory_status_counts', health)
        self.assertIn('quota_utilization_buckets', health)

    async def test_account_delete_cascades_memory_and_keeps_cleanup(self):
        async with self.sessions() as db:
            memory = await Memories.insert_new_memory('user-secret-id', 'owned memory', db=db)
            memory_id = memory.id
            with (
                patch.object(
                    Users,
                    'get_user_by_id',
                    new=AsyncMock(return_value=SimpleNamespace(id='user-secret-id')),
                ),
                patch.object(account_lifecycle.Groups, 'remove_user_from_all_groups_tx', new=AsyncMock()),
                patch.object(account_lifecycle.Chats, 'delete_chats_by_user_id_tx', new=AsyncMock()),
                patch.object(account_lifecycle.OAuthSessions, 'delete_sessions_by_user_id_tx', new=AsyncMock()),
            ):
                await delete_account('user-secret-id', protect_primary_admin=False, db=db)
            remaining_user = (await db.execute(select(User).where(User.id == 'user-secret-id'))).scalars().first()
            remaining_memory = (await db.execute(select(Memory).where(Memory.id == memory_id))).scalars().first()
            remaining_auth = (await db.execute(select(Auth).where(Auth.id == 'user-secret-id'))).scalars().first()
            cleanup = (
                await db.execute(select(MemoryAccountCleanup).where(MemoryAccountCleanup.user_id == 'user-secret-id'))
            ).scalars().first()
            self.assertIsNone(remaining_user)
            self.assertIsNone(remaining_memory)
            self.assertIsNone(remaining_auth)
            self.assertIsNotNone(cleanup)
            self.assertEqual(cleanup.status, 'pending')


class MemoryRouteHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_route_returns_envelope(self):
        user = SimpleNamespace(id='user-1')
        with (
            patch.object(memories_router, 'check_memories_permission', new=AsyncMock()),
            patch.object(
                Memories,
                'list_memories_page',
                new=AsyncMock(return_value=([], 0, None)),
            ) as listed,
        ):
            result = await memories_router.get_memories_page(skip=0, limit=20, user=user)
        listed.assert_awaited()
        self.assertEqual(result['items'], [])
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['skip'], 0)
        self.assertIn('limit', result)

    async def test_admin_health_route_is_admin_only_shape(self):
        admin = SimpleNamespace(id='admin-1', role='admin')
        payload = {
            'memory_status_counts': {'active': 2},
            'job_status_counts': {},
            'expired_leases': 0,
            'oldest_pending_age_seconds': 0,
            'cleanup_status_counts': {},
            'transfer_status_counts': {},
            'generation_status_counts': {},
            'quota_utilization_buckets': {'empty': 1},
        }
        with patch.object(Memories, 'get_admin_health', new=AsyncMock(return_value=payload)):
            result = await memories_router.get_memory_admin_health(user=admin)
        self.assertEqual(result, payload)

    async def test_quota_errors_map_to_structured_conflict(self):
        from fastapi import HTTPException

        from open_webui.models.memories import MemoryQuotaExceeded

        with self.assertRaises(HTTPException) as raised:
            memories_router._raise_memory_http(
                MemoryQuotaExceeded('Memory quota exceeded', counted_items=5, max_items=5)
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail['code'], 'memory_quota_exceeded')


if __name__ == '__main__':
    unittest.main()
