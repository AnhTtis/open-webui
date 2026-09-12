<script lang="ts">
	import Switch from '$lib/components/common/Switch.svelte';
	import { config, settings } from '$lib/stores';
	import { createEventDispatcher, onMount, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import DropdownMenu from '$lib/components/common/DropdownMenu.svelte';
	import ExperimentalBadge from '$lib/components/common/ExperimentalBadge.svelte';
	import MemoryModal from './Personalization/MemoryModal.svelte';
	import {
		deleteMemoriesByUserId,
		deleteMemoryById,
		exportMemories,
		getMemoryHistory,
		importMemories,
		getMemoryProfile,
		getMemoryProposals,
		restoreMemoryRevision,
		reviewMemoryProposal,
		searchMemories,
		setMemoryLearningPaused,
		syncMemories,
		type MemoryImportResult,
		type MemoryItem,
		type MemoryProfile,
		type MemoryProposal,
		type MemoryRevision
	} from '$lib/apis/memories';
	import { toast } from 'svelte-sonner';
	import UserSettingRow from './UserSettingRow.svelte';
	import UserSettingSection from './UserSettingSection.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Refresh from '$lib/components/icons/Refresh.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Trash from '$lib/components/icons/Trash.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const dispatch = createEventDispatcher();
	const i18n = getContext<Writable<i18nType>>('i18n');

	export let saveSettings: (settings: Record<string, unknown>) => void | Promise<void>;

	let enableMemory = false;
	let memories: MemoryItem[] = [];
	let memoryProfile: MemoryProfile | null = null;
	let proposals: MemoryProposal[] = [];
	let history: MemoryRevision[] = [];
	let loadingMemories = true;
	let loadingProposals = true;
	let loadingHistory = false;
	let changingLearning = false;
	let syncing = false;
	let reviewingProposalId: string | null = null;
	let restoringRevision: number | null = null;
	let restoringMemoryId: string | null = null;
	let exportingMemories = false;
	let importingMemories = false;
	let memoryImportInput: HTMLInputElement;
	let pendingImportFile: File | null = null;
	let importPreview: MemoryImportResult | null = null;
	let showImportConfirmDialog = false;

	let showMemoryModal = false;
	let selectedMemory: MemoryItem | null = null;
	let showClearConfirmDialog = false;
	let showDeleteConfirm = false;
	let query = '';
	let submittedQuery = '';
	let memoryType: 'all' | 'user' | 'context' = 'all';
	let memoryStatus: 'active' | 'candidate' | 'archived' | 'deleted' | 'all' = 'active';
	let page = 0;
	const pageSize = 10;
	let hasNextPage = false;
	let searchTimer: ReturnType<typeof setTimeout>;
	let searchInitialized = false;

	const actionButtonClass =
		'shrink-0 text-xs text-gray-500 transition-colors hover:text-gray-900 disabled:pointer-events-none disabled:opacity-40 dark:text-gray-500 dark:hover:text-white';
	const filterClass =
		'h-7 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300';

	const loadMemories = async () => {
		loadingMemories = true;
		const results = await searchMemories(localStorage.token, {
			query: submittedQuery,
			type: memoryType,
			status: memoryStatus,
			skip: page * pageSize,
			limit: pageSize + 1
		}).catch((error) => {
			toast.error(`${error}`);
			return [];
		});

		hasNextPage = results.length > pageSize;
		memories = results.slice(0, pageSize);
		if (page > 0 && memories.length === 0) {
			page -= 1;
			loadingMemories = false;
			await loadMemories();
			return;
		}
		loadingMemories = false;
	};

	const loadMemoryCenter = async () => {
		loadingProposals = true;
		const [profile, pendingProposals] = await Promise.all([
			getMemoryProfile(localStorage.token).catch((error) => {
				toast.error(`${error}`);
				return null;
			}),
			getMemoryProposals(localStorage.token).catch((error) => {
				toast.error(`${error}`);
				return [];
			})
		]);
		memoryProfile = profile;
		proposals = pendingProposals;
		loadingProposals = false;
	};

	const confirmDeleteMemory = (memory: MemoryItem) => {
		selectedMemory = memory;
		showDeleteConfirm = true;
	};

	const editMemory = (memory: MemoryItem) => {
		selectedMemory = memory;
		showMemoryModal = true;
	};

	const toggleHistory = async (memory: MemoryItem) => {
		if (selectedMemory?.id === memory.id && history.length > 0) {
			history = [];
			selectedMemory = null;
			return;
		}

		selectedMemory = memory;
		loadingHistory = true;
		history =
			(await getMemoryHistory(localStorage.token, memory.id).catch((error) => {
				toast.error(`${error}`);
				return [];
			})) ?? [];
		loadingHistory = false;
	};

	const restoreRevision = async (revision: MemoryRevision) => {
		if (!selectedMemory) return;
		restoringRevision = revision.revision;
		const result = await restoreMemoryRevision(
			localStorage.token,
			selectedMemory.id,
			revision.revision
		).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (result) {
			toast.success($i18n.t('Memory restored successfully'));
			await loadMemories();
			selectedMemory = result;
			history = await getMemoryHistory(localStorage.token, result.id).catch((error) => {
				toast.error(`${error}`);
				return [];
			});
		}
		restoringRevision = null;
	};

	const restoreDeletedMemory = async (memory: MemoryItem) => {
		restoringMemoryId = memory.id;
		const revisions = await getMemoryHistory(localStorage.token, memory.id).catch((error) => {
			toast.error(`${error}`);
			return [];
		});
		const source = revisions.find((revision) => revision.status !== 'deleted' && revision.content);
		if (!source) {
			toast.error($i18n.t('No restorable memory version was found'));
			restoringMemoryId = null;
			return;
		}

		const restored = await restoreMemoryRevision(
			localStorage.token,
			memory.id,
			source.revision
		).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (restored) {
			toast.success($i18n.t('Memory restored successfully'));
			if (selectedMemory?.id === memory.id) {
				selectedMemory = null;
				history = [];
			}
			await loadMemories();
		}
		restoringMemoryId = null;
	};

	const reviewProposal = async (proposal: MemoryProposal, approve: boolean) => {
		reviewingProposalId = proposal.id;
		const result = await reviewMemoryProposal(localStorage.token, proposal.id, approve).catch(
			(error) => {
				toast.error(`${error}`);
				return null;
			}
		);
		if (result) {
			proposals = proposals.filter((item) => item.id !== proposal.id);
			toast.success(approve ? $i18n.t('Memory approved') : $i18n.t('Memory suggestion dismissed'));
			if (approve) await loadMemories();
		}
		reviewingProposalId = null;
	};

	const setLearningPaused = async (paused: boolean) => {
		changingLearning = true;
		const profile = await setMemoryLearningPaused(localStorage.token, paused).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (profile) {
			memoryProfile = profile;
			toast.success(
				paused
					? $i18n.t('Automatic memory learning paused')
					: $i18n.t('Automatic memory learning resumed')
			);
		}
		changingLearning = false;
	};

	const syncMemoryCenter = async () => {
		syncing = true;
		const result = await syncMemories(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return false;
		});
		if (result) {
			toast.success($i18n.t('Memory sync started'));
			await loadMemories();
		}
		syncing = false;
	};

	const exportMemoryCenter = async () => {
		exportingMemories = true;
		const blob = await exportMemories(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (blob) {
			const url = URL.createObjectURL(blob);
			const link = document.createElement('a');
			link.href = url;
			link.download = 'open-webui-memory-export.json';
			document.body.appendChild(link);
			link.click();
			link.remove();
			setTimeout(() => URL.revokeObjectURL(url), 0);
			toast.success($i18n.t('Memory export downloaded'));
		}
		exportingMemories = false;
	};

	const importMemoryCenter = async (event: Event) => {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;

		importingMemories = true;
		const preview = await importMemories(localStorage.token, file, true).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		importingMemories = false;
		if (!preview) return;
		if (preview.total === 0) {
			toast.error($i18n.t('The selected file contains no memories to import.'));
			return;
		}

		pendingImportFile = file;
		importPreview = preview;
		showImportConfirmDialog = true;
	};

	const confirmMemoryImport = async () => {
		if (!pendingImportFile) return;
		importingMemories = true;
		const result = await importMemories(localStorage.token, pendingImportFile).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (result) {
			toast.success(
				$i18n.t('Imported {{imported}} memories; skipped {{skipped}} duplicates', {
					imported: result.imported,
					skipped: result.skipped
				})
			);
			page = 0;
			await loadMemories();
		}
		importingMemories = false;
		showImportConfirmDialog = false;
		pendingImportFile = null;
		importPreview = null;
	};

	const cancelMemoryImport = () => {
		showImportConfirmDialog = false;
		pendingImportFile = null;
		importPreview = null;
	};

	const onClearConfirmed = async () => {
		const res = await deleteMemoriesByUserId(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			toast.success($i18n.t('Memory cleared successfully'));
			page = 0;
			await loadMemories();
		}
		showClearConfirmDialog = false;
	};

	const proposalContent = (proposal: MemoryProposal) =>
		`${proposal.payload?.content ?? proposal.payload?.path ?? ''}`;

	const formatTime = (timestamp?: number | null) =>
		timestamp ? new Date(timestamp * 1000).toLocaleString() : '';

	$: syncCounts = memories.reduce(
		(counts, memory) => {
			counts[memory.sync_status] = (counts[memory.sync_status] ?? 0) + 1;
			return counts;
		},
		{} as Record<string, number>
	);

	$: searchKey = JSON.stringify([query, memoryType, memoryStatus]);

	$: if (searchInitialized && searchKey) {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(async () => {
			submittedQuery = query.trim();
			page = 0;
			await loadMemories();
		}, 300);
	}

	$: if (searchInitialized && page >= 0) {
		void loadMemories();
	}

	onMount(async () => {
		enableMemory = $settings?.memory ?? $config?.features?.enable_memories ?? false;
		await Promise.all([loadMemories(), loadMemoryCenter()]);
		searchInitialized = true;
	});
</script>

<form
	id="tab-personalization"
	class="flex h-full flex-col justify-between text-sm"
	on:submit|preventDefault={() => {
		dispatch('save');
	}}
>
	<h2 class="mb-4 text-sm font-medium text-gray-900 dark:text-white">
		{$i18n.t('Personalization')}
	</h2>

	<div class="min-h-0 flex-1 overflow-y-auto pr-1.5 scrollbar-hover">
		<UserSettingSection title={$i18n.t('Memory')} first>
			<UserSettingRow
				description={$i18n
					.t(
						"You can personalize your interactions with LLMs by adding memories through the 'Manage' button below, making them more helpful and tailored to you."
					)
					.replace($i18n.t('Manage'), $i18n.t('Add Memory'))}
			>
				<div slot="label" class="flex items-center gap-2">
					{$i18n.t('Memory')}
					<ExperimentalBadge />
				</div>

				<Switch
					bind:state={enableMemory}
					on:change={async () => {
						saveSettings({ memory: enableMemory });
					}}
				/>
			</UserSettingRow>

			{#if enableMemory}
				<div class="space-y-5">
					<div
						class="rounded-xl border border-gray-100/70 bg-gray-50/30 p-3 dark:border-white/[0.06] dark:bg-white/[0.02]"
					>
						<div class="flex items-center justify-between gap-4">
							<div>
								<div class="text-xs font-medium text-gray-800 dark:text-gray-200">
									{$i18n.t('Automatic learning')}
								</div>
								<div class="mt-0.5 text-[0.6875rem] leading-4 text-gray-500">
									{memoryProfile?.learning_paused
										? $i18n.t('Paused. New suggestions will not be learned from chats.')
										: $i18n.t('On. New useful details may appear below for your approval.')}
								</div>
							</div>
							{#if memoryProfile && !changingLearning}
								<Switch
									state={!memoryProfile.learning_paused}
									on:change={() => setLearningPaused(!memoryProfile?.learning_paused)}
								/>
							{:else}
								<Spinner className="size-4" />
							{/if}
						</div>
					</div>

					<div>
						<div class="mb-2 flex items-center justify-between gap-3">
							<div>
								<div class="text-xs font-medium text-gray-700 dark:text-gray-300">
									{$i18n.t('Suggested memories')}
									{#if !loadingProposals}
										<span class="ml-1 text-gray-400 dark:text-gray-600">{proposals.length}</span>
									{/if}
								</div>
								<div class="mt-0.5 text-[0.6875rem] text-gray-500">
									{$i18n.t('Review what was noticed before it becomes a saved memory.')}
								</div>
							</div>
						</div>

						{#if loadingProposals}
							<div class="flex min-h-14 items-center justify-center">
								<Spinner className="size-4" />
							</div>
						{:else if proposals.length === 0}
							<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
								{$i18n.t('No memory suggestions need your review.')}
							</div>
						{:else}
							<div class="flex flex-col gap-2">
								{#each proposals as proposal (proposal.id)}
									<div
										class="rounded-xl border border-gray-100/70 px-3 py-2.5 dark:border-white/[0.06]"
									>
										<div class="flex items-start justify-between gap-3">
											<div class="min-w-0">
												<div
													class="text-[0.625rem] font-medium uppercase tracking-wide text-gray-400"
												>
													{$i18n.t(
														proposal.action === 'remove'
															? 'Remove'
															: proposal.action === 'replace'
																? 'Update'
																: 'Add'
													)}
												</div>
												<div
													class="mt-1 whitespace-pre-wrap break-words text-xs text-gray-700 dark:text-gray-300"
												>
													{proposalContent(proposal) || $i18n.t('A memory change was suggested.')}
												</div>
												{#if proposal.reason}
													<div class="mt-1 text-[0.6875rem] text-gray-500">{proposal.reason}</div>
												{/if}
											</div>
											<div class="flex shrink-0 items-center gap-2">
												<button
													type="button"
													class="text-xs text-gray-500 hover:text-gray-900 hover:underline dark:hover:text-white"
													disabled={reviewingProposalId === proposal.id}
													on:click={() => reviewProposal(proposal, false)}
													>{$i18n.t('Dismiss')}</button
												>
												<button
													type="button"
													class="rounded-full bg-black px-3 py-1 text-xs text-white hover:bg-gray-900 disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-gray-100"
													disabled={reviewingProposalId === proposal.id}
													on:click={() => reviewProposal(proposal, true)}
													>{$i18n.t('Approve')}</button
												>
											</div>
										</div>
									</div>
								{/each}
							</div>
						{/if}
					</div>

					<div>
						<div class="mb-1 flex items-center justify-between gap-3">
							<div class="text-xs font-medium text-gray-700 dark:text-gray-300">
								{$i18n.t('Saved Memories')}
							</div>
							<div class="flex items-center gap-2 text-[0.6875rem] text-gray-500">
								{#if syncCounts.failed}
									<span class="text-red-500">{syncCounts.failed} {$i18n.t('need attention')}</span>
								{:else if syncCounts.pending}
									<span>{syncCounts.pending} {$i18n.t('syncing')}</span>
								{:else if memories.length > 0}
									<span>{$i18n.t('Up to date')}</span>
								{/if}
								<Tooltip content={$i18n.t('Sync memories now')}>
									<button
										type="button"
										class="rounded-lg p-1 text-gray-400 hover:text-gray-800 disabled:opacity-50 dark:hover:text-gray-200"
										disabled={syncing}
										on:click={syncMemoryCenter}
										aria-label={$i18n.t('Sync memories now')}
									>
										<Refresh className={`size-3.5 ${syncing ? 'animate-spin' : ''}`} />
									</button>
								</Tooltip>
							</div>
						</div>

						<div class="mb-2 flex flex-wrap items-center gap-2">
							<div
								class="flex min-w-48 flex-1 items-center gap-2 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 dark:border-white/[0.04] dark:bg-white/[0.03]"
							>
								<Search className="size-3.5 shrink-0 text-gray-400 dark:text-gray-600" />
								<input
									data-settings-search
									class="min-w-0 flex-1 bg-transparent py-1.5 text-xs text-gray-700 outline-hidden placeholder:text-gray-300 dark:text-gray-300 dark:placeholder:text-gray-700"
									bind:value={query}
									placeholder={$i18n.t('Search memories')}
									maxlength="500"
								/>
								{#if query}
									<button
										class="shrink-0 rounded-lg p-0.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
										type="button"
										aria-label={$i18n.t('Clear search')}
										on:click={() => (query = '')}
									>
										<XMark className="size-3" strokeWidth="2" />
									</button>
								{/if}
							</div>

							<select
								class={filterClass}
								bind:value={memoryType}
								aria-label={$i18n.t('Memory type')}
							>
								<option value="all">{$i18n.t('All types')}</option>
								<option value="user">{$i18n.t('About me')}</option>
								<option value="context">{$i18n.t('Context')}</option>
							</select>
							<select
								class={filterClass}
								bind:value={memoryStatus}
								aria-label={$i18n.t('Memory status')}
							>
								<option value="active">{$i18n.t('Active')}</option>
								<option value="candidate">{$i18n.t('Candidate')}</option>
								<option value="archived">{$i18n.t('Archived')}</option>
								<option value="deleted">{$i18n.t('Deleted')}</option>
								<option value="all">{$i18n.t('All statuses')}</option>
							</select>

							<Dropdown align="end">
								<Tooltip content={$i18n.t('Actions')}>
									<button
										class="flex h-7 items-center gap-1.5 px-1.5 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-white"
										type="button"
									>
										<span>{$i18n.t('Actions')}</span>
										<ChevronDown className="size-3" strokeWidth="2.5" />
									</button>
								</Tooltip>
								<div slot="content">
									<DropdownMenu className="w-[10.625rem] shadow-sm">
										<button
											class="flex h-[1.6875rem] w-full items-center gap-2 rounded-lg px-2 text-xs hover:text-gray-900 dark:hover:text-gray-100"
											type="button"
											on:click={() => {
												selectedMemory = null;
												showMemoryModal = true;
											}}
										>
											<Plus className="size-3.5" strokeWidth="1.5" />
											<div class="truncate text-left">{$i18n.t('Add Memory')}</div>
										</button>
										<button
											class="flex h-[1.6875rem] w-full items-center gap-2 rounded-lg px-2 text-xs hover:text-gray-900 disabled:opacity-30 dark:hover:text-gray-100"
											type="button"
											disabled={exportingMemories}
											on:click={exportMemoryCenter}
										>
											<div class="truncate text-left">{$i18n.t('Export memories')}</div>
										</button>
										<button
											class="flex h-[1.6875rem] w-full items-center gap-2 rounded-lg px-2 text-xs hover:text-gray-900 disabled:opacity-30 dark:hover:text-gray-100"
											type="button"
											disabled={importingMemories}
											on:click={() => memoryImportInput?.click()}
										>
											<div class="truncate text-left">{$i18n.t('Import memories')}</div>
										</button>
										<input
											class="hidden"
											type="file"
											accept="application/json,.json"
											bind:this={memoryImportInput}
											on:change={importMemoryCenter}
										/>
										<button
											class="flex h-[1.6875rem] w-full items-center gap-2 rounded-lg px-2 text-xs hover:text-gray-900 disabled:opacity-30 dark:hover:text-gray-100"
											type="button"
											on:click={() => (showClearConfirmDialog = true)}
										>
											<Trash className="size-3.5" strokeWidth="1.5" />
											<div class="truncate text-left">{$i18n.t('Clear memory')}</div>
										</button>
									</DropdownMenu>
								</div>
							</Dropdown>
						</div>

						{#if loadingMemories}
							<div class="flex min-h-16 items-center justify-center">
								<Spinner className="size-4" />
							</div>
						{:else if memories.length === 0}
							<div class="min-h-16 text-[0.6875rem] text-gray-400 dark:text-gray-600">
								{submittedQuery || memoryType !== 'all' || memoryStatus !== 'active'
									? $i18n.t('No results found')
									: $i18n.t('Memories accessible by LLMs will be shown here.')}
							</div>
						{:else}
							<div class="flex flex-col gap-2">
								{#each memories as memory (memory.id)}
									<div
										class="rounded-xl border border-gray-100/70 px-3 py-2.5 dark:border-white/[0.06]"
									>
										<div class="flex items-start justify-between gap-3">
											<div class="min-w-0">
												<div
													class="whitespace-pre-wrap break-words text-xs text-gray-700 dark:text-gray-300"
												>
													{memory.content}
												</div>
												<div
													class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.625rem] text-gray-400"
												>
													<span
														>{memory.type === 'user'
															? $i18n.t('About me')
															: $i18n.t('Context')}</span
													>
													{#if memory.path}<span>{memory.path}</span>{/if}
													<span>{formatTime(memory.updated_at)}</span>
													<span class={memory.sync_status === 'failed' ? 'text-red-500' : ''}>
														{memory.sync_status === 'synced'
															? $i18n.t('Synced')
															: memory.sync_status === 'failed'
																? $i18n.t('Sync failed')
																: $i18n.t('Syncing')}
													</span>
												</div>
											</div>
											<div class="flex shrink-0 items-center gap-2">
												<button
													type="button"
													class={`${actionButtonClass} hover:underline`}
													on:click={() => toggleHistory(memory)}>{$i18n.t('History')}</button
												>
												{#if memory.status === 'deleted'}
													<button
														type="button"
														class={`${actionButtonClass} hover:underline`}
														disabled={restoringMemoryId === memory.id}
														on:click={() => restoreDeletedMemory(memory)}
													>
														{restoringMemoryId === memory.id
															? $i18n.t('Restoring')
															: $i18n.t('Restore')}
													</button>
												{/if}
												<button
													type="button"
													class={`${actionButtonClass} hover:underline`}
													disabled={memory.status === 'deleted'}
													on:click={() => editMemory(memory)}>{$i18n.t('Edit')}</button
												>
												<button
													type="button"
													class={`${actionButtonClass} hover:underline`}
													disabled={memory.status === 'deleted'}
													on:click={() => confirmDeleteMemory(memory)}>{$i18n.t('Remove')}</button
												>
											</div>
										</div>

										{#if selectedMemory?.id === memory.id}
											<div class="mt-3 border-t border-gray-100/70 pt-2 dark:border-white/[0.06]">
												<div
													class="mb-2 text-[0.6875rem] font-medium text-gray-600 dark:text-gray-400"
												>
													{$i18n.t('Change history')}
												</div>
												{#if loadingHistory}
													<div class="flex justify-center py-3">
														<Spinner className="size-3.5" />
													</div>
												{:else if history.length === 0}
													<div class="text-[0.6875rem] text-gray-400">
														{$i18n.t('No earlier versions are available.')}
													</div>
												{:else}
													<div class="flex max-h-52 flex-col gap-2 overflow-y-auto scrollbar-hover">
														{#each history as revision (revision.id)}
															<div
																class="flex items-start justify-between gap-3 rounded-lg bg-gray-50/60 px-2.5 py-2 dark:bg-white/[0.03]"
															>
																<div class="min-w-0">
																	<div class="text-[0.625rem] text-gray-400">
																		{$i18n.t('Version')}
																		{revision.revision} · {formatTime(revision.created_at)}
																	</div>
																	<div
																		class="mt-0.5 whitespace-pre-wrap break-words text-[0.6875rem] text-gray-600 dark:text-gray-400"
																	>
																		{revision.content ?? $i18n.t('Memory removed')}
																	</div>
																</div>
																{#if revision.revision !== memory.current_revision && revision.content}
																	<button
																		type="button"
																		class="shrink-0 text-[0.6875rem] text-gray-500 hover:text-gray-900 hover:underline disabled:opacity-50 dark:hover:text-white"
																		disabled={restoringRevision === revision.revision}
																		on:click={() => restoreRevision(revision)}
																		>{$i18n.t('Restore')}</button
																	>
																{/if}
															</div>
														{/each}
													</div>
												{/if}
											</div>
										{/if}
									</div>
								{/each}
							</div>

							{#if page > 0 || hasNextPage}
								<div class="mt-3 flex items-center justify-center gap-4">
									<button
										type="button"
										class="rounded-lg p-1.5 hover:bg-gray-50 disabled:text-gray-300 dark:hover:bg-gray-850 dark:disabled:text-gray-700"
										disabled={page === 0 || loadingMemories}
										on:click={() => (page -= 1)}
										aria-label={$i18n.t('Previous page')}
									>
										<ChevronLeft className="size-4" strokeWidth="2" />
									</button>
									<span class="text-xs text-gray-500">{$i18n.t('Page')} {page + 1}</span>
									<button
										type="button"
										class="rounded-lg p-1.5 hover:bg-gray-50 disabled:text-gray-300 dark:hover:bg-gray-850 dark:disabled:text-gray-700"
										disabled={!hasNextPage || loadingMemories}
										on:click={() => (page += 1)}
										aria-label={$i18n.t('Next page')}
									>
										<ChevronRight className="size-4" strokeWidth="2" />
									</button>
								</div>
							{/if}
						{/if}
					</div>
				</div>
			{/if}
		</UserSettingSection>
	</div>

	<div class="flex shrink-0 justify-end text-sm font-normal">
		<button
			class="rounded-full bg-black px-3.5 py-1.5 text-sm font-normal text-white transition hover:bg-gray-900 dark:bg-white dark:text-black dark:hover:bg-gray-100"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>

<ConfirmDialog
	title={$i18n.t('Import memories?')}
	show={showImportConfirmDialog}
	on:confirm={confirmMemoryImport}
	on:cancel={cancelMemoryImport}
>
	<div class="flex-1 text-sm text-gray-500">
		{$i18n.t(
			'The file was validated. Review the expected changes before adding them to your memory.'
		)}
		<div
			class="mt-3 rounded-lg border border-gray-100/70 bg-gray-50/50 p-3 text-xs dark:border-white/[0.06] dark:bg-white/[0.03]"
		>
			<div class="flex justify-between gap-4">
				<span>{$i18n.t('Memories in file')}</span>
				<span class="font-medium text-gray-700 dark:text-gray-300">{importPreview?.total ?? 0}</span
				>
			</div>
			<div class="mt-1 flex justify-between gap-4">
				<span>{$i18n.t('New memories')}</span>
				<span class="font-medium text-gray-700 dark:text-gray-300"
					>{importPreview?.imported ?? 0}</span
				>
			</div>
			<div class="mt-1 flex justify-between gap-4">
				<span>{$i18n.t('Duplicates skipped')}</span>
				<span class="font-medium text-gray-700 dark:text-gray-300"
					>{importPreview?.skipped ?? 0}</span
				>
			</div>
		</div>
		<div class="mt-2 text-xs text-gray-400">
			{$i18n.t('Existing memories will not be overwritten.')}
		</div>
	</div>
</ConfirmDialog>

<ConfirmDialog
	title={$i18n.t('Clear Memory')}
	message={$i18n.t(
		'Are you sure you want to clear all memories? They will move to Deleted and can be restored from the Deleted filter.'
	)}
	show={showClearConfirmDialog}
	on:confirm={onClearConfirmed}
	on:cancel={() => (showClearConfirmDialog = false)}
/>

<ConfirmDialog
	title={$i18n.t('Delete Memory?')}
	show={showDeleteConfirm}
	on:confirm={async () => {
		if (!selectedMemory) return;
		const res = await deleteMemoryById(localStorage.token, selectedMemory.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (res) {
			toast.success($i18n.t('Memory deleted successfully'));
			await loadMemories();
		}
		showDeleteConfirm = false;
	}}
	on:cancel={() => (showDeleteConfirm = false)}
>
	<div class="flex-1 text-sm text-gray-500">
		{$i18n.t(
			'Are you sure you want to delete this memory? It will move to Deleted and can be restored from its history.'
		)}
		<div
			class="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-gray-100/50 bg-gray-50/40 p-2 text-xs text-gray-600 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-400"
		>
			{selectedMemory?.content}
		</div>
	</div>
</ConfirmDialog>

<MemoryModal
	bind:show={showMemoryModal}
	memory={selectedMemory}
	on:save={async () => {
		await loadMemories();
	}}
	on:conflict={async () => {
		selectedMemory = null;
		await loadMemories();
	}}
/>
