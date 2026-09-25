import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	exportMemories,
	exportMemoriesNdjson,
	getMemoriesPage,
	getMemoryAdminHealth,
	getMemoryHistory,
	getMemoryProposals,
	getMemorySummary,
	importMemories,
	importMemoryFile,
	MemoryApiError,
	parseMemoryApiError,
	restoreMemoryRevision,
	reviewMemoryProposal,
	searchMemories,
	updateMemoryById
} from './index';

describe('Memory Center API', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('sends bounded server-side search and pagination options', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('[]', {
				status: 200,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		await searchMemories('test-token', {
			query: 'project',
			type: 'context',
			status: 'archived',
			skip: 20,
			limit: 11
		});

		expect(fetchMock).toHaveBeenCalledWith('/api/v1/memories/search', {
			method: 'POST',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: 'Bearer test-token'
			},
			body: JSON.stringify({
				query: 'project',
				type: 'context',
				status: 'archived',
				path: null,
				memory_id: null,
				skip: 20,
				limit: 11
			})
		});
	});

	it('preserves omitted path and sends optimistic version on update', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('{}', {
				status: 200,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		await updateMemoryById('test-token', 'memory-id', 'updated', 'user', undefined, 7);

		const request = fetchMock.mock.calls[0][1];
		expect(JSON.parse(request.body)).toEqual({
			content: 'updated',
			type: 'user',
			expected_version: 7
		});
	});

	it('preserves HTTP 409 details for optimistic edit conflicts', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('{"detail":"Memory was changed by another request"}', {
				status: 409,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);
		vi.spyOn(console, 'error').mockImplementation(() => undefined);

		const request = updateMemoryById(
			'test-token',
			'memory-id',
			'stale update',
			'user',
			undefined,
			7
		);

		await expect(request).rejects.toBeInstanceOf(MemoryApiError);
		await expect(request).rejects.toMatchObject({
			status: 409,
			message: 'Memory was changed by another request'
		});
	});

	it('loads revision history and restores a selected version', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				new Response('[]', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			)
			.mockResolvedValueOnce(
				new Response('{"id":"memory-id","content":"restored"}', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			);
		vi.stubGlobal('fetch', fetchMock);

		await getMemoryHistory('test-token', 'memory-id');
		await restoreMemoryRevision('test-token', 'memory-id', 3);

		expect(fetchMock.mock.calls[0]).toEqual([
			'/api/v1/memories/memory-id/history',
			expect.objectContaining({ method: 'GET' })
		]);
		expect(fetchMock.mock.calls[1]).toEqual([
			'/api/v1/memories/memory-id/restore/3',
			expect.objectContaining({ method: 'POST' })
		]);
	});

	it('loads and reviews pending proposals through user-scoped routes', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				new Response('[]', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			)
			.mockResolvedValueOnce(
				new Response('{"proposal":{},"results":[]}', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			);
		vi.stubGlobal('fetch', fetchMock);

		await getMemoryProposals('test-token');
		await reviewMemoryProposal('test-token', 'proposal-id', true);

		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/memories/proposals?proposal_status=pending');
		expect(fetchMock.mock.calls[1]).toEqual([
			'/api/v1/memories/proposals/proposal-id/review',
			expect.objectContaining({
				method: 'POST',
				body: JSON.stringify({ approve: true })
			})
		]);
	});

	it('downloads the memory export as a Blob without sending JSON content headers', async () => {
		const exportPayload = '{"schema_version":1}';
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(exportPayload, {
				status: 200,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const result = await exportMemories('test-token');

		expect(result).toBeInstanceOf(Blob);
		expect(await result.text()).toBe(exportPayload);
		expect(fetchMock).toHaveBeenCalledWith('/api/v1/memories/export', {
			method: 'GET',
			headers: {
				Accept: 'application/json',
				authorization: 'Bearer test-token'
			}
		});
	});

	it('uploads a memory export as multipart data and supports dry-run validation', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('{"schema_version":1,"dry_run":true,"total":1,"imported":0,"skipped":1}', {
				status: 200,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);
		const file = new File(['{"schema_version":1,"memories":[]}'], 'memory.json', {
			type: 'application/json'
		});

		const result = await importMemories('test-token', file, true);
		const [url, request] = fetchMock.mock.calls[0];

		expect(result).toEqual({
			schema_version: 1,
			dry_run: true,
			total: 1,
			imported: 0,
			skipped: 1
		});
		expect(url).toBe('/api/v1/memories/import?dry_run=true');
		expect(request.method).toBe('POST');
		expect(request.headers).toEqual({
			Accept: 'application/json',
			authorization: 'Bearer test-token'
		});
		expect(request.body).toBeInstanceOf(FormData);
		expect(request.body.get('file')).toBe(file);
	});

	it('preserves import HTTP status and backend details', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('{"detail":"Memory export is too large"}', {
				status: 413,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);
		const file = new File(['too large'], 'memory.json', { type: 'application/json' });

		await expect(importMemories('test-token', file)).rejects.toMatchObject({
			name: 'MemoryApiError',
			status: 413,
			message: 'Memory export is too large'
		});
	});

	it('parses structured quota conflicts from object details', () => {
		const error = parseMemoryApiError(
			{
				detail: {
					code: 'memory_quota_exceeded',
					message: 'Memory quota exceeded',
					counted_items: 5,
					max_items: 5
				}
			},
			409
		);
		expect(error).toMatchObject({
			name: 'MemoryApiError',
			status: 409,
			code: 'memory_quota_exceeded',
			message: 'Memory quota exceeded'
		});
		expect(error.context).toMatchObject({ counted_items: 5, max_items: 5 });
	});

	it('loads a paged memory envelope from the server', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('{"items":[],"total":0,"next_cursor":null,"skip":0,"limit":20}', {
				status: 200,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const result = await getMemoriesPage('test-token', {
			query: 'project',
			type: 'context',
			status: 'active',
			skip: 20,
			limit: 10
		});

		expect(result).toEqual({ items: [], total: 0, next_cursor: null, skip: 0, limit: 20 });
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/v1/memories/page?query=project&memory_type=context&status=active&skip=20&limit=10',
			expect.objectContaining({ method: 'GET' })
		);
	});

	it('loads per-user memory summary and metadata-only admin health', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				new Response('{"counted_items":2,"counted_content_bytes":40,"max_items":5000,"max_content_bytes":5242880,"status_counts":{"active":2},"sync_counts":{},"pending_proposals":0}', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			)
			.mockResolvedValueOnce(
				new Response('{"memory_status_counts":{"active":2},"job_status_counts":{},"expired_leases":0,"oldest_pending_age_seconds":0,"cleanup_status_counts":{},"transfer_status_counts":{},"generation_status_counts":{},"quota_utilization_buckets":{"empty":1}}', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			);
		vi.stubGlobal('fetch', fetchMock);

		await expect(getMemorySummary('test-token')).resolves.toMatchObject({ counted_items: 2, max_items: 5000 });
		await expect(getMemoryAdminHealth('test-token')).resolves.toMatchObject({
			memory_status_counts: { active: 2 },
			quota_utilization_buckets: { empty: 1 }
		});
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/memories/summary');
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/memories/admin/health');
	});

	it('exports NDJSON and routes .ndjson imports to the streaming endpoint', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				new Response('{"type":"manifest"}\n', {
					status: 200,
					headers: { 'Content-Type': 'application/x-ndjson' }
				})
			)
			.mockResolvedValueOnce(
				new Response('{"schema_version":2,"dry_run":false,"total":1,"imported":1,"skipped":0}', {
					status: 200,
					headers: { 'Content-Type': 'application/json' }
				})
			);
		vi.stubGlobal('fetch', fetchMock);
		const file = new File(['{"type":"manifest"}\n'], 'memory.ndjson', {
			type: 'application/x-ndjson'
		});

		const exported = await exportMemoriesNdjson('test-token');
		expect(exported.ok).toBe(true);
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/memories/export/ndjson');
		expect(fetchMock.mock.calls[0][1]).toMatchObject({
			method: 'GET',
			headers: {
				Accept: 'application/x-ndjson',
				authorization: 'Bearer test-token'
			}
		});

		await expect(importMemoryFile('test-token', file)).resolves.toMatchObject({ imported: 1 });
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/memories/import/ndjson');
	});

	it('keeps JSON imports on the v1 endpoint', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('{"schema_version":1,"dry_run":false,"total":0,"imported":0,"skipped":0}', {
				status: 200,
				headers: { 'Content-Type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);
		const file = new File(['{"schema_version":1,"memories":[]}'], 'memory.json', {
			type: 'application/json'
		});

		await importMemoryFile('test-token', file);
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/memories/import');
	});
});
