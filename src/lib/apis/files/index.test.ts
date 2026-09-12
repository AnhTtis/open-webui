import { afterEach, describe, expect, it, vi } from 'vitest';

import { getFilePreviewById } from './index';

describe('getFilePreviewById', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('requests an authenticated PDF preview and returns its bytes', async () => {
		const pdf = new Uint8Array([0x25, 0x50, 0x44, 0x46]);
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(pdf, {
				status: 200,
				headers: { 'Content-Type': 'application/pdf' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const result = await getFilePreviewById('test-token', 'file-id');

		expect(new Uint8Array(result)).toEqual(pdf);
		expect(fetchMock).toHaveBeenCalledWith('/api/v1/files/file-id/preview', {
			method: 'GET',
			headers: {
				Accept: 'application/pdf',
				authorization: 'Bearer test-token'
			},
			credentials: 'include'
		});
	});

	it('surfaces the backend detail so callers can use their fallback preview', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue(
				new Response(JSON.stringify({ detail: 'LibreOffice executable is unavailable' }), {
					status: 503,
					headers: { 'Content-Type': 'application/json' }
				})
			)
		);

		await expect(getFilePreviewById('test-token', 'file-id')).rejects.toThrow(
			'LibreOffice executable is unavailable'
		);
	});
});
