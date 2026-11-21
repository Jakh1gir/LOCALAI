<script lang="ts">
  import { api } from '../api/client.ts';
  import { showTime, problemText } from '../admin/model.ts';
  import {
    knowledgeUrl,
    sourceFrom,
    scopeLabel,
    states,
    observe,
    type Scope,
    type Page,
    type Queue,
  } from './model.ts';
  import Editor from './Editor.svelte';
  import Sources from './Sources.svelte';
  import Card from './Card.svelte';
  import Glossary from './Glossary.svelte';
  import KksPicker from './KksPicker.svelte';
  import './knowledge.css';
  let {
    query,
    manage,
    user,
    queue,
    queueError,
    navigate,
  }: {
    query: string;
    manage: boolean;
    user: string;
    queue: Queue | null;
    queueError: string;
    navigate: (url: string) => void;
  } = $props();
  let params = $derived(new URLSearchParams(query)),
    tab = $derived(params.get('tab') || 'notes');
  let noteId = $derived(params.get('note')),
    creating = $derived(params.has('new'));
  let search = $state(''),
    kind = $state<Scope['kind'] | ''>(''),
    scopeId = $state(''),
    label = $state('');
  let types = $state<{ id: string; name: string }[]>([]),
    result = $state<Page | null>(null),
    error = $state(''),
    typeError = $state('');
  $effect(() => {
    if (tab !== 'notes' || noteId || creating) return;
    const q = search.trim(),
      scope_kind = kind || undefined,
      scope_id = scopeId || undefined;
    return observe(
      'knowledge_note',
      (s) => api.knowledge.notes({ q, scope_kind, scope_id }, s),
      (data) => {
        result = data;
        error = '';
      },
      (msg) => {
        result = null;
        error = msg;
      },
      120,
    );
  });
  $effect(() => {
    if (kind !== 'type') return;
    const c = new AbortController();
    void api
      .listTypes(c.signal)
      .then((data) => {
        if (!c.signal.aborted) types = data.items;
      })
      .catch((e) => {
        if (!c.signal.aborted) typeError = problemText(e);
      });
    return () => c.abort();
  });
  let list = $derived(tab === 'queue' ? queue : result);
  function follow(event: MouseEvent) {
    if (
      event.defaultPrevented ||
      event.button ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey
    )
      return;
    const link = event.target instanceof Element ? event.target.closest('a') : null;
    if (link?.origin === location.origin && link.pathname === '/knowledge' && !link.target) {
      event.preventDefault();
      navigate(link.pathname + link.search);
    }
  }
</script>

<svelte:window onclick={follow} />

<section class="access knowledge">
  <nav class="knowledge-toolbar" aria-label="Разделы знаний">
    <a href="/knowledge">Заметки</a>
    {#if manage}<a href={knowledgeUrl({ tab: 'queue' })}>Очередь проверки</a>{/if}
    <a href={knowledgeUrl({ tab: 'glossary' })}>Словарь станции</a>
  </nav>
  {#if creating}
    <Editor
      source={sourceFrom(params)}
      saved={(note) => navigate(knowledgeUrl({ note: note.id }))}
      cancel={() => navigate('/knowledge')}
    />
  {:else if noteId}<Card id={noteId} {manage} {user} />
  {:else if tab === 'glossary'}<Glossary {manage} initial={params.get('q') ?? ''} />
  {:else if tab === 'queue' && !manage}<p>Доступ к очереди ограничен</p>
  {:else}
    {#if tab === 'queue'}<h2>Очередь проверки</h2>
      {#if queue}<p>
          Предложены · {queue.items.filter((n) => n.state === 'proposed').length} · Устарели · {queue.items.filter(
            (n) => n.state === 'stale',
          ).length}{queue.more ? ' · показана часть очереди' : ''}
        </p>{/if}
    {:else}<div class="knowledge-toolbar">
        <h2>Подтверждённые заметки</h2>
        <a href={knowledgeUrl({ new: '1' })}>Предложить заметку</a>
      </div>
      <div class="knowledge-toolbar">
        <label>Поиск по заметкам<input type="search" bind:value={search} /></label>
        <label
          >Область<select
            aria-label="Фильтр по области"
            bind:value={kind}
            onchange={() => {
              scopeId = '';
              label = '';
            }}
          >
            <option value="">Все области</option><option value="station">Станция</option><option
              value="kks">ККС</option
            ><option value="type">Тип данных</option></select
          ></label
        >
        {#if kind === 'type'}<label
            >Тип данных<select aria-label="Фильтр по типу" bind:value={scopeId}
              ><option value="">Все типы</option>{#each types as t}<option value={t.id}
                  >{t.name}</option
                >{/each}</select
            ></label
          >{/if}
        {#if kind === 'kks'}<KksPicker
            label="Фильтр по ККС"
            select={(k) => {
              scopeId = k.id;
              label = k.code;
            }}
          />{#if scopeId}<button
              onclick={() => {
                scopeId = '';
                label = '';
              }}>{label} ×</button
            >{/if}{/if}
      </div>{/if}
    {#if typeError || (tab === 'queue' ? queueError : error)}<p role="alert">
        {typeError || (tab === 'queue' ? queueError : error)}
      </p>
    {:else if list}<ul class="knowledge-list">
        {#each list.items as note (note.id)}
          <li class="access-panel editor">
            <h3><a href={knowledgeUrl({ note: note.id })}>{note.title}</a></h3>
            <p>{states[note.state]} · {scopeLabel(note.scope)} · {showTime(note.updated_at)}</p>
            {#if note.stale_reason}<p>
                {note.stale_reason}
              </p>{/if}{#if tab === 'queue' && note.state === 'stale'}<Sources
                sources={note.sources}
              />{/if}
          </li>
        {:else}<li>{tab === 'queue' ? 'Очередь пуста' : 'Заметки не найдены'}</li>{/each}
      </ul>
      {#if list.more}<p>Показаны первые {list.items.length} заметок.</p>{/if}
    {:else}<p role="status">Загрузка…</p>{/if}
  {/if}
</section>
