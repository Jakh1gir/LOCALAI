<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import type { Dataset, Me } from '../api/types.ts';
  import { canImport, registryPath, wizardPath } from './model.ts';
  let { id, me, navigate }: { id: string; me: Me; navigate: (p: string) => void } = $props();
  let items = $state<Dataset[]>([]),
    error = $state(''),
    count = $state<number | null>(null);
  let current = $derived(items.find((d) => d.id === id));
  onMount(() => {
    const c = new AbortController();
    api
      .listDatasets(undefined, c.signal)
      .then((p) => (items = p.items.filter((d) => !d.archived_at)))
      .catch((e) => {
        if (!c.signal.aborted) error = problemText(e);
      });
    return () => c.abort();
  });
  $effect(() => {
    const selected = current;
    count = null;
    if (
      !selected ||
      !(
        me.permissions.datasets['*']?.includes('view') ||
        me.permissions.datasets[selected.id]?.includes('view')
      )
    )
      return;
    const c = new AbortController();
    api
      .countRecords(selected.id, {}, c.signal)
      .then((p) => {
        if (!c.signal.aborted) count = p.count;
      })
      .catch((e) => {
        if (!c.signal.aborted) error = problemText(e);
      });
    return () => c.abort();
  });
</script>

<div class="access import-ui">
  <label>
    Реестр
    <select
      aria-label="Реестр"
      value={id}
      onchange={(e) => navigate(registryPath(e.currentTarget.value))}
    >
      <option value="">Выберите реестр</option>
      {#each items as d}<option value={d.id}>{d.name}</option>{/each}
    </select>
  </label>
  {#if current}<h2>{current.name}</h2>
    {#if count !== null}<p>Записей · {count}</p>{/if}
    {#if canImport(me.permissions, current.id) || me.permissions.sections.includes('structure')}<button
        onclick={() => navigate(wizardPath(current!.id))}
      >
        Импорт из Excel
      </button>{/if}{/if}
  {#if error}<p role="alert">{error}</p>{/if}
</div>
