<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError } from '../api/client.ts';
  import type { Me, Dataset, NamedEntity } from '../api/types.ts';
  import { problemText } from '../admin/model.ts';
  import Setup from './Setup.svelte';
  import Review from './Review.svelte';
  import Profiles from './Profiles.svelte';
  import Totals from './Totals.svelte';
  import MagnetResult from './MagnetResult.svelte';
  import { targets, shown, registryPath, type Batch, type Plan, type Report } from './model.ts';
  import '../admin/admin.css';
  import './wizard.css';

  let { me, preferred }: { me: Me; preferred: string } = $props();
  let step = $state<'file' | 'setup' | 'review' | 'apply' | 'report'>('file'),
    profiles = $state(false);
  let batch = $state<Batch | null>(null),
    settings = $state<Plan | null>(null),
    report = $state<Report | null>(null);
  let sheet = $state(''),
    header = $state(1),
    name = $state(''),
    busy = $state(false),
    error = $state(''),
    stale = $state(false);
  let all = $state<Dataset[]>([]),
    sites = $state<NamedEntity[]>([]),
    ready = $state(false);
  let available = $derived(targets(all, me.permissions));
  let page = $derived(batch?.sheets.find((s) => s.name === sheet));
  let reportTargets = $derived(
    all.filter(
      (d) =>
        d.id === report?.dataset_id ||
        (!!report?.datasets?.includes(d.name) && settings?.site_id === d.site_id),
    ),
  );
  const ctrl = new AbortController();
  const steps = {
    file: 'Файл и лист',
    setup: 'Назначение и сопоставление',
    review: 'Проверка',
    apply: 'Применение',
    report: 'Отчёт',
  };

  async function run(fn: () => Promise<void>) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await fn();
    } catch (e) {
      if (!ctrl.signal.aborted) {
        error = problemText(e);
        if (e instanceof ApiError) {
          if (e.code === 'import_stale') stale = true;
          if (
            e.code === 'import_failed' &&
            e.details?.row &&
            !error.includes('Строка ' + e.details.row)
          )
            error = 'Строка ' + e.details.row + ' · ' + error;
        }
      }
    } finally {
      busy = false;
    }
  }
  async function catalog() {
    const [d, s] = await Promise.all([
      api.listDatasets(undefined, ctrl.signal),
      api.listSites(ctrl.signal),
    ]);
    all = d.items;
    sites = s.items.filter((s) => !s.archived_at);
    ready = true;
  }
  function selectSheet(value: string) {
    sheet = value;
    header = batch?.sheets.find((s) => s.name === value)?.header_row ?? 1;
    settings = null;
  }
  function restart() {
    step = 'file';
    batch = null;
    settings = null;
    report = null;
    name = '';
    stale = false;
    error = '';
  }
  function upload(file?: File) {
    if (!file) return;
    restart();
    void run(async () => {
      batch = await api.imports.upload(file, ctrl.signal);
      selectSheet(
        batch.sheets.find((s) => s.rows && s.columns)?.name ?? batch.sheets[0]?.name ?? '',
      );
    });
  }
  function plan(p: Plan) {
    void run(async () => {
      const updated = await api.imports.plan(batch!.id, p, ctrl.signal);
      batch = updated;
      settings = p;
      stale = false;
      step = 'review';
    });
  }
  function apply() {
    void run(async () => {
      report = await api.imports.apply(batch!.id, name, ctrl.signal);
      step = 'report';
      await catalog();
    });
  }
  onMount(() => {
    void run(catalog);
    return () => ctrl.abort();
  });
</script>

