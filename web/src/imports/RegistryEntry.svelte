<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import type { Dataset, Permissions } from '../api/types.ts';
  import { events } from '../chat/events.ts';
  import { canImport, wizardPath } from './model.ts';

  let { dataset, permissions, navigate }: {
    dataset: Dataset;
    permissions: Permissions;
    navigate: (path: string) => void;
  } = $props();
  let count = $state<number | null>(null);
  let error = $state('');

  onMount(() => {
    if (!(permissions.datasets['*']?.includes('view') || permissions.datasets[dataset.id]?.includes('view'))) return;
    let request: AbortController;
    async function refresh() {
      request?.abort();
      const next = new AbortController();
      request = next;
      try {
        const result = await api.countRecords(dataset.id, {}, next.signal);
        if (!next.signal.aborted) {
          count = result.count;
          error = '';
        }
      } catch (e) {
        if (!next.signal.aborted) error = problemText(e);
      }
    }
    const off = events.subscribe('record', (changes) => {
      if (changes.some((c) => c.dataset_id === dataset.id)) void refresh();
    });
    const resume = events.subscribe('resync', refresh);
    void refresh();
    return () => {
      request?.abort();
      off();
      resume();
    };
  });
</script>

<h2>{dataset.name}</h2>
{#if count !== null}<p>Записей · {count}</p>{/if}
{#if !dataset.archived_at && (canImport(permissions, dataset.id) || permissions.sections.includes('structure'))}
  <button onclick={() => navigate(wizardPath(dataset.id))}>Импорт из Excel</button>
{/if}
{#if error}<p role="alert">{error}</p>{/if}
