<script lang="ts">
  import { api } from '../api/client.ts';
  import { problemText, showTime } from '../admin/model.ts';
  let { id, version }: { id: string; version: number } = $props();
  let open = $state(false),
    error = $state('');
  let rows = $state<{ version: number; at: string; actor: string }[]>([]);
  $effect(() => {
    const key = id;
    void version;
    if (!open) return;
    const request = new AbortController();
    async function load() {
      const revisions = [];
      let before: string | undefined;
      do {
        const { items } = await api.documents.audit(key, before, request.signal);
        for (const event of items) {
          if (event.action === 'document.create' || event.action === 'document.replace')
            revisions.push({
              version: event.details.version ?? 1,
              at: event.at,
              actor: event.actor_label,
            });
        }
        before = items.length === 200 ? String(items.at(-1)!.seq) : undefined;
      } while (before);
      if (!request.signal.aborted) {
        rows = revisions;
        error = '';
      }
    }
    void load().catch((cause) => {
      if (!request.signal.aborted) error = problemText(cause);
    });
    return () => request.abort();
  });
</script>

<details bind:open>
  <summary>История версий</summary>
  {#if error}<p role="alert">{error}</p>{:else}<ul>
      {#each rows as row}<li>Версия {row.version} · {showTime(row.at)} · {row.actor}</li>{/each}
    </ul>{/if}
</details>
