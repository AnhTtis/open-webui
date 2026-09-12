import { WEBUI_API_BASE_URL } from '$lib/constants';

export class MemoryApiError extends Error {
	status: number;

	constructor(message: string, status: number) {
		super(message);
		this.name = 'MemoryApiError';
		this.status = status;
	}
}

export const getMemories = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories/`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const addNewMemory = async (token: string, content: string, type = 'user', path = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories/add`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			content: content,
			type,
			path
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateMemoryById = async (
	token: string,
	id: string,
	content: string,
	type?: string,
	path?: string,
	expectedVersion?: number
) => {
	const body = {
		content,
		...(type ? { type } : {}),
		...(path !== undefined ? { path } : {}),
		...(expectedVersion !== undefined ? { expected_version: expectedVersion } : {})
	};

	try {
		const response = await fetch(`${WEBUI_API_BASE_URL}/memories/${id}/update`, {
			method: 'POST',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: `Bearer ${token}`
			},
			body: JSON.stringify(body)
		});

		if (!response.ok) {
			const payload = await response.json().catch(() => null);
			throw new MemoryApiError(
				payload?.detail ?? `Memory update failed with status ${response.status}`,
				response.status
			);
		}

		return response.json();
	} catch (error) {
		console.error(error);
		throw error;
	}
};

export const queryMemory = async (token: string, content: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories/query`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			content: content
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const reindexMemoryVectors = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories/reindex`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteMemoryById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories/${id}`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteMemoriesByUserId = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories/delete/user`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export type MemoryItem = {
	id: string;
	user_id: string;
	type: 'user' | 'context';
	path?: string | null;
	content: string;
	status: string;
	current_revision: number;
	version: number;
	sync_status: string;
	updated_at: number;
	created_at: number;
};

export type MemorySearchOptions = {
	query?: string;
	type?: 'user' | 'context' | 'all';
	status?: 'active' | 'candidate' | 'archived' | 'deleted' | 'all';
	path?: string;
	memory_id?: string;
	skip?: number;
	limit?: number;
};

export type MemoryProposal = {
	id: string;
	memory_id?: string | null;
	action: string;
	payload: Record<string, unknown>;
	status: string;
	confidence?: number | null;
	reason?: string | null;
	created_at: number;
};

export type MemoryRevision = {
	id: string;
	memory_id: string;
	revision: number;
	action: string;
	content?: string | null;
	memory_type?: string | null;
	path?: string | null;
	status: string;
	reason?: string | null;
	source: string;
	created_at: number;
};

export type MemoryProfile = {
	id: string;
	learning_paused: boolean;
	current_revision: number;
	updated_at: number;
};

const memoryRequest = async <T>(token: string, path: string, options: RequestInit = {}) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/memories${path}`, {
		...options,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`,
			...options.headers
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json() as Promise<T>;
		})
		.catch((err) => {
			error = err.detail ?? err.message ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res as T;
};

export const searchMemories = async (
	token: string,
	options: MemorySearchOptions = {}
): Promise<MemoryItem[]> => {
	return memoryRequest<MemoryItem[]>(token, '/search', {
		method: 'POST',
		body: JSON.stringify({
			query: options.query || null,
			type: options.type ?? 'all',
			status: options.status ?? 'active',
			path: options.path ?? null,
			memory_id: options.memory_id ?? null,
			skip: options.skip ?? 0,
			limit: options.limit ?? 20
		})
	});
};

export const getMemoryProposals = async (
	token: string,
	status: 'pending' | 'approved' | 'rejected' | 'all' = 'pending'
): Promise<MemoryProposal[]> => {
	const query = new URLSearchParams({ proposal_status: status });
	return memoryRequest<MemoryProposal[]>(token, `/proposals?${query.toString()}`, {
		method: 'GET'
	});
};

export const reviewMemoryProposal = async (token: string, id: string, approve: boolean) => {
	return memoryRequest<{ proposal: MemoryProposal; results: unknown[] }>(
		token,
		`/proposals/${id}/review`,
		{
			method: 'POST',
			body: JSON.stringify({ approve })
		}
	);
};

export const getMemoryHistory = async (token: string, id: string): Promise<MemoryRevision[]> => {
	return memoryRequest<MemoryRevision[]>(token, `/${id}/history`, { method: 'GET' });
};

export const restoreMemoryRevision = async (
	token: string,
	id: string,
	revision: number
): Promise<MemoryItem> => {
	return memoryRequest<MemoryItem>(token, `/${id}/restore/${revision}`, { method: 'POST' });
};

export const getMemoryProfile = async (token: string): Promise<MemoryProfile> => {
	return memoryRequest<MemoryProfile>(token, '/profile', { method: 'GET' });
};

export const setMemoryLearningPaused = async (
	token: string,
	paused: boolean
): Promise<MemoryProfile> => {
	return memoryRequest<MemoryProfile>(token, '/profile/learning', {
		method: 'POST',
		body: JSON.stringify({ paused })
	});
};

export const syncMemories = async (token: string): Promise<boolean> => {
	return memoryRequest<boolean>(token, '/reset', { method: 'POST' });
};

export type MemoryImportResult = {
	schema_version: number;
	dry_run: boolean;
	total: number;
	imported: number;
	skipped: number;
};

export const exportMemories = async (token: string): Promise<Blob> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/memories/export`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	});
	if (!response.ok) {
		throw new MemoryApiError(
			`Memory export failed with status ${response.status}`,
			response.status
		);
	}
	return response.blob();
};

export const importMemories = async (
	token: string,
	file: File,
	dryRun = false
): Promise<MemoryImportResult> => {
	const formData = new FormData();
	formData.append('file', file);
	const query = dryRun ? '?dry_run=true' : '';
	const response = await fetch(`${WEBUI_API_BASE_URL}/memories/import${query}`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		},
		body: formData
	});
	if (!response.ok) {
		const payload = await response.json().catch(() => null);
		throw new MemoryApiError(
			payload?.detail ?? `Memory import failed with status ${response.status}`,
			response.status
		);
	}
	return response.json();
};
