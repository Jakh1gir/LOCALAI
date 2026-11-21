<script lang="ts">
  import { api } from '../api/client.ts';
  import { observe, type Kks } from './model.ts';
  let { label, select }: { label: string; select: (item: Kks) => void } = $props();
  let term = $state(''),
    found = $state<Kks[]>([]),
    error = $state('');
  $effect(() => {
    const q = term.trim();
    found = [];
    error = '';
    if (q)
      return observe(
        'kks',
        (signal) => api.knowledge.search(q, signal),
        (data) => {
          found = data.kks;
        },
        (message) => (error = message),
        180,
      );
  });
</script>

<label>{label}<input type="search" maxlength="200" bind:value={term} /></label>
{#if error}<p role="alert">{error}</p>{/if}
{#if found.length}<ul class="knowledge-options">
    {#each found as item}<li>
        <button
          type="button"
          onclick={() => {
            select(item);
            term = '';
          }}>{item.code}{item.site ? ' · ' + item.site : ''}</button
        >
      </li>{/each}
  </ul>{/if}