<section class="access import-ui" aria-label="Мастер импорта Excel">
  <div class="access-tabs">
    <button
      aria-current={!profiles ? 'page' : undefined}
      disabled={busy}
      onclick={() => (profiles = false)}
    >
      Мастер импорта
    </button>
    <button
      aria-current={profiles ? 'page' : undefined}
      disabled={busy || !['file', 'report'].includes(step)}
      onclick={() => (profiles = true)}
    >
      Профили
    </button>
  </div>
  {#if profiles}<Profiles {me} />
  {:else}
    <ol class="import-steps">
      {#each Object.entries(steps) as [k, label]}<li aria-current={step === k ? 'step' : undefined}>
          {label}
        </li>{/each}
    </ol>
    <div class="access-panel import-page" aria-busy={busy}>
      {#if step === 'file'}
        <h2>Книга Excel</h2>
        <label>
          Файл .xlsx
          <input
            type="file"
            accept=".xlsx"
            disabled={busy || !ready}
            onchange={(e) => {
              const input = e.currentTarget;
              const file = input.files?.[0];
              input.value = '';
              upload(file);
            }}
          />
        </label>
        {#if busy}<progress aria-label="Загрузка"></progress>
          <span role="status">
            {ready ? 'Загрузка и разбор книги…' : 'Загрузка справочников…'}
          </span>{/if}
        {#if !ready && !busy}<button onclick={() => run(catalog)}>
            Повторить загрузку справочников
          </button>{/if}
        {#if batch}<p>{batch.file.name}</p>
          <label>
            Лист
            <select
              aria-label="Лист"
              value={sheet}
              onchange={(e) => selectSheet(e.currentTarget.value)}
            >
              {#each batch.sheets as s}<option value={s.name}>
                  {s.name} · {s.rows} стр. × {s.columns} ст.
                </option>{/each}
            </select>
          </label>
          {#if page}<h3>Строка заголовков</h3>
            <div class="table-scroll">
              <table class="access-table" aria-label="Начало листа">
                <tbody>
                  {#each page.top as cells, i}<tr class:selected={header === i + 1}>
                      <th>
                        <label class="check-label">
                          <input
                            type="radio"
                            name="header"
                            value={i + 1}
                            checked={header === i + 1}
                            aria-label={'Строка ' + (i + 1)}
                            onchange={() => {
                              header = i + 1;
                              settings = null;
                            }}
                          />
                          {i + 1}
                        </label>
                      </th>
                      {#each cells as cell}<td>{shown(cell)}</td>{/each}
                    </tr>{/each}
                </tbody>
              </table>
            </div>
            <button
              class="primary"
              disabled={!page.top.length || busy}
              onclick={() => {
                error = '';
                step = 'setup';
              }}
            >
              К сопоставлению
            </button>{/if}
        {/if}
      {:else if step === 'setup' && batch}
        <Setup
          batchId={batch.id}
          {sheet}
          {header}
          datasets={available}
          {sites}
          {me}
          initial={settings}
          {preferred}
          {busy}
          submit={plan}
          back={() => {
            error = '';
            step = 'file';
          }}
        />
      {:else if step === 'review' && batch?.summary}
        <Review
          id={batch.id}
          summary={batch.summary}
          back={() => {
            error = '';
            step = 'setup';
          }}
          next={() => {
            error = '';
            step = 'apply';
          }}
        />
      {:else if step === 'apply' && batch?.summary && settings}
        <h2>Применение плана</h2>
        <p>{batch.file.name} · {sheet}. Готовые строки будут записаны одной транзакцией.</p>
        <Totals counts={batch.summary.counts} />
        <label>
          Сохранить профиль под именем (необязательно)
          <input bind:value={name} disabled={busy} />
        </label>
        <div class="form-actions">
          <button
            disabled={busy}
            onclick={() => {
              error = '';
              step = 'review';
            }}
          >
            К проверке
          </button>
          {#if stale}<button class="primary" disabled={busy} onclick={() => plan(settings!)}>
              Перестроить план
            </button>
          {:else}<button class="primary" disabled={busy} onclick={apply}>
              Применить импорт
            </button>{/if}
        </div>
      {:else if step === 'report' && report}
        <h2>Импорт завершён</h2>
        <p>{batch?.file.name} · {sheet}</p>
        <Totals counts={report.counts} />
        {#if report.profile}<p>Профиль сохранён · {report.profile.name}</p>{/if}
        {#if report.skipped?.length}<h3>Пропущенные строки</h3>
          <ul>
            {#each report.skipped as i}<li>Строка {i.row} · {i.message} ({i.reason})</li>{/each}
          </ul>
          {#if report.skipped.length < report.counts.error + report.counts.conflict}<p>
              Показаны первые {report.skipped.length} пропусков.
            </p>{/if}{/if}
        {#if report.result}<MagnetResult
            plan={report.result.plan}
            issues={report.result.issues}
          />{/if}
        <div class="form-actions">
          {#each reportTargets as d}<a href={registryPath(d.id)}>{d.name}</a>{/each}
          {#if error}<button onclick={() => run(catalog)}>Обновить список реестров</button>{/if}
          <button disabled={busy} onclick={restart}>Загрузить другую книгу</button>
        </div>
      {/if}
      {#if error}<p class="form-error" role="alert">{error}</p>{/if}
      {#if busy && step !== 'file'}<p role="status">Сервер обрабатывает запрос…</p>{/if}
    </div>{/if}
</section>
