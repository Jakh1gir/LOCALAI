<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Dataset, Permissions } from '../api/types.ts';
  import { problemText } from '../admin/model.ts';
  import { events } from '../chat/events.ts';
  import LiveRows from './LiveRows.svelte';

  let {
    datasetId,
    permissions,
    navigate,
  }: { datasetId: string; permissions: Permissions; navigate: (path: string) => void } = $props();
  let datasets = $state<Dataset[]>([]);
  let problem = $state('');
  let current = $derived(datasets.find((d) => d.id === datasetId));

  onMount(() => {
    let request: AbortController;
    async function load() {
      request?.abort();
      const next = new AbortController();
      request = next;
      try {
        const data = await api.listDatasets(undefined, next.signal);
        if (!next.signal.aborted) {
          datasets = data.items;
          problem = '';
        }
      } catch (error) {
        if (!next.signal.aborted) problem = problemText(error);
      }
    }
    const off = events.subscribe('dataset', load);
    const resume = events.subscribe('resync', load);
    void load();
    return () => {
      request?.abort();
      off();
      resume();
    };
  });
</script>

<section class="access registry-live">
  <label
    >Реестр<select
      value={datasetId}
      onchange={(e) =>
        navigate('/registries?' + new URLSearchParams({ dataset: e.currentTarget.value }))}
    >
      <option value="">Выберите реестр</option>
      {#each datasets as d}<option value={d.id}>{d.name}</option>{/each}
    </select></label
  >
  {#if problem}<p role="alert">{problem}</p>{/if}
  {#if current}{#key current.id}<LiveRows dataset={current} {permissions} {navigate} />{/key}{/if}
</section>
