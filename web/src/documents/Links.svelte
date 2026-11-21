<script lang="ts">
  import { api } from '../api/client.ts';
  import { kksUrl, recordUrl } from '../search/target.ts';
  import { lookup } from './search.svelte.ts';
  import type { Detail, Kind } from './model.ts';
  let {
    doc,
    busy,
    run,
  }: { doc: Detail; busy: boolean; run: (action: () => Promise<unknown>) => Promise<boolean> } =
    $props();
  let query = $state('');
  const matches = lookup(() => query);
  async function add(kind: Kind, id: string) {
    if (await run(() => api.documents.link(doc.id, kind, id))) query = '';
  }
</script>

<section class="access-panel editor">
  <h3>Привязки</h3>
  <ul class="doc-links">
    {#each doc.links as link}
      <li>
        {#if !link.access}Нет доступа
        {:else if link.kind === 'kks'}<a href={kksUrl(link.id)}>{link.code ?? 'Объект ККС'}</a>
        {:else if link.dataset_id}<a href={recordUrl(link.dataset_id, link.id)}
            >Запись · {link.dataset}</a
          >{/if}
        {#if doc.can.edit && link.access}<button
            disabled={busy}
            onclick={() => run(() => api.documents.unlink(doc.id, link.kind, link.id))}
            >Снять привязку</button
          >{/if}
      </li>
    {:else}<li>Привязок нет</li>{/each}
  </ul>
  {#if doc.can.edit}
    <label
      >Найти ККС или запись для привязки<input
        type="search"
        maxlength="200"
        bind:value={query}
      /></label
    >
    {#if matches.error}<p role="alert">{matches.error}</p>{/if}
    {#if matches.result}
      <ul class="doc-links">
        {#each matches.result.kks as obj}<li>
            {obj.code} · {obj.site}
            <button disabled={busy} onclick={() => add('kks', obj.id)}>Привязать ККС</button>
          </li>{/each}
        {#each matches.result.records as row}<li>
            {row.label} · {row.dataset}
            <button disabled={busy} onclick={() => add('record', row.id)}>Привязать запись</button>
          </li>{/each}
        {#if !matches.result.kks.length && !matches.result.records.length}<li>Совпадений нет</li>{/if}
      </ul>
    {/if}
  {/if}
</section>
