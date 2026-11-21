<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import { scopeLabel, type Note, type Source, type Scope } from './model.ts';
  import SourcePicker from './SourcePicker.svelte';
  import SourceLink from './SourceLink.svelte';
  import KksPicker from './KksPicker.svelte';
  let {
    note,
    source,
    saved,
    cancel,
  }: { note?: Note; source?: Source; saved: (n: Note) => void; cancel: () => void } = $props();
  let title = $state(''),
    body = $state(''),
    sources = $state<Source[]>([]);
  let scopeKind = $state<Scope['kind']>('station'),
    scopeId = $state(''),
    code = $state('');
  let types = $state<{ id: string; name: string }[]>([]),
    busy = $state(false),
    error = $state(''),
    version = 0;
  let live = true;
  onDestroy(() => {
    live = false;
  });
  onMount(() => {
    title = note?.title ?? '';
    body = note?.body ?? '';
    version = note?.version ?? 0;
    sources = note?.sources ?? (source ? [source] : []);
    scopeKind = note?.scope.kind ?? (source?.kind === 'kks' ? 'kks' : 'station');
    scopeId = note?.scope.id ?? (source?.kind === 'kks' ? source.id : '');
    code = note?.scope.code ?? '';
  });
  $effect(() => {
    if (note || scopeKind !== 'type') return;
    const c = new AbortController();
    void api
      .listTypes(c.signal)
      .then((data) => {
        if (!c.signal.aborted) types = data.items;
      })
      .catch((e) => {
        if (!c.signal.aborted) error = problemText(e);
      });
    return () => c.abort();
  });
  async function save() {
    if (busy) return;
    busy = true;
    error = '';
    const fields = {
      title: title.trim(),
      body: body.trim(),
      sources: sources.map(({ kind, id, page }) => ({ kind, id, page })),
    };
    try {
      const result = note
        ? await api.knowledge.edit(note.id, { ...fields, version })
        : await api.knowledge.create({
            ...fields,
            scope: { kind: scopeKind, id: scopeKind === 'station' ? null : scopeId },
          });
      if (live) saved(result);
    } catch (e) {
      error = problemText(e);
    } finally {
      busy = false;
    }
  }
</script>

<form
  class="access-panel editor"
  onsubmit={(e) => {
    e.preventDefault();
    void save();
  }}
>
  <h2>{note ? 'Исправить заметку' : 'Предложить заметку'}</h2>
  <label>Заголовок<input required maxlength="200" bind:value={title} /></label>
  <label>Текст заметки<textarea required rows="5" bind:value={body}></textarea></label>
  {#if note}<p>Область · {scopeLabel(note.scope)}</p>{:else}
    <label
      >Область заметки<select
        aria-label="Область заметки"
        bind:value={scopeKind}
        onchange={() => {
          scopeId = '';
          code = '';
        }}
      >
        <option value="station">Станция</option><option value="kks">Объект ККС</option><option
          value="type">Тип данных</option
        ></select
      ></label
    >
    {#if scopeKind === 'kks'}
      <KksPicker
        label="Найти объект ККС"
        select={(item) => {
          scopeId = item.id;
          code = item.code;
        }}
      />
      {#if scopeId}<SourceLink source={{ kind: 'kks', id: scopeId, code }} />{/if}
    {:else if scopeKind === 'type'}<label
        >Тип данных<select required aria-label="Тип данных" bind:value={scopeId}>
          <option value="">Выберите тип</option>{#each types as t}<option value={t.id}
              >{t.name}</option
            >{/each}</select
        ></label
      >{/if}
  {/if}
  {#if sources.length < 20}
    <SourcePicker selected={sources} add={(item) => (sources = [...sources, item])} />
  {/if}
  {#if sources.length}<h3>Источники</h3>
    {#each sources as item}<div class="row-actions">
        <SourceLink source={item} />
        <button type="button" onclick={() => (sources = sources.filter((s) => s !== item))}
          >Убрать источник</button
        >
      </div>{/each}{/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  <div class="form-actions">
    <button
      class="primary"
      disabled={busy ||
        !title.trim() ||
        !body.trim() ||
        (!note && scopeKind !== 'station' && !scopeId)}>Отправить на проверку</button
    >
    <button type="button" disabled={busy} onclick={cancel}>Отмена</button>
  </div>
</form>
