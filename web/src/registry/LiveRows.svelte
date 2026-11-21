<script module lang="ts">
  import {
    Tabulator,
    FormatModule,
    ResizeTableModule,
    SelectRowModule,
    type ColumnDefinition,
  } from 'tabulator-tables';
  Tabulator.registerModule([FormatModule, ResizeTableModule, SelectRowModule]);
</script>

<script lang="ts">
  import { onMount } from 'svelte';
  import 'tabulator-tables/dist/css/tabulator.min.css';
  import { api } from '../api/client.ts';
  import type { Dataset, Permissions, RecordRow, Refs, Scalar, TypeField } from '../api/types.ts';
  import { problemText } from '../admin/model.ts';
  import { recordUrl } from '../search/target.ts';
  import { events } from '../chat/events.ts';
  import './registry.css';

  let {
    dataset,
    permissions,
    navigate,
  }: {
    dataset: Dataset;
    permissions: Permissions;
    navigate: (path: string) => void;
  } = $props();
  let host: HTMLDivElement;
  let table: Tabulator;
  let filter = $state('');
  let applied = '';
  let pages = 1;
  let more = $state(false);
  let selected = $state('');
  let problem = $state('');
  let fields: TypeField[] = [];
  let request: AbortController | undefined;
  let ready = false;
  let schemaKey = '';

  function display(value: Scalar | Scalar[] | undefined, field: TypeField, refs: Refs): string {
    if (Array.isArray(value)) return value.map((v) => display(v, field, refs)).join(', ');
    if (value == null) return '—';
    if (typeof value === 'boolean') return value ? 'Да' : 'Нет';
    if (typeof value === 'string') {
      if (field.kind === 'kks') return refs.kks[value] ?? '—';
      if (field.kind === 'choice') return refs.choices[value] ?? '—';
      if (field.kind === 'record') return refs.records?.[value] ?? '—';
    }
    return String(value);
  }

  function columns(): ColumnDefinition[] {
    return fields.map((f, i) => ({
      title: f.name,
      field: 'c' + i,
      minWidth: 150,
      formatter: 'plaintext',
      titleFormatter: () => {
        const node = document.createElement('span');
        node.textContent = f.name;
        return node;
      },
    }));
  }
  function rowData(record: RecordRow, refs: Refs) {
    return {
      id: record.id,
      ...Object.fromEntries(fields.map((f, i) => ['c' + i, display(record.doc[f.key], f, refs)])),
    };
  }

  async function refresh(schema = false) {
    if (!ready) return;
    request?.abort();
    const next = new AbortController();
    request = next;
    try {
      if (schema || !schemaKey) {
        const data = await api.getType(dataset.type_id, next.signal);
        const visible = data.fields.filter(
          (f) => !f.archived_at && permissions.fields[f.id] !== 'hidden',
        );
        const key = JSON.stringify(visible.map((f) => [f.id, f.name, f.kind]));
        if (key !== schemaKey) {
          fields = visible;
          schemaKey = key;
          table.setColumns(columns());
        }
      }
      const data = [];
      let cursor: string | null = null;
      for (let index = 0; index < pages; index++) {
        const page = await api.queryRecords(
          dataset.id,
          {
            search: applied || null,
            limit: 100,
            sort: { field: '$created_at', dir: 'desc' },
            cursor,
          },
          next.signal,
        );
        data.push(...page.items.map((row) => rowData(row, page.refs)));
        cursor = page.next_cursor;
        if (!cursor) break;
      }
      if (next.signal.aborted) return;
      // При вставке перед страницей выделенная строка может уйти за её нижнюю границу.
      // Сохраняем строку, только если сервер по-прежнему возвращает её с текущим фильтром.
      if (cursor && selected && !data.some((row) => row.id === selected)) {
        const kept = await api.queryRecords(
          dataset.id,
          {
            search: applied || null,
            where: [{ field: '$id', op: 'eq', value: selected }],
            limit: 1,
          },
          next.signal,
        );
        data.push(...kept.items.map((row) => rowData(row, kept.refs)));
      }
      if (next.signal.aborted) return;
      const id = selected;
      const holder = host.querySelector<HTMLElement>('.tabulator-tableholder');
      const top = holder?.scrollTop ?? 0,
        left = holder?.scrollLeft ?? 0;
      await table.setData(data);
      if (next.signal.aborted) return;
      if (holder) {
        holder.scrollTop = top;
        holder.scrollLeft = left;
      }
      if (id && data.some((row) => row.id === id)) table.selectRow(id);
      selected = table.getSelectedData()[0]?.id ?? '';
      more = !!cursor;
      problem = '';
    } catch (error) {
      if (!next.signal.aborted) problem = problemText(error);
    }
  }

  function applyFilter() {
    applied = filter.trim();
    pages = 1;
    selected = '';
    table?.deselectRow();
    void refresh();
  }

  onMount(() => {
    table = new Tabulator(host, {
      data: [],
      columns: [],
      layout: 'fitDataStretch',
      height: 'min(56vh, 550px)',
      // Страницы загружает сервер. Высота видимых строк должна быть точной и у края страницы.
      renderVertical: 'basic',
      selectableRows: 1,
      rowHeight: 34,
      placeholder: 'Нет записей',
    });
    table.on('tableBuilt', () => {
      ready = true;
      void refresh(true);
    });
    table.on('rowSelectionChanged', (data) => {
      selected = data[0]?.id ?? '';
    });
    const off = events.subscribe('record', (changes) => {
      if (changes.some((c) => c.dataset_id === dataset.id)) void refresh();
    });
    const subs = ['resync', 'field', 'type', 'kks', 'dictionary_value', 'dataset'].map((kind) =>
      events.subscribe(kind, () => {
        void refresh(true);
      }),
    );
    return () => {
      ready = false;
      off();
      subs.forEach((f) => f());
      request?.abort();
      table.destroy();
    };
  });
</script>

<form
  class="registry-tools"
  onsubmit={(e) => {
    e.preventDefault();
    applyFilter();
  }}
>
  <label>Фильтр записей<input type="search" bind:value={filter} /></label><button>Применить</button>
  <button
    type="button"
    disabled={!selected}
    onclick={() => navigate(recordUrl(dataset.id, selected))}>Открыть запись</button
  >
</form>
{#if problem}<p role="alert">
    {problem} <button onclick={() => refresh(true)}>Повторить</button>
  </p>{/if}
<div bind:this={host} aria-label="Строки реестра"></div>
{#if more}<button
    onclick={() => {
      pages++;
      void refresh();
    }}>Ещё записи</button
  >{/if}
