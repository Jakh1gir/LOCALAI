<script lang="ts">
  import { api } from '../api/client.ts';
  import { showTime, problemText } from '../admin/model.ts';
  import { observe, scopeLabel, states, type Note } from './model.ts';
  import Sources from './Sources.svelte';
  import Editor from './Editor.svelte';
  let { id, manage, user }: { id: string; manage: boolean; user: string } = $props();
  let note = $state<Note | null>(null),
    editing = $state<Note | null>(null),
    reject = $state(false),
    reason = $state('');
  let error = $state(''),
    readError = $state(''),
    busy = $state(false),
    rev = $state(0);
  $effect(() => {
    const key = id;
    void rev;
    return observe(
      'knowledge_note',
      (signal) => api.knowledge.note(key, signal),
      (next) => {
        note = next;
        readError = '';
      },
      (msg) => {
        note = null;
        editing = null;
        readError = msg;
      },
    );
  });
  async function decide(confirm: boolean) {
    if (!note || busy || !manage) return;
    busy = true;
    error = '';
    try {
      note = confirm
        ? await api.knowledge.confirm(id, note.version)
        : await api.knowledge.reject(id, reason.trim());
      reject = false;
      reason = '';
      rev++;
    } catch (e) {
      error = problemText(e);
    } finally {
      busy = false;
    }
  }
</script>

{#if readError}<p role="alert">{readError}</p>
  <button onclick={() => rev++}>Обновить</button>
{:else if editing}<Editor
    note={editing}
    cancel={() => (editing = null)}
    saved={(next) => {
      note = next;
      editing = null;
      rev++;
    }}
  />
{:else if note}
  <article class="access-panel editor">
    <h2>{note.title}</h2>
    <p>{states[note.state]} · {scopeLabel(note.scope)}</p>
    <p class="knowledge-body">{note.body}</p>
    <p>Автор · {note.author} · {showTime(note.created_at)}</p>
    <p>Изменена · {showTime(note.updated_at)}</p>
    {#if note.confirmed_by}<p>
        Подтвердил(а) · {note.confirmed_by} · {showTime(note.confirmed_at)}
      </p>{/if}
    {#if note.stale_reason}<p><strong>Причина устаревания</strong> · {note.stale_reason}</p>{/if}
    {#if note.reject_reason}<p>Причина отклонения · {note.reject_reason}</p>{/if}
    {#if note.sources.length}<h3>Источники</h3>
      <Sources sources={note.sources} />{/if}
    <div class="form-actions">
      {#if manage && (note.state === 'proposed' || note.state === 'stale')}
        <button class="primary" disabled={busy} onclick={() => decide(true)}
          >{note.state === 'stale' ? 'Проверено, верно' : 'Подтвердить'}</button
        >
        <button disabled={busy} onclick={() => (reject = true)}>Отклонить</button>
      {/if}
      {#if manage || note.author === user}<button
          disabled={busy}
          onclick={() => {
            editing = note;
          }}>Исправить</button
        >{/if}
    </div>
    {#if reject && manage}<form
        class="editor"
        onsubmit={(e) => {
          e.preventDefault();
          void decide(false);
        }}
      >
        <label>Причина отклонения<input required maxlength="500" bind:value={reason} /></label>
        <div class="form-actions">
          <button disabled={busy || !reason.trim()}>Отклонить с причиной</button><button
            type="button"
            onclick={() => (reject = false)}>Отмена</button
          >
        </div>
      </form>{/if}
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  </article>
{:else}<p role="status">Загрузка заметки…</p>{/if}
