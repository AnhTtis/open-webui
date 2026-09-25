<script lang="ts">
	import { getAdminConfig, updateAdminConfig } from '$lib/apis/auths';
	import { getMemoryAdminHealth, type MemoryAdminHealth } from '$lib/apis/memories';
	import { getBackendConfig } from '$lib/apis';
	import { config } from '$lib/stores';
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import AdminSettingField from './AdminSettingField.svelte';
	import AdminSettingSection from './AdminSettingSection.svelte';

	const i18n: any = getContext('i18n');

	export let saveHandler: Function;

	let adminConfig: any = null;
	let health: MemoryAdminHealth | null = null;
	let loadingHealth = false;

	const inputClass =
		'w-full h-7 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors placeholder:text-gray-300 focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:placeholder:text-gray-700 dark:focus:border-blue-500';

	const countEntries = (value?: Record<string, number>) =>
		Object.entries(value ?? {}).sort(([left], [right]) => left.localeCompare(right));

	const loadHealth = async () => {
		loadingHealth = true;
		health = await getMemoryAdminHealth(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		loadingHealth = false;
	};

	const updateHandler = async () => {
		const payload = {
			...adminConfig,
			MEMORIES_MAX_ITEMS_PER_USER: Number(adminConfig.MEMORIES_MAX_ITEMS_PER_USER),
			MEMORIES_MAX_CONTENT_BYTES_PER_USER: Number(adminConfig.MEMORIES_MAX_CONTENT_BYTES_PER_USER)
		};
		const res = await updateAdminConfig(localStorage.token, payload);
		await config.set(await getBackendConfig());
		if (res) {
			adminConfig = { ...adminConfig, ...res };
			saveHandler();
		} else {
			toast.error($i18n.t('Failed to update settings'));
		}
	};

	onMount(async () => {
		adminConfig = await getAdminConfig(localStorage.token);
		await loadHealth();
	});
</script>

<form
	class="flex h-full flex-col justify-between text-sm"
	on:submit|preventDefault={async () => {
		updateHandler();
	}}
>
	<h2 class="mb-4 text-sm font-medium text-gray-900 dark:text-white">{$i18n.t('Memory')}</h2>

	<div class="min-h-0 flex-1 overflow-y-auto pr-1.5 scrollbar-hover">
		{#if adminConfig !== null}
			<AdminSettingSection title={$i18n.t('Per-user quotas')} first>
				<AdminSettingField
					label={$i18n.t('Maximum memories per user')}
					description={$i18n.t(
						'Canonical non-deleted memories counted toward each user. Soft-deleted items do not consume this quota.'
					)}
					forId="memories-max-items"
				>
					<input
						id="memories-max-items"
						class={inputClass}
						type="number"
						min="1"
						bind:value={adminConfig.MEMORIES_MAX_ITEMS_PER_USER}
					/>
				</AdminSettingField>
				<AdminSettingField
					label={$i18n.t('Maximum memory content bytes per user')}
					description={$i18n.t(
						'UTF-8 bytes of canonical content counted toward each user. Restore must reserve quota again.'
					)}
					forId="memories-max-bytes"
				>
					<input
						id="memories-max-bytes"
						class={inputClass}
						type="number"
						min="1024"
						bind:value={adminConfig.MEMORIES_MAX_CONTENT_BYTES_PER_USER}
					/>
				</AdminSettingField>
				<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
					{$i18n.t(
						'Administrators can change global limits and inspect aggregate health. Memory contents, paths, and user identifiers are not shown here.'
					)}
				</p>
			</AdminSettingSection>

			<AdminSettingSection title={$i18n.t('Health')}>
				<div class="mb-2 flex items-center justify-between gap-3">
					<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
						{$i18n.t('Aggregate counts only. This is not a liveness or readiness probe.')}
					</p>
					<button
						class="shrink-0 text-xs text-gray-500 transition-colors hover:text-gray-900 disabled:opacity-50 dark:text-gray-500 dark:hover:text-white"
						type="button"
						disabled={loadingHealth}
						on:click={loadHealth}
					>
						{$i18n.t('Refresh')}
					</button>
				</div>

				{#if loadingHealth && !health}
					<div class="flex min-h-16 items-center justify-center">
						<Spinner className="size-4" />
					</div>
				{:else if health}
					<div class="grid gap-3 sm:grid-cols-2">
						<div class="rounded-xl border border-gray-100/70 p-3 dark:border-white/[0.06]">
							<div class="text-[0.625rem] font-medium uppercase tracking-wide text-gray-400">
								{$i18n.t('Memory status')}
							</div>
							{#each countEntries(health.memory_status_counts) as [status, count] (status)}
								<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
									<span>{status}</span><span>{count}</span>
								</div>
							{/each}
						</div>
						<div class="rounded-xl border border-gray-100/70 p-3 dark:border-white/[0.06]">
							<div class="text-[0.625rem] font-medium uppercase tracking-wide text-gray-400">
								{$i18n.t('Jobs')}
							</div>
							{#each countEntries(health.job_status_counts) as [status, count] (status)}
								<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
									<span>{status}</span><span>{count}</span>
								</div>
							{/each}
							<div class="mt-2 flex justify-between text-xs text-gray-600 dark:text-gray-400">
								<span>{$i18n.t('Expired leases')}</span><span>{health.expired_leases}</span>
							</div>
							<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
								<span>{$i18n.t('Oldest pending age (s)')}</span>
								<span>{health.oldest_pending_age_seconds}</span>
							</div>
						</div>
						<div class="rounded-xl border border-gray-100/70 p-3 dark:border-white/[0.06]">
							<div class="text-[0.625rem] font-medium uppercase tracking-wide text-gray-400">
								{$i18n.t('Cleanup / transfer / generation')}
							</div>
							{#each countEntries(health.cleanup_status_counts) as [status, count] (`cleanup-${status}`)}
								<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
									<span>cleanup:{status}</span><span>{count}</span>
								</div>
							{/each}
							{#each countEntries(health.transfer_status_counts) as [status, count] (`transfer-${status}`)}
								<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
									<span>transfer:{status}</span><span>{count}</span>
								</div>
							{/each}
							{#each countEntries(health.generation_status_counts) as [status, count] (`generation-${status}`)}
								<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
									<span>generation:{status}</span><span>{count}</span>
								</div>
							{/each}
						</div>
						<div class="rounded-xl border border-gray-100/70 p-3 dark:border-white/[0.06]">
							<div class="text-[0.625rem] font-medium uppercase tracking-wide text-gray-400">
								{$i18n.t('Quota utilization')}
							</div>
							{#each countEntries(health.quota_utilization_buckets) as [bucket, count] (bucket)}
								<div class="mt-1 flex justify-between text-xs text-gray-600 dark:text-gray-400">
									<span>{bucket}</span><span>{count}</span>
								</div>
							{/each}
						</div>
					</div>
				{:else}
					<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
						{$i18n.t('Memory health is currently unavailable.')}
					</div>
				{/if}
			</AdminSettingSection>
		{:else}
			<div class="flex min-h-16 items-center justify-center">
				<Spinner className="size-4" />
			</div>
		{/if}
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
