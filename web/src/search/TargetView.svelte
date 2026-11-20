<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Dataset, KksCard, RecordDetail, Scalar, TypeField } from '../api/types.ts';
  import { text } from '../text.ts';
  import type { SearchTarget } from './model.ts';
  import '../admin/admin.css';
  import './search.css';

  let { target }: { target: SearchTarget } = $props();
  let card = $state<KksCard | null>(null);
  let record = $state<RecordDetail | null>(null);
  let fields = $state<TypeField[]>([]);
  let datasets = $state<Dataset[]>([]);
  let problem = $state('');
  let loading = $state(true);
  const controller = new AbortController();

  onMount(() => {
    void load();
    return () => controller.abort();
  });

  async function load() {
    try {
      if (target.kind === 'kks') {
        const [next, sets] = await Promise.all([
          api.getKksCard(target.id, controller.signal),
          api.listDatasets(undefined, controller.signal),
        ]);
        card = next;
        datasets = sets.items;
      } else {
        const [next, dataset] = await Promise.all([
          api.getRecord(target.id, controller.signal),
          api.getDataset(target.dataset, controller.signal),
        ]);
        if (next.record.dataset_id !== target.dataset) throw new Error(text.find.wrongDataset);
        const schema = await api.getType(dataset.type_id, controller.signal);
        fields = schema.fields.filter((field) => Object.hasOwn(next.record.doc, field.key));
        datasets = [dataset];
        record = next;
      }
    } catch (error) {
      if (!controller.signal.aborted)
        problem = error instanceof Error ? error.message : text.networkError;
    } finally {
      loading = false;
    }
  }

  function datasetName(id: string) {
    return datasets.find((item) => item.id === id)?.name ?? text.find.unknown;
  }
  function value(field: TypeField, item: Scalar | Scalar[]): string {
    if (Array.isArray(item)) return item.map((part) => value(field, part)).join('; ');
    if (item === null) return '—';
    if (typeof item === 'boolean') return item ? 'Да' : 'Нет';
    if (!record || typeof item !== 'string') return String(item);
    if (field.kind === 'choice') return record.refs.choices[item] ?? text.find.unknown;
    if (field.kind === 'kks') return record.refs.kks[item] ?? text.find.unknown;
    if (field.kind === 'record') return record.refs.records?.[item] ?? text.find.unknown;
    return item;
  }
</script>

<section
  class="access target-view"
  aria-label={target.kind === 'kks' ? text.find.card : text.find.inspector}
>
  {#if loading}<p role="status">{text.access.loading}</p>
  {:else if problem}<p class="form-error" role="alert">{problem}</p>
  {:else if card}
    <div class="target-heading">
      <h2>{text.find.card} · {card.code}</h2>
      {#if card.archived_at}<span class="state-badge">{text.find.archived}</span>{/if}
    </div>
    {#if card.name}<p>{card.name}</p>{/if}
    {#if card.aliases.length}<p>
        {text.find.aliases}: {card.aliases.map((item) => item.alias).join(', ')}
      </p>{/if}
    <div class="access-panel table-scroll">
      <table class="access-table" aria-label={text.find.usage}>
        <thead
          ><tr
            ><th>{text.find.dataset}</th><th>{text.find.connection}</th><th>{text.find.count}</th
            ></tr
          ></thead
        >
        <tbody
          >{#each card.usage as item}<tr
              ><th scope="row">{datasetName(item.dataset_id)}</th><td
                >{text.find.roles[item.role] ?? item.role}</td
              ><td>{item.records}</td></tr
            >
          {:else}<tr><td colspan="3">{text.find.noUsage}</td></tr>{/each}</tbody
        >
      </table>
    </div>
    {#if card.via_links.length}<h3>{text.find.linked}</h3>
      <div class="access-panel">
        <table class="access-table" aria-label={text.find.linked}>
          <thead><tr><th>{text.find.dataset}</th><th>{text.find.count}</th></tr></thead><tbody
            >{#each card.via_links as item}<tr
                ><th scope="row">{datasetName(item.dataset_id)}</th><td>{item.records}</td></tr
              >{/each}</tbody
          >
        </table>
      </div>{/if}
  {:else if record}
    <h2>{text.find.inspector} · {datasetName(record.record.dataset_id)}</h2>
    <p class="muted">{text.find.version}: {record.record.version}</p>
    <div class="access-panel">
      <table class="access-table" aria-label={text.find.inspector}>
        <thead><tr><th>{text.find.field}</th><th>{text.find.value}</th></tr></thead>
        <tbody
          >{#each fields as field (field.id)}<tr
              ><th scope="row">{field.name}</th><td>{value(field, record.record.doc[field.key])}</td
              ></tr
            >
          {:else}<tr><td colspan="2">{text.find.noFields}</td></tr>{/each}</tbody
        >
      </table>
    </div>
  {/if}
</section>
