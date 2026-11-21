<script lang="ts">
  import { api } from '../api/client.ts';
  import { kksUrl, recordUrl } from '../search/target.ts';
  import { problemText } from '../admin/model.ts';
  import type { Source } from './model.ts';
  let { source }: { source: Source } = $props();
  let link = $state(''),
    label = $state(''),
    error = $state('');
  $effect(() => {
    const item = source,
      c = new AbortController();
    link = '';
    label = '';
    error = '';
    async function resolve() {
      if (item.kind === 'kks') {
        const code = item.code || (await api.getKksCard(item.id, c.signal)).code;
        if (!c.signal.aborted) {
          link = kksUrl(item.id);
          label = code;
        }
      } else if (item.kind === 'record') {
        const { record } = await api.getRecord(item.id, c.signal);
        if (!c.signal.aborted) {
          link = recordUrl(record.dataset_id, item.id);
          label = 'Запись · ' + (typeof record.doc.name === 'string' ? record.doc.name : item.id);
        }
      } else {
        const title = item.title || (await api.knowledge.document(item.id, c.signal)).title;
        if (!c.signal.aborted) {
          link =
            '/documents?' +
            new URLSearchParams({ document: item.id, ...(item.page ? { page: item.page } : {}) });
          label = title + (item.page ? ' · стр. ' + item.page : '');
        }
      }
    }
    void resolve().catch((e) => {
      if (!c.signal.aborted) error = problemText(e);
    });
    return () => c.abort();
  });
</script>

{#if link}<a href={link}>{label}</a>{:else if error}<span>{error}</span>{:else}<span>Источник…</span
  >{/if}
