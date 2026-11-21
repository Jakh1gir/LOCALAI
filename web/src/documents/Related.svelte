<script lang="ts">
  import { api } from '../api/client.ts';
  import { documentUrl, watchDocuments, type Kind, type Brief } from './model.ts';
  let { kind, id }: { kind: Kind; id: string } = $props();
  let items = $state<Brief[]>([]),
    error = $state(''),
    more = $state(false),
    loaded = $state(false);
  $effect(() => {
    const query = { target_kind: kind, target_id: id };
    return watchDocuments(
      (signal) => api.documents.list(query, signal),
      (result) => {
        items = result.items;
        more = result.more;
        error = '';
        loaded = true;
      },
      (message) => {
        items = [];
        error = message;
      },
    );
  });
</script>

<section aria-label="Связанные документы">
  <h3>Связанные документы</h3>
  {#if error}<p role="alert">{error}</p>{:else if items.length}
    <ul>
      {#each items as item}<li>
          <a href={documentUrl(item.id)}>{item.title}</a> · {item.type.name}
        </li>{/each}
    </ul>
    {#if more}<p>Показаны первые 500 документов</p>{/if}
  {:else if loaded}<p>Привязок нет</p>{/if}
</section>
