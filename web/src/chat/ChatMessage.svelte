<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError } from '../api/client.ts';
  import { problemText, showTime } from '../admin/model.ts';
  import ConfirmAction from '../admin/ConfirmAction.svelte';
  import MessageText from './MessageText.svelte';
  import type { Message, Revision } from './types.ts';

  let { message, refresh }: { message: Message; refresh: () => void } = $props();
  let mode = $state<'' | 'edit' | 'hide' | 'delete' | 'history'>('');
  let input = $state('');
  let baseVersion = 0;
  let revisions = $state<Revision[]>([]);
  let busy = $state(false);
  let problem = $state('');
  let conflict = $state(false);
  const abort = new AbortController();
  onDestroy(() => abort.abort());

  function begin(next: typeof mode) {
    problem = '';
    conflict = false;
    mode = next;
    baseVersion = message.version;
    input = next === 'edit' ? (message.text ?? '') : '';
  }

  async function act(operation: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    problem = '';
    conflict = false;
    try {
      await operation();
      if (!abort.signal.aborted) {
        mode = '';
        refresh();
      }
    } catch (error) {
      problem = problemText(error);
      conflict = error instanceof ApiError && error.code === 'version_conflict';
    } finally {
      busy = false;
    }
  }

  async function history() {
    begin('history');
    revisions = [];
    try {
      revisions = (await api.messageHistory(message.id, abort.signal)).items;
    } catch (error) {
      problem = problemText(error);
    }
  }
</script>

<article class="chat-message" id={'message-' + message.seq} tabindex="-1">
  <header>
    <strong>{message.author.name}</strong><time datetime={message.created_at}
      >{showTime(message.created_at)}</time
    >
    {#if message.edited_at}<small>изменено</small>{/if}
  </header>
  {#if message.state === 'deleted'}<p class="muted">Сообщение удалено</p>
  {:else if message.state === 'hidden'}<p class="muted">Сообщение скрыто</p>{/if}
  {#if message.text !== null}<MessageText {message} />{/if}
  <div class="chat-actions">
    {#if message.can.edit}<button onclick={() => begin('edit')}>Изменить</button>{/if}
    {#if message.can.delete}<button onclick={() => begin('delete')}>Удалить</button>{/if}
    {#if message.can.hide}<button onclick={() => begin('hide')}>Скрыть</button>{/if}
    {#if message.can.unhide}<button
        disabled={busy}
        onclick={() => act(() => api.unhideMessage(message.id))}>Вернуть</button
      >{/if}
    {#if message.can.history}<button onclick={() => (mode === 'history' ? begin('') : history())}
        >История</button
      >{/if}
  </div>
  {#if mode === 'edit' && message.can.edit}
    <form
      onsubmit={(e) => {
        e.preventDefault();
        void act(() => api.editMessage(message.id, baseVersion, input));
      }}
    >
      <label>Текст сообщения<textarea bind:value={input} required></textarea></label>
      <button disabled={busy || !input.trim() || Array.from(input).length > 4000}>Сохранить</button>
      <button type="button" onclick={() => begin('')}>Отмена</button>
    </form>
  {:else if mode === 'hide' && message.can.hide}
    <form
      onsubmit={(e) => {
        e.preventDefault();
        void act(() => api.hideMessage(message.id, input));
      }}
    >
      <label>Причина (необязательно)<input bind:value={input} maxlength="500" /></label>
      <button disabled={busy}>Скрыть сообщение</button>
      <button type="button" onclick={() => begin('')}>Отмена</button>
    </form>
  {:else if mode === 'delete' && message.can.delete}
    <ConfirmAction
      title="Удалить сообщение?"
      consequence="Другие участники увидят отметку об удалении."
      action="Удалить"
      onConfirm={() => {
        mode = '';
        void act(() => api.deleteMessage(message.id, baseVersion));
      }}
      onCancel={() => begin('')}
    />
  {:else if mode === 'history' && message.can.history}
    <section aria-label="История сообщения">
      <h3>История сообщения</h3>
      {#each revisions as row}<p><strong>Версия {row.version}</strong> · {showTime(row.at)}</p>
        <p class="message-text">{row.text}</p>{/each}
    </section>
  {/if}
  {#if problem}<p role="alert">{problem}</p>{/if}
  {#if conflict}<button
      onclick={() => {
        begin('');
        refresh();
      }}>Обновить ленту</button
    >{/if}
</article>
