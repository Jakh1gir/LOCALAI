<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import { actions, shown, type Row, type Action, type Summary } from './model.ts';
  import Totals from './Totals.svelte';
  import MagnetResult from './MagnetResult.svelte';
  let {
    id,
    summary,
    back,
    next,
  }: { id: string; summary: Summary; back: () => void; next: () => void } = $props();
  let filter = $state<Action | ''>(''),
    rows = $state<Row[]>([]),
    more = $state(false),
    last = 0,
    busy = $state(false),
    error = $state('');
  const ctrl = new AbortController();
  async function page(append = false) {
    busy = true;
    error = '';
    if (!append) {
      rows = [];
      last = 0;
      more = false;
    }
    try {
      const p = await api.imports.rows(id, filter, last, ctrl.signal);
      rows = append ? [...rows, ...p.items] : p.items;
      more = p.more;
      last = p.last ?? 0;
    } catch (e) {
      if (!ctrl.signal.aborted) error = problemText(e);
    } finally {
      busy = false;
    }
  }
  async function decide(row: Row, decision: 'skip' | 'apply') {
    busy = true;
    error = '';
    try {
      const result = await api.imports.decide(id, row.row, decision, ctrl.signal);
      rows = rows.map((r) => (r.row === row.row ? result : r));
    } catch (e) {
      if (!ctrl.signal.aborted) error = problemText(e);
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    if (summary.mode !== 'magnet') void page();
    return () => ctrl.abort();
  });
</script>

<h2>Проверка плана</h2>
<p>Строк на листе · {summary.rows}</p>
<Totals counts={summary.counts} />
{#if summary.mode === 'magnet'}<p>{summary.datasets?.join(', ')}</p>
  <MagnetResult
    plan={summary.plan}
    issues={[...(summary.issues ?? []), ...(summary.write_errors ?? [])]}
  />
{:else}
  <label>
    Действие
    <select
      aria-label="Действие"
      value={filter}
      disabled={busy}
      onchange={(e) => {
        filter = e.currentTarget.value as Action | '';
        void page();
      }}
    >
      <option value="">Все строки</option>
      {#each Object.entries(actions) as [k, v]}<option value={k}>{v}</option>{/each}
    </select>
  </label>
  <div class="table-scroll">
    <table class="access-table" aria-label="Строки плана">
      <thead>
        <tr>
          <th>Строка</th>
          <th>Действие</th>
          <th>Данные и изменения</th>
          <th>Причина и решение</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as r (r.row)}<tr>
            <th scope="row">{r.row}</th>
            <td>{actions[r.action]}</td>
            <td>
              {#each Object.entries(r.changes ?? {}) as [k, pair]}<div>
                  {k} · {shown(pair[0])} → {shown(pair[1])}
                </div>{:else}{shown(r.cells)}{/each}
            </td>
            <td>
              {#if r.message}{r.message}
                <small>{r.reason}</small>{/if}
              {#if r.action === 'conflict'}<label>
                  Решение · {r.row}
                  <select
                    value={r.decision ?? 'skip'}
                    disabled={busy}
                    onchange={(e) => decide(r, e.currentTarget.value as 'skip' | 'apply')}
                  >
                    <option value="skip">Пропустить</option>
                    {#if r.reason === 'version_conflict'}<option value="apply">
                        Применить поверх
                      </option>{/if}
                  </select>
                </label>{/if}
            </td>
          </tr>
        {:else}<tr><td colspan="4">{busy ? 'Загрузка…' : 'Строк нет'}</td></tr>{/each}
      </tbody>
    </table>
  </div>
  {#if more}<button disabled={busy} onclick={() => page(true)}>Ещё строки</button>{/if}
{/if}
{#if error}<p role="alert">
    {error}
    <button onclick={() => page()}>Обновить строки</button>
  </p>{/if}
<div class="form-actions">
  <button disabled={busy} onclick={back}>Изменить настройки</button>
  <button class="primary" disabled={busy || !!error} onclick={next}>К применению</button>
</div>
