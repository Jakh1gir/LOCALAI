<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import { events } from './events.ts';
  import ChatMessage from './ChatMessage.svelte';
  import ChatSearch from './ChatSearch.svelte';
  import type { ChatPage, Message } from './types.ts';
  import '../admin/admin.css';
  import './chat.css';

  let {
    canWrite,
    draft = $bindable(''),
    onRead,
  }: {
    canWrite: boolean;
    draft: string;
    onRead: () => void;
  } = $props();
  let rows = $state<Message[]>([]);
  let older = $state(false);
  let loaded = $state(false);
  let viewport: HTMLDivElement;
  let problem = $state('');
  let sendProblem = $state('');
  let sending = $state(false);
  let olderPending = false;
  let lastRead = 0;
  let marking = false;
  let pendingRead = false;
  let queue = Promise.resolve();
  const abort = new AbortController();
  let count = $derived(Array.from(draft).length);

  function enqueue(task: () => Promise<void>) {
    queue = queue.then(async () => {
      if (abort.signal.aborted) return;
      try {
        await task();
      } catch (error) {
        if (!abort.signal.aborted) problem = problemText(error);
      }
    });
    return queue;
  }

  function merge(page: ChatPage) {
    const items = new Map(rows.map((row) => [row.seq, row]));
    for (const msg of page.items) items.set(msg.seq, msg);
    rows = [...items.values()].sort((a, b) => a.seq - b.seq);
    lastRead = Math.max(lastRead, page.last_read);
  }

  function nearEnd() {
    return viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 5;
  }

  async function sync(changed: Set<string>) {
    problem = '';
    const follow = !loaded || nearEnd();
    if (!loaded) {
      const page = await api.messages({}, abort.signal);
      merge(page);
      older = page.more;
      loaded = true;
    } else {
      // У правки или скрытия старой строки seq не меняется. Запрашиваем её отдельно.
      for (const row of rows.filter((row) => changed.has(row.id))) {
        merge(await api.messages({ after: row.seq - 1, limit: 1 }, abort.signal));
      }
      let more;
      do {
        const page = await api.messages({ after: rows.at(-1)?.seq ?? 0 }, abort.signal);
        merge(page);
        more = page.more;
      } while (more && !abort.signal.aborted);
    }
    await tick();
    if (abort.signal.aborted) return;
    if (follow) viewport.scrollTop = viewport.scrollHeight;
    void markVisible();
  }

  let dirty = new Set<string>();
  let scheduled = false;
  let retryTimer: ReturnType<typeof setTimeout>;
  function refresh(ids: string[] = []) {
    ids.forEach((id) => dirty.add(id));
    if (scheduled) return;
    scheduled = true;
    void enqueue(async () => {
      scheduled = false;
      const batch = dirty;
      dirty = new Set();
      try {
        await sync(batch);
      } catch (error) {
        batch.forEach((id) => dirty.add(id));
        clearTimeout(retryTimer);
        if (!abort.signal.aborted) retryTimer = setTimeout(refresh, 1500);
        throw error;
      }
    });
  }

  async function prepend() {
    const first = rows[0];
    if (!older || !first) return;
    const page = await api.messages({ before: first.seq }, abort.signal);
    const anchor = document.getElementById('message-' + first.seq);
    const y = anchor?.getBoundingClientRect().top;
    merge(page);
    older = page.more;
    await tick();
    if (anchor && y !== undefined) viewport.scrollTop += anchor.getBoundingClientRect().top - y;
  }

  function loadOlder() {
    if (olderPending || !older) return;
    olderPending = true;
    void enqueue(prepend).finally(() => {
      olderPending = false;
    });
  }

  function jump(seq: number) {
    void enqueue(async () => {
      while (older && rows[0]?.seq > seq && !abort.signal.aborted) await prepend();
      const item = document.getElementById('message-' + seq);
      item?.focus({ preventScroll: true });
      item?.scrollIntoView({ block: 'center' });
    });
  }

  async function markVisible() {
    const seq = rows.at(-1)?.seq;
    if (marking) {
      pendingRead = true;
      return;
    }
    if (abort.signal.aborted || !viewport || !seq || seq <= lastRead || document.hidden) return;
    const bounds = document.getElementById('message-' + seq)?.getBoundingClientRect();
    const frame = viewport.getBoundingClientRect();
    if (
      !bounds ||
      bounds.bottom <= Math.max(0, frame.top) ||
      bounds.top >= Math.min(innerHeight, frame.bottom)
    )
      return;
    marking = true;
    try {
      lastRead = (await api.readChat(seq, abort.signal)).last_read;
      if (!abort.signal.aborted) onRead();
    } catch (error) {
      if (!abort.signal.aborted) problem = problemText(error);
    } finally {
      marking = false;
      if (pendingRead) {
        pendingRead = false;
        void markVisible();
      }
    }
  }

  async function send() {
    if (sending || !canWrite || !draft.trim() || count > 4000) return;
    sending = true;
    sendProblem = '';
    const value = draft;
    try {
      await api.postMessage(value);
      if (!abort.signal.aborted) {
        if (draft === value) draft = '';
        refresh();
      }
    } catch (error) {
      sendProblem = problemText(error);
    } finally {
      sending = false;
    }
  }

  onMount(() => {
    const off = events.subscribe('chat_message', (changes) =>
      refresh(changes.flatMap((c) => (c.id ? [c.id] : []))),
    );
    const connected = events.subscribe('resync', () => refresh());
    const visibility = () => {
      void markVisible();
    };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('scroll', visibility);
    window.addEventListener('resize', visibility);
    refresh();
    return () => {
      abort.abort();
      clearTimeout(retryTimer);
      off();
      connected();
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('scroll', visibility);
      window.removeEventListener('resize', visibility);
    };
  });
</script>

<section class="access chat-panel" aria-label="Панель общего чата">
  <ChatSearch {jump} />
  {#if problem}<div role="alert">
      {problem} <button onclick={() => refresh()}>Обновить ленту</button>
    </div>{/if}
  <div
    class="chat-feed"
    role="region"
    aria-label="Лента сообщений"
    bind:this={viewport}
    onscroll={() => {
      if (viewport.scrollTop < 12) loadOlder();
      void markVisible();
    }}
  >
    {#if older}<button onclick={loadOlder}>Предыдущие сообщения</button>{/if}
    {#each rows as message (message.id)}
      <ChatMessage {message} refresh={() => refresh([message.id])} />
    {:else}<p class="muted">{loaded ? 'Сообщений пока нет' : 'Загрузка сообщений…'}</p>{/each}
  </div>
  {#if canWrite}
    <form
      class="chat-compose"
      onsubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <label for="chat-draft">Новое сообщение</label>
      <textarea
        id="chat-draft"
        rows="2"
        bind:value={draft}
        onkeydown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            void send();
          }
        }}
      ></textarea>
      {#if sendProblem}<p role="alert">{sendProblem}</p>{/if}
      <div>
        <small>{count} / 4000 · Shift+Enter для новой строки</small>
        <button class="primary" disabled={sending || !draft.trim() || count > 4000}
          >Отправить</button
        >
      </div>
    </form>
  {/if}
</section>
