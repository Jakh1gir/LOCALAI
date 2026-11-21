<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError } from '../api/client.ts';
  import { showTime, problemText } from '../admin/model.ts';
  import ConfirmAction from '../admin/ConfirmAction.svelte';
  import Links from './Links.svelte';
  import Versions from './Versions.svelte';
  import {
    documentUrl,
    states,
    watchDocuments,
    type Detail,
    type Chunk,
    type Mention,
  } from './model.ts';
  import './documents.css';

  let { id, page, canAudit }: { id: string; page: string; canAudit: boolean } = $props();
  let doc = $state<Detail | null>(null),
    chunks = $state<Chunk[]>([]),
    mentions = $state<Mention[]>([]);
  let readError = $state(''),
    actionError = $state(''),
    revision = $state(0);
  let busy = $state(false),
    archive = $state(false),
    file = $state<File | undefined>(),
    baseVersion = $state(0);
  let picker = $state<HTMLInputElement>(),
    conflict = $state(false);
  let live = true;
  onDestroy(() => {
    live = false;
  });
  $effect(() => {
    const key = id,
      selected = page || undefined;
    void revision;
    return watchDocuments(
      async (signal) => {
        const [detail, text, found] = await Promise.all([
          api.documents.get(key, signal),
          api.documents.text(key, selected, signal),
          api.documents.mentions(key, signal),
        ]);
        return { detail, text, found };
      },
      ({ detail, text, found }) => {
        if (text.version !== detail.version) {
          revision++;
          return;
        }
        doc = detail;
        chunks = text.items;
        mentions = found.items;
        readError = '';
      },
      (message) => {
        doc = null;
        chunks = [];
        mentions = [];
        readError = message;
      },
      key,
      (result) => ['queued', 'reading'].includes(result.detail.state),
    );
  });
  let pages = $derived([...new Set(chunks.map((chunk) => chunk.page))]);
  async function run(action: () => Promise<unknown>) {
    if (busy) return false;
    busy = true;
    actionError = '';
    try {
      await action();
      if (live) revision++;
      return true;
    } catch (cause) {
      if (live) {
        actionError = problemText(cause);
        conflict = cause instanceof ApiError && cause.code === 'version_conflict';
      }
      return false;
    } finally {
      if (live) busy = false;
    }
  }
  function resetFile() {
    file = undefined;
    conflict = false;
    if (picker) picker.value = '';
  }
  async function replace() {
    const selected = file;
    if (selected && (await run(() => api.documents.replace(id, selected, baseVersion)))) resetFile();
  }
</script>

<section class="access doc-work">
  <a href="/documents">Все документы</a>
  {#if readError}<p role="alert" class="form-error">{readError}</p>
    <button onclick={() => revision++}>Обновить карточку</button>
  {:else if !doc}<p role="status">Загрузка карточки…</p>
  {:else}
    <div class="access-panel editor">
      <h2>{doc.title}</h2>
      <p>
        {doc.type.name} · Версия {doc.version} · {showTime(doc.updated_at)}{doc.archived
          ? ' · В архиве'
          : ''}
      </p>
      <p role="status">{states[doc.state]}{doc.error ? ' · ' + doc.error : ''}</p>
      <a download href={'/api/v1/documents/' + encodeURIComponent(id) + '/file'}
        >Скачать файл · {doc.file_name}</a
      >
      {#if canAudit}<Versions {id} version={doc.version} />{/if}
      {#if doc.can.edit}
        <div class="doc-controls">
          <label
            >Новый файл<input
              type="file"
              bind:this={picker}
              disabled={busy}
              onchange={(e) => {
                file = e.currentTarget.files?.[0];
                baseVersion = doc?.version ?? 0;
                conflict = false;
              }}
            /></label
          >
          <button disabled={!file || busy || conflict} onclick={replace}>Заменить файл</button>
          <button disabled={busy} onclick={() => (archive = true)}>В архив</button>
        </div>
      {/if}
      {#if actionError}<p class="form-error" role="alert">{actionError}</p>{/if}
      {#if conflict}<button
          onclick={() => {
            resetFile();
            actionError = '';
            revision++;
          }}>Обновить карточку</button
        >{/if}
    </div>
    <Links {doc} {busy} {run} />
    <section class="access-panel editor">
      <h3>Упоминания ККС</h3>
      {#each mentions as m (m.kks_id)}
        <div>
          <strong>{m.code}</strong> · Вхождений: {m.hits}
          <nav aria-label={'Страницы для ' + m.code} class="doc-pages">
            {#each m.pages as p}<a href={documentUrl(id, p)}>Стр. {p}</a>{/each}
          </nav>
          {#if m.state === 'proposed' && doc.can.edit}<div class="row-actions">
              <button
                disabled={busy}
                onclick={() => run(() => api.documents.decide(id, m.kks_id, 'confirm'))}
                >Подтвердить</button
              >
              <button
                disabled={busy}
                onclick={() => run(() => api.documents.decide(id, m.kks_id, 'reject'))}
                >Отклонить</button
              >
            </div>
          {:else}<span
              >{{ proposed: 'Предложено', confirmed: 'Подтверждено', rejected: 'Отклонено' }[
                m.state
              ]}</span
            >{/if}
        </div>
      {:else}<p>Упоминаний нет</p>{/each}
    </section>
    <section class="access-panel editor">
      <h3>Текст по страницам</h3>
      {#if page}<a href={documentUrl(id)}>Все страницы</a>
      {:else if pages.length > 1}<nav class="doc-pages" aria-label="Страницы документа">
          {#each pages as p}<a href={documentUrl(id, p)}>Стр. {p}</a>{/each}
        </nav>{/if}
      {#each chunks as chunk, index (chunk.ordinal)}
        {#if !index || chunks[index - 1].page !== chunk.page}<h4>Страница {chunk.page}</h4>{/if}
        <p class="doc-page">{chunk.text}</p>
      {:else}<p>
          {page ? 'На выбранной странице нет текста' : 'Прочитанный текст пока отсутствует'}
        </p>{/each}
    </section>
    {#if archive}<ConfirmAction
        title="Переместить документ в архив?"
        consequence="Документ исчезнет из поиска, чтение файла остановится."
        action="В архив"
        onCancel={() => (archive = false)}
        onConfirm={() => {
          archive = false;
          void run(() => api.documents.archive(id));
        }}
      />{/if}
  {/if}
</section>
