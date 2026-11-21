<script lang="ts">
  import { api } from '../api/client.ts';
  import { proposeUrl } from './model.ts';
  let { id, page }: { id: string; page?: string } = $props();
  let visible = $state(false);
  $effect(() => {
    const c = new AbortController();
    visible = false;
    void api.knowledge
      .document(id, c.signal)
      .then(() => {
        if (!c.signal.aborted) visible = true;
      })
      .catch(() => {});
    return () => c.abort();
  });
</script>

{#if visible}<a class="knowledge-action" href={proposeUrl('document', id, page)}
    >Предложить заметку</a
  >{/if}
