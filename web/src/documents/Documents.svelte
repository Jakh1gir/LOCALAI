<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText, showTime } from '../admin/model.ts';
  import { documentUrl, states, watchDocuments, type Brief } from './model.ts';
  import ReadError from './ReadError.svelte';
  import './documents.css';

  let { canEdit, navigate }: { canEdit: boolean; navigate: (url: string) => void } = $props();
  let docs = $state<Brief[]>([]);
  let types = $state<{ id: string; name: string }[]>([]);
  let filter = $state(''),
    stateFilter = $state(''),
    archived = $state(false);
  let uploadType = $state(''),
    title = $state(''),
    files = $state<FileList>();
  let problem = $state(''),
    uploadError = $state(''),
    typeError = $state('');
  let busy = $state(false),
    loading = $state(true),
    more = $state(false);
  let limit = $state(50);
  let filtered = $derived(docs.filter((item) => !filter || item.type.id === filter));
  let live = true;
  onDestroy(() => {
    live = false;
  });
  onMount(() => {
    const request = new AbortController();
    void api.documents
      .types(request.signal)
      .then(({ items }) => {
        types = items;
        uploadType = items[0]?.id ?? '';
      })
      .catch((error) => {
        if (!request.signal.aborted) typeError = problemText(error);
      });
    return () => request.abort();
  });
  $effect(() => {
    const query = { archived: String(archived), state: stateFilter || undefined };
    loading = true;
    return watchDocuments(
      (signal) => api.documents.list(query, signal),
      (list) => {
        docs = list.items;
        more = list.more;
        problem = '';
        loading = false;
      },
      (message) => {
        docs = [];
        problem = message;
        loading = false;
      },
      undefined,
      (result) => result.items.some((item) => ['queued', 'reading'].includes(item.state)),
    );
  });
  async function upload() {
    if (!files?.[0] || busy) return;
    busy = true;
    uploadError = '';
    try {
      const doc = await api.documents.upload(files[0], title.trim(), uploadType);
      if (live) navigate(documentUrl(doc.id));
    } catch (error) {
      uploadError = problemText(error);
    } finally {
      busy = false;
    }
  }
</script>

<section class="access doc-work">
  {#if typeError}<p class="form-error" role="alert">{typeError}</p>{/if}
  {#if canEdit && types.length}
    <form
      class="access-panel doc-controls"
      onsubmit={(e) => {
        e.preventDefault();
        void upload();
      }}
    >
      <label>Файл документа<input type="file" required bind:files /></label>
      <label>Название<input bind:value={title} placeholder="Имя файла" /></label>
      <label
        >Тип документа<select aria-label="Тип документа" bind:value={uploadType}
          >{#each types as t}<option value={t.id}>{t.name}</option>{/each}</select
        ></label
      >
      <button class="primary" disabled={busy || !files?.length}>Загрузить</button>
    </form>
  {/if}
  {#if uploadError}<p role="alert" class="form-error">{uploadError}</p>{/if}
  <div class="doc-controls">
    <label
      >Фильтр по типу<select aria-label="Фильтр по типу" bind:value={filter}
        ><option value="">Все типы</option>{#each types as t}<option value={t.id}>{t.name}</option
          >{/each}</select
      ></label
    >
    <label
      >Состояние чтения<select aria-label="Состояние чтения" bind:value={stateFilter}
        ><option value="">Все состояния</option>{#each Object.entries(states) as [key, label]}<option
            value={key}>{label}</option
          >{/each}</select
      ></label
    >
    <label class="check-label"><input type="checkbox" bind:checked={archived} />Архив</label>
  </div>
  {#if problem}<p class="form-error" role="alert">{problem}</p>
  {:else if loading}<p role="status">Загрузка документов…</p>
  {:else}
    <div class="access-panel table-scroll">
      <table class="access-table" aria-label="Список документов">
        <thead
          ><tr><th>Название</th><th>Тип</th><th>Состояние</th><th>Версия</th><th>Изменён</th></tr
          ></thead
        >
        <tbody
          >{#each filtered.slice(0, limit) as d (d.id)}
            <tr
              ><th><a href={documentUrl(d.id)}>{d.title}</a></th><td>{d.type.name}</td><td
                >{states[d.state]}
                {#if d.state === 'error'}{#key d.updated_at}<ReadError id={d.id} />{/key}{/if}</td
              ><td>{d.version}</td><td>{showTime(d.updated_at)}</td></tr
            >
          {:else}<tr><td colspan="5">Нет документов по выбранным условиям</td></tr>{/each}</tbody
        >
      </table>
    </div>
    {#if filtered.length > limit}<button onclick={() => (limit += 50)}>Показать ещё</button>{/if}
    {#if more}<p role="status">
        Сервер вернул первые 500 документов. Поиск по тексту доступен через Ctrl+K.
      </p>{/if}
  {/if}
</section>
