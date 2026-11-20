<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Me } from '../api/types.ts';
  import { problemText, showTime } from '../admin/model.ts';
  import ConfirmAction from '../admin/ConfirmAction.svelte';
  import type { Profile } from './model.ts';
  let { me }: { me: Me } = $props();
  let items = $state<Profile[]>([]),
    selected = $state<Profile | null>(null),
    error = $state(''),
    busy = $state(false);
  const ctrl = new AbortController();
  async function load() {
    busy = true;
    error = '';
    try {
      items = (await api.imports.profiles(ctrl.signal)).items;
    } catch (e) {
      if (!ctrl.signal.aborted) error = problemText(e);
    } finally {
      busy = false;
    }
  }
  async function remove() {
    const p = selected;
    if (!p) return;
    selected = null;
    busy = true;
    error = '';
    try {
      await api.imports.removeProfile(p.id, ctrl.signal);
      items = items.filter((i) => i.id !== p.id);
    } catch (e) {
      if (!ctrl.signal.aborted) error = problemText(e);
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    void load();
    return () => ctrl.abort();
  });
</script>

<h2>Профили импорта</h2>
{#if error}<p role="alert">
    {error}
    <button onclick={load}>Повторить</button>
  </p>{/if}
<table class="access-table">
  <thead>
    <tr>
      <th>Профиль</th>
      <th>Обновлён</th>
      <th>Действия</th>
    </tr>
  </thead>
  <tbody>
    {#each items as p}<tr>
        <th>{p.name}</th>
        <td>{showTime(p.updated_at)}</td>
        <td>
          {#if p.created_by === me.user.id || me.permissions.sections.includes('admin')}<button
              disabled={busy}
              onclick={() => (selected = p)}
            >
              Удалить
            </button>{/if}
        </td>
      </tr>
    {:else}<tr><td colspan="3">{busy ? 'Загрузка…' : 'Сохранённых профилей нет'}</td></tr>{/each}
  </tbody>
</table>
{#if selected}<ConfirmAction
    title="Удалить профиль?"
    consequence={'Профиль «' + selected.name + '» больше не будет предложен для следующих книг.'}
    action="Удалить"
    onConfirm={remove}
    onCancel={() => (selected = null)}
  />{/if}
