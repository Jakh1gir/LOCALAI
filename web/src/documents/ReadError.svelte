<script lang="ts">
  import { api } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';

  let { id }: { id: string } = $props();
  let reason = $state('');
  $effect(() => {
    const request = new AbortController();
    void api.documents
      .get(id, request.signal)
      .then((doc) => {
        if (!request.signal.aborted) reason = doc.error ?? '';
      })
      .catch((error) => {
        if (!request.signal.aborted) reason = problemText(error);
      });
    return () => request.abort();
  });
</script>

<span class="cell-description">{reason}</span>
