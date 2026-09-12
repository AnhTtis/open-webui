import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from open_webui.internal.db import Base  # noqa: E402
from open_webui.models.memories import Memories, MemoryConflictError  # noqa: E402
from open_webui.models.users import User  # noqa: E402


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / 'memory-lifecycle.db'
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
                        role='user',
                        created_at=now,
                        updated_at=now,
                    ),
                    User(
                        id='user-2',
                        email='user-2@example.test',
                        name='User Two',
                        role='user',
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_optimistic_update_rejects_stale_version(self):
        async with self.sessions() as db:
            memory = await Memories.insert_new_memory(
                'user-1',
                'Original value',
                memory_type='context',
                path='projects/example',
                db=db,
            )
            updated = await Memories.update_memory_by_id_and_user_id(
                memory.id,
                'user-1',
                'First update',
                expected_version=memory.version,
                db=db,
            )

            with self.assertRaises(MemoryConflictError):
                await Memories.update_memory_by_id_and_user_id(
                    memory.id,
                    'user-1',
                    'Stale update',
                    expected_version=memory.version,
                    db=db,
                )

            current = await Memories.get_memory_by_id(memory.id, db=db)
            self.assertEqual(current.content, 'First update')
            self.assertEqual(current.version, updated.version)

    async def test_soft_deleted_memory_can_be_restored_from_revision(self):
        async with self.sessions() as db:
            memory = await Memories.insert_new_memory(
                'user-1',
                'Remember this value',
                memory_type='user',
                path='preferences/editor',
                db=db,
            )
            self.assertTrue(await Memories.delete_memory_by_id_and_user_id(memory.id, 'user-1', db=db))

            deleted = await Memories.get_memory_by_id(memory.id, db=db)
            self.assertEqual(deleted.status, 'deleted')
            self.assertIsNotNone(deleted.deleted_at)

            restored = await Memories.restore_memory_revision(memory.id, 1, 'user-1', db=db)
            self.assertEqual(restored.status, 'active')
            self.assertEqual(restored.content, 'Remember this value')
            self.assertEqual(restored.path, 'preferences/editor')
            self.assertIsNone(restored.deleted_at)

            revisions = await Memories.get_memory_revisions(memory.id, 'user-1', db=db)
            self.assertEqual([revision.action for revision in revisions], ['restore', 'remove', 'add'])

    async def test_clear_only_tombstones_active_memories_and_preserves_history(self):
        async with self.sessions() as db:
            first = await Memories.insert_new_memory('user-1', 'First memory', db=db)
            second = await Memories.insert_new_memory('user-1', 'Second memory', db=db)

            self.assertTrue(await Memories.delete_memories_by_user_id('user-1', db=db))

            for memory_id in (first.id, second.id):
                deleted = await Memories.get_memory_by_id(memory_id, db=db)
                self.assertEqual(deleted.status, 'deleted')
                revisions = await Memories.get_memory_revisions(memory_id, 'user-1', db=db)
                self.assertEqual([revision.action for revision in revisions], ['remove', 'add'])

            restored = await Memories.restore_memory_revision(first.id, 1, 'user-1', db=db)
            self.assertEqual(restored.status, 'active')
            self.assertEqual(restored.content, 'First memory')

            user_two = await Memories.insert_new_memory('user-2', 'Other tenant memory', db=db)
            self.assertTrue(await Memories.delete_memories_by_user_id('user-1', db=db))
            untouched = await Memories.get_memory_by_id(user_two.id, db=db)
            self.assertEqual(untouched.status, 'active')

    async def test_cross_user_mutations_and_history_are_denied(self):
        async with self.sessions() as db:
            memory = await Memories.insert_new_memory('user-1', 'Private memory', db=db)

            updated = await Memories.update_memory_by_id_and_user_id(
                memory.id,
                'user-2',
                'Changed by another user',
                db=db,
            )
            deleted = await Memories.delete_memory_by_id_and_user_id(memory.id, 'user-2', db=db)
            restored = await Memories.restore_memory_revision(memory.id, 1, 'user-2', db=db)
            history = await Memories.get_memory_revisions(memory.id, 'user-2', db=db)

            self.assertIsNone(updated)
            self.assertFalse(deleted)
            self.assertIsNone(restored)
            self.assertEqual(history, [])
            current = await Memories.get_memory_by_id(memory.id, db=db)
            self.assertEqual(current.content, 'Private memory')
            self.assertEqual(current.status, 'active')

    async def test_proposal_review_is_single_use(self):
        async with self.sessions() as db:
            proposals = await Memories.create_proposals(
                'user-1',
                [{'action': 'add', 'content': 'Approved once', 'type': 'context'}],
                db=db,
            )
            proposal_id = proposals[0].id

            approved, results = await Memories.review_proposal(proposal_id, 'user-1', True, db=db)
            repeated, repeated_results = await Memories.review_proposal(proposal_id, 'user-1', True, db=db)

            self.assertEqual(approved.status, 'approved')
            self.assertEqual(results[0]['status'], 'created')
            self.assertIsNone(repeated)
            self.assertEqual(repeated_results, [])

    async def test_concurrent_proposal_review_applies_at_most_once(self):
        async with self.sessions() as db:
            proposals = await Memories.create_proposals(
                'user-1',
                [{'action': 'add', 'content': 'Concurrent approval', 'type': 'context'}],
                db=db,
            )
            proposal_id = proposals[0].id

        async def approve_once():
            async with self.sessions() as db:
                return await Memories.review_proposal(proposal_id, 'user-1', True, db=db)

        outcomes = await asyncio.gather(approve_once(), approve_once())
        approved = [proposal for proposal, _ in outcomes if proposal is not None]

        self.assertEqual(len(approved), 1)
        async with self.sessions() as db:
            memories, count = await Memories.search_memories(
                'user-1',
                query='Concurrent approval',
                status='active',
                db=db,
            )
            self.assertEqual(count, 1)
            self.assertEqual(len(memories), 1)

    async def test_export_is_user_scoped_and_omits_tenant_ids(self):
        async with self.sessions() as db:
            first = await Memories.insert_new_memory('user-1', 'Private export', db=db)
            await Memories.insert_new_memory('user-2', 'Other tenant', db=db)
            bundle = await Memories.export_memory_bundle('user-1', db=db)

            self.assertEqual(bundle['schema_version'], 1)
            self.assertEqual([item['content'] for item in bundle['memories']], ['Private export'])
            self.assertTrue(all('user_id' not in item for item in bundle['memories']))
            self.assertTrue(all(item['memory_id'] == first.id for item in bundle['revisions']))
            self.assertNotIn('user_id', bundle['profiles'][0] if bundle['profiles'] else {})
            self.assertNotIn('user-2', str(bundle))

    async def test_import_dry_run_counts_duplicates_without_mutating(self):
        async with self.sessions() as db:
            await Memories.insert_new_memory('user-1', 'Already saved', db=db)
            bundle = {
                'schema_version': 1,
                'memories': [
                    {'content': 'Already saved', 'type': 'context'},
                    {
                        'content': 'New imported memory',
                        'type': 'user',
                        'scope': 'working',
                        'kind': 'goal',
                        'structured_value': {'priority': 'high'},
                        'user_id': 'user-2',
                    },
                    {'content': 'New imported memory', 'type': 'user'},
                ],
            }

            result = await Memories.import_memory_bundle('user-1', bundle, dry_run=True, db=db)
            memories, count = await Memories.search_memories('user-1', status='all', db=db)

            self.assertEqual(
                result,
                {
                    'schema_version': 1,
                    'dry_run': True,
                    'total': 3,
                    'imported': 1,
                    'skipped': 2,
                },
            )
            self.assertEqual(count, 1)
            self.assertEqual(len(memories), 1)

    async def test_import_preserves_status_and_isolates_user(self):
        async with self.sessions() as db:
            bundle = {
                'schema_version': 1,
                'memories': [
                    {
                        'content': 'Imported working goal',
                        'type': 'user',
                        'scope': 'working',
                        'kind': 'goal',
                        'structured_value': {'priority': 'high'},
                        'status': 'candidate',
                        'user_id': 'user-2',
                    },
                    {'content': 'Imported deleted memory', 'type': 'context', 'status': 'deleted'},
                    {'content': 'Imported archived memory', 'type': 'context', 'status': 'archived'},
                ],
            }

            result = await Memories.import_memory_bundle('user-1', bundle, db=db)
            memories, count = await Memories.search_memories('user-1', status='all', db=db)
            user_one = {memory.content: memory for memory in memories}
            user_two, user_two_count = await Memories.search_memories('user-2', status='all', db=db)

            self.assertEqual(result['imported'], 3)
            self.assertEqual(result['skipped'], 0)
            self.assertEqual(count, 3)
            self.assertEqual(user_one['Imported working goal'].status, 'candidate')
            self.assertEqual(user_one['Imported working goal'].scope, 'working')
            self.assertEqual(user_one['Imported working goal'].structured_value, {'priority': 'high'})
            self.assertEqual(user_one['Imported deleted memory'].status, 'deleted')
            self.assertEqual(user_one['Imported archived memory'].status, 'archived')
            self.assertEqual(user_two, [])
            self.assertEqual(user_two_count, 0)

            deleted_history = await Memories.get_memory_revisions(
                user_one['Imported deleted memory'].id, 'user-1', db=db
            )
            self.assertEqual([revision.action for revision in deleted_history[:2]], ['remove', 'add'])

    async def test_import_rejects_invalid_schema_and_status(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported memory export schema'):
            Memories.validate_memory_bundle({'schema_version': 2, 'memories': []})
        with self.assertRaisesRegex(ValueError, 'invalid status'):
            Memories.validate_memory_bundle(
                {
                    'schema_version': 1,
                    'memories': [{'content': 'bad status', 'status': 'unknown'}],
                }
            )


if __name__ == '__main__':
    unittest.main()
