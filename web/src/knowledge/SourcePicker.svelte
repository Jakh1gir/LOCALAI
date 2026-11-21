<script lang="ts">
  import { api } from '../api/client.ts';
  import { observe, type Source } from './model.ts';
  let { selected, add }: { selected: Source[]; add: (source: Source) => void } = $props();
  let q = $state(''),
    error = $state(''),
    choices = $state<{ source: Source; label: string }[]>([]);
  $effect(() => {
    const needle = q.trim();
    choices = [];
    error = '';
    if (needle)
      return observe(
        'knowledge_note',
        (s) => api.knowledge.search(needle, s),
        (r) => {
          choices = [
            ...r.kks.map((k) => ({
              label: k.code,
              source: { kind: 'kks' as const, id: k.id, code: k.code },
            })),
            ...r.documents.map((d) => ({
              label: d.title + ' · стр. ' + d.page,
              source: {
                kind: 'document' as const,
                id: d.document_id,
                page: d.page,
                title: d.title,
              },
            })),
            ...r.records.map((row) => ({
              label: row.dataset + ' · ' + row.label,
              source: { kind: 'record' as const, id: row.id },
            })),
          ];
        },
        (msg) => (error = msg),
        180,
      );
  });
</script>

<label
  >Добавить источник<input
    type="search"
    placeholder="ККС, запись или текст документа"
    maxlength="200"
    bind:value={q}
  /></label
>
{#if error}<p role="alert">{error}</p>{/if}
{#if choices.length}<ul class="knowledge-options">
    {#each choices as c}<li>
        <button
          type="button"
          disabled={selected.some((s) => s.kind === c.source.kind && s.id === c.source.id)}
          onclick={() => {
            add(c.source);
            q = '';
          }}>{c.label}</button
        >
      </li>{/each}
  </ul>{/if}
