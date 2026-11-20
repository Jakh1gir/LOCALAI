<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Dataset, NamedEntity, Me, TypeField } from '../api/types.ts';
  import { problemText } from '../admin/model.ts';
  import LayoutEditor from './LayoutEditor.svelte';
  import {
    eligibleKey,
    kinds,
    shown,
    writable,
    type Preview,
    type Config,
    type Mode,
    type FieldSpec,
    type Layout,
    type Profile,
    type Plan,
  } from './model.ts';

  let {
    batchId,
    sheet,
    header,
    datasets,
    sites,
    me,
    initial,
    preferred,
    busy,
    submit,
    back,
  }: {
    batchId: string;
    sheet: string;
    header: number;
    datasets: Dataset[];
    sites: NamedEntity[];
    me: Me;
    initial: Config | null;
    preferred: string;
    busy: boolean;
    submit: (p: Plan) => void;
    back: () => void;
  } = $props();
  let preview = $state<Preview | null>(null),
    fields = $state<TypeField[]>([]),
    loading = $state(false),
    error = $state('');
  let dataset = $state(''),
    site = $state(''),
    mode = $state<Mode>('records'),
    mapping = $state<Record<string, string>>({}),
    key = $state('');
  let typeName = $state(''),
    datasetName = $state(''),
    name = $state(''),
    specs = $state<(FieldSpec & { included: boolean })[]>([]),
    layout = $state<Layout>();
  let structure = $derived(me.permissions.sections.includes('structure'));
  let keys = $derived(
    fields.filter((f) => eligibleKey(f) && Object.values(mapping).includes(f.key)),
  );
  let magnetNames = $derived(
    layout?.signal ? [name.trim() + ' — сигналы', name.trim() + ' — кабели'] : [name.trim()],
  );
  let magnetAllowed = $derived(
    structure ||
      magnetNames.every((n) =>
        datasets.some(
          (d) =>
            d.site_id === site &&
            d.name.trim().toLocaleLowerCase('ru') === n.toLocaleLowerCase('ru'),
        ),
      ),
  );
  let canPlan = $derived(
    mode === 'records'
      ? !!dataset && Object.values(mapping).some(Boolean)
      : mode === 'new_type'
        ? structure && specs.some((f) => f.included)
        : magnetAllowed,
  );
  const controller = new AbortController();

  async function load(selectMode: boolean, config?: Config) {
    loading = true;
    error = '';
    preview = null;
    try {
      const selected = datasets.find((d) => d.id === dataset);
      const [p, type] = await Promise.all([
        api.imports.preview(batchId, sheet, header, dataset, controller.signal),
        selected ? api.getType(selected.type_id, controller.signal) : null,
      ]);
      fields = writable(type?.fields ?? [], me.permissions);
      const available = new Set(['@id', '@version', ...fields.map((f) => f.key)]);
      mapping = Object.fromEntries(
        Object.entries(config?.mapping ?? p.suggested.mapping).filter(
          ([h, v]) => p.headers.includes(h) && available.has(v),
        ),
      );
      key = fields.some(
        (f) => f.key === config?.key && eligibleKey(f) && Object.values(mapping).includes(f.key),
      )
        ? config!.key!
        : '';
      if (selectMode) {
        mode =
          config?.mode ??
          (p.suggested.mode === 'magnet'
            ? 'magnet'
            : dataset
              ? 'records'
              : structure
                ? 'new_type'
                : 'magnet');
        site = config?.new_type?.site_id ?? config?.site_id ?? sites[0]?.id ?? '';
        typeName = config?.new_type?.type_name ?? sheet;
        datasetName = config?.new_type?.dataset_name ?? sheet;
        name = config?.name ?? sheet;
        specs = p.headers.map((column) => {
          const f = config?.new_type?.fields.find((f) => f.column === column);
          return {
            column,
            name: f?.name ?? column,
            kind: f?.kind ?? 'text',
            unique: f?.unique ?? false,
            included: !config?.new_type || !!f,
          };
        });
        layout = structuredClone(
          config?.layout ? $state.snapshot(config.layout) : p.suggested.layout,
        );
      }
      preview = p;
    } catch (e) {
      if (!controller.signal.aborted) error = problemText(e);
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    dataset =
      datasets.find((d) => d.id === (initial?.dataset_id ?? preferred))?.id ??
      datasets[0]?.id ??
      '';
    void load(true, initial ?? undefined);
    return () => controller.abort();
  });
  function pick(p: Profile) {
    if (p.settings.mode === 'records') dataset = p.settings.dataset_id ?? '';
    void load(true, p.settings);
  }
  function usable(c: Config) {
    if (c.mode === 'new_type') return structure;
    if (c.mode === 'records') return datasets.some((d) => d.id === c.dataset_id);
    return true;
  }
  function config(): Config {
    if (mode === 'records')
      return {
        mode,
        dataset_id: dataset,
        mapping: Object.fromEntries(Object.entries(mapping).filter(([, v]) => v)),
        ...(keys.some((f) => f.key === key) ? { key } : {}),
      };
    if (mode === 'new_type')
      return {
        mode,
        new_type: {
          type_name: typeName.trim(),
          dataset_name: datasetName.trim(),
          site_id: site,
          fields: specs.filter((f) => f.included).map(({ included, ...f }) => f),
        },
      };
    return { mode, name: name.trim(), site_id: site, layout };
  }
  const modes: Record<Mode, string> = {
    records: 'В существующий реестр',
    new_type: 'Создать тип и загрузить',
    magnet: 'Магнит ККС',
  };
</script>

