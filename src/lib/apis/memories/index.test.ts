import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	exportMemories,
	getMemoryHistory,
	getMemoryProposals,
	importMemories,
	MemoryApiError,
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
});
