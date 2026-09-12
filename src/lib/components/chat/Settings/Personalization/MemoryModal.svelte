<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from 'svelte-sonner';

	import {
		addNewMemory,
		MemoryApiError,
		updateMemoryById,
		type MemoryItem
	} from '$lib/apis/memories';

	import Modal from '$lib/components/common/Modal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const dispatch = createEventDispatcher();

	export let show = false;
	export let memory: MemoryItem | null = null;

	const i18n = getContext<Writable<i18nType>>('i18n');

	let loading = false;
	let content = '';
	let type: 'user' | 'context' = 'user';
	let path = '';

	$: edit = !!memory?.id;
	$: if (show) {
		content = memory?.content ?? '';
		type = memory?.type ?? 'user';
		path = memory?.path ?? '';
	}

	const submitHandler = async () => {
		const currentMemory = memory;
		if (!content.trim() || (edit && !currentMemory)) return;

		loading = true;
		const res = await (
			currentMemory
				? updateMemoryById(
						localStorage.token,
						currentMemory.id,
						content,
						type,
						path,
						currentMemory.version
					)
				: addNewMemory(localStorage.token, content, type, path)
		).catch((error) => {
			if (error instanceof MemoryApiError && error.status === 409) {
				toast.error(
					$i18n.t(
						'This memory changed elsewhere. The latest version has been reloaded; review it before editing again.'
					)
				);
				show = false;
				dispatch('conflict', { id: currentMemory?.id });
			} else {
				toast.error(`${error}`);
			}
			return null;
		});

		if (res) {
			toast.success(
				edit ? $i18n.t('Memory updated successfully') : $i18n.t('Memory added successfully')
			);
			show = false;
			dispatch('save');
		}

		loading = false;
	};
</script>

<Modal bind:show size="sm">
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-5 pt-4 pb-2">
			<div class=" text-lg font-medium self-center">
				{#if edit}
					{$i18n.t('Edit Memory')}
				{:else}
					{$i18n.t('Add Memory')}
				{/if}
			</div>
			<button class="self-center" on:click={() => (show = false)}>
				<XMark className={'size-5'} />
			</button>
		</div>

		<div class="flex flex-col md:flex-row w-full px-4 pb-4 md:space-x-4 dark:text-gray-200">
			<div class=" flex flex-col w-full sm:flex-row sm:justify-center sm:space-x-6">
				<form class="flex flex-col w-full" on:submit|preventDefault={submitHandler}>
					<div class="px-1">
						<fieldset class="mb-2">
							<legend class="mb-1.5 text-xs text-gray-500">{$i18n.t('Type')}</legend>
							<div class="grid grid-cols-2 gap-2">
								<label
									class="cursor-pointer rounded-lg border px-3 py-2 text-xs transition {type ===
									'user'
										? 'border-gray-300 bg-gray-50 text-gray-900 dark:border-gray-600 dark:bg-white/[0.06] dark:text-white'
										: 'border-gray-100 text-gray-500 dark:border-white/[0.06] dark:text-gray-400'}"
								>
									<input
										class="sr-only"
										type="radio"
										name="memory-type"
										value="user"
										bind:group={type}
									/>
									<span class="block font-medium">{$i18n.t('About me')}</span>
									<span class="mt-0.5 block text-[0.6875rem] opacity-70"
										>{$i18n.t('Preferences and stable personal details')}</span
									>
								</label>
								<label
									class="cursor-pointer rounded-lg border px-3 py-2 text-xs transition {type ===
									'context'
										? 'border-gray-300 bg-gray-50 text-gray-900 dark:border-gray-600 dark:bg-white/[0.06] dark:text-white'
										: 'border-gray-100 text-gray-500 dark:border-white/[0.06] dark:text-gray-400'}"
								>
									<input
										class="sr-only"
										type="radio"
										name="memory-type"
										value="context"
										bind:group={type}
									/>
									<span class="block font-medium">{$i18n.t('Context')}</span>
									<span class="mt-0.5 block text-[0.6875rem] opacity-70"
										>{$i18n.t('Reusable project or task information')}</span
									>
								</label>
							</div>
						</fieldset>

						<textarea
							bind:value={content}
							class="bg-transparent w-full text-sm outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
							rows="6"
							style="resize: vertical;"
							placeholder={type === 'user'
								? $i18n.t('Add a preference, fact, or instruction about you')
								: $i18n.t('Add durable context for future chats')}
						></textarea>

						<div class="flex flex-col w-full mt-1.5">
							<label for="memory-path" class="mb-0.5 text-xs text-gray-500">
								{$i18n.t('Category')}
								<span class="opacity-50">({$i18n.t('optional')})</span>
							</label>

							<input
								id="memory-path"
								bind:value={path}
								class="w-full text-sm bg-transparent outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
								placeholder={type === 'user'
									? $i18n.t('For example: preferences/work')
									: $i18n.t('For example: projects/website')}
								autocomplete="off"
							/>
						</div>
					</div>

					<div class="flex justify-end pt-1 text-sm font-medium">
						<button
							class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-2 whitespace-nowrap {loading
								? ' cursor-not-allowed'
								: ''}"
							type="submit"
							disabled={loading}
						>
							{#if edit}
								{$i18n.t('Update')}
							{:else}
								{$i18n.t('Add')}
							{/if}

							{#if loading}
								<span class="shrink-0">
									<Spinner />
								</span>
							{/if}
						</button>
					</div>
				</form>
			</div>
		</div>
	</div>
</Modal>