<form
  onsubmit={(e) => {
    e.preventDefault();
    if (preview && canPlan && !busy && !loading) submit({ ...config(), sheet, header_row: header });
  }}
>
  <fieldset disabled={busy || loading}>
    {#if loading}<p role="status">Загрузка примера и полей…</p>{/if}
    {#if error}<p role="alert">{error}</p>
      <button type="button" onclick={() => load(true)}>Повторить</button>{/if}
    {#if preview}
      {#if preview.profiles.some((p) => usable(p.settings))}<section class="import-profiles">
          <h3>Подходящие профили</h3>
          {#each preview.profiles.filter((p) => usable(p.settings)) as p}<button
              type="button"
              onclick={() => pick(p)}
            >
              Использовать профиль «{p.name}»
            </button>{/each}
        </section>{/if}
      <h2>Назначение</h2>
      <div class="import-controls">
        <label>
          Режим
          <select aria-label="Режим" bind:value={mode}>
            {#each Object.entries(modes) as [k, v]}{#if (k !== 'records' || datasets.length) && (k !== 'new_type' || structure)}<option
                  value={k}
                >
                  {v}
                </option>{/if}{/each}
          </select>
        </label>
        {#if mode === 'records'}<label>
            Целевой реестр
            <select
              aria-label="Целевой реестр"
              bind:value={dataset}
              onchange={(e) => {
                dataset = e.currentTarget.value;
                void load(false);
              }}
            >
              {#each datasets as d}<option value={d.id}>{d.name}</option>{/each}
            </select>
          </label>
        {:else}<label>
            Площадка
            <select aria-label="Площадка" bind:value={site} required>
              {#each sites as s}<option value={s.id}>{s.name}</option>{/each}
            </select>
          </label>
          {#if mode === 'new_type'}<label>
              Название типа
              <input bind:value={typeName} required />
            </label>
            <label>
              Название реестра
              <input bind:value={datasetName} required />
            </label>
          {:else}<label>
              Имя раскладки
              <input bind:value={name} required />
            </label>{/if}
        {/if}
      </div>
      <details>
        <summary>Пример данных · {preview.rows} строк</summary>
        <div class="table-scroll">
          <table class="access-table">
            <thead>
              <tr>
                <th>Строка</th>
                {#each preview.headers as h}<th>{h}</th>{/each}
              </tr>
            </thead>
            <tbody>
              {#each preview.sample as r}<tr>
                  <th>{r.row}</th>
                  {#each preview.headers as h}<td>{shown(r.cells[h])}</td>{/each}
                </tr>{/each}
            </tbody>
          </table>
        </div>
      </details>
      <h2>Сопоставление</h2>
      {#if mode === 'records'}
        <table class="access-table">
          <thead>
            <tr>
              <th>Столбец Excel</th>
              <th>Поле реестра</th>
            </tr>
          </thead>
          <tbody>
            {#each preview.headers as h}<tr>
                <th>{h}</th>
                <td>
                  <select aria-label={'Поле · ' + h} bind:value={mapping[h]}>
                    <option value="">Пропустить</option>
                    <option value="@id">UUID · служебный</option>
                    <option value="@version">Версия · служебный</option>
                    {#each fields as f}<option value={f.key}>{f.name}</option>{/each}
                  </select>
                </td>
              </tr>{/each}
          </tbody>
        </table>
        <label>
          Ключ записи
          <select aria-label="Ключ записи" bind:value={key}>
            <option value="">Без ключа · UUID или добавление</option>
            {#each keys as f}<option value={f.key}>{f.name}</option>{/each}
          </select>
        </label>
      {:else if mode === 'new_type'}
        <table class="access-table">
          <thead>
            <tr>
              <th>Столбец Excel</th>
              <th>Название поля</th>
              <th>Вид</th>
              <th>Ключ</th>
            </tr>
          </thead>
          <tbody>
            {#each specs as spec}<tr>
                <th>
                  <label class="check-label">
                    <input
                      type="checkbox"
                      aria-label={'Включить · ' + spec.column}
                      bind:checked={spec.included}
                    />
                    {spec.column}
                  </label>
                </th>
                <td>
                  <input
                    disabled={!spec.included}
                    aria-label={'Название · ' + spec.column}
                    bind:value={spec.name}
                    required
                  />
                </td>
                <td>
                  <select
                    disabled={!spec.included}
                    aria-label={'Вид · ' + spec.column}
                    bind:value={spec.kind}
                    onchange={(e) => {
                      if (['boolean', 'kks'].includes(e.currentTarget.value)) spec.unique = false;
                    }}
                  >
                    {#each Object.entries(kinds) as [k, v]}<option value={k}>{v}</option>{/each}
                  </select>
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={'Ключ · ' + spec.column}
                    checked={spec.unique}
                    disabled={!spec.included || ['boolean', 'kks'].includes(spec.kind)}
                    onchange={(e) => {
                      const checked = e.currentTarget.checked;
                      specs.forEach((f) => (f.unique = f.column === spec.column && checked));
                    }}
                  />
                </td>
              </tr>{/each}
          </tbody>
        </table>
      {:else if layout}
        <p>Целевые реестры · {magnetNames.join(', ')}</p>
        {#if !magnetAllowed}<p role="status">
            Для этой раскладки нужны существующие реестры с правом импорта либо право на структуру.
          </p>{/if}
        <LayoutEditor headers={preview.headers} bind:layout />
      {/if}
    {/if}
    <div class="form-actions">
      <button type="button" onclick={back}>Назад к листу</button>
      <button class="primary" disabled={!preview || !canPlan}>Построить план</button>
    </div>
  </fieldset>
</form>
