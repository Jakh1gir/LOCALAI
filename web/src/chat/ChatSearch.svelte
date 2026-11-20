<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import { events } from './events.ts';
  import MessageText from './MessageText.svelte';
  import type { Message } from './types.ts';
  let { jump }: { jump: (seq: number) => void } = $props();
  let query = $state('');
  let hits = $state<Message[] | null>(null);
  let problem = $state('');
  let request: AbortController | undefined;
  let searching = false;
  function clear() {
    searching = false;
    request?.abort();
    hits = null;
    problem = '';
  }
  const off = events.subscribe('chat_message', () => {
    if (searching) void search();
  });
  onDestroy(() => {
    off();
    request?.abort();
  });
  async function search() {
    clear();
    if (!query.trim()) return;
    searching = true;
    const current = new AbortController();
    request = current;
    try {
      const page = await api.findMessages(query.trim(), current.signal);
      if (!current.signal.aborted) hits = page.items;
    } catch (error) {
      if (!current.signal.aborted) problem = problemText(error);
    }
  }
</script>

<form
  class="chat-search"
  onsubmit={(e) => {
    e.preventDefault();
    void search();
  }}
>
  <input
    type="search"
    aria-label="Поиск по сообщениям"
    placeholder="Поиск по сообщениям"
    bind:value={query}
    oninput={clear}
  />
  <button disabled={!query.trim()}>Найти</button>
</form>
{#if problem}<p role="alert">{problem}</p>{/if}
{#if hits}
  <section class="chat-hits" aria-label="Результаты поиска">
    <button onclick={clear}>Закрыть поиск</button>
    {#each hits as hit}<div>
        <MessageText message={hit} /><button
          onclick={() => {
            clear();
            jump(hit.seq);
          }}>К сообщению</button
        >
      </div>
    {:else}<p>Сообщения не найдены</p>{/each}
  </section>
{/if}
