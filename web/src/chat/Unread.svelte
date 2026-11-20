<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import { events } from './events.ts';
  let { readEvents }: { readEvents: EventTarget } = $props();
  let unread = $state(0);
  onMount(() => {
    let request: AbortController;
    let timer: ReturnType<typeof setTimeout>;
    async function update() {
      request?.abort();
      clearTimeout(timer);
      const current = new AbortController();
      request = current;
      try {
        const page = await api.messages({ limit: 1 }, current.signal);
        if (!current.signal.aborted) unread = page.unread;
      } catch {
        if (!current.signal.aborted) timer = setTimeout(update, 2000);
      }
    }
    const off = events.subscribe('chat_message', update);
    const resume = events.subscribe('resync', update);
    readEvents.addEventListener('read', update);
    void update();
    return () => {
      request?.abort();
      clearTimeout(timer);
      off();
      resume();
      readEvents.removeEventListener('read', update);
    };
  });
</script>

{#if unread}<span class="chat-badge" aria-label={'Непрочитано: ' + unread}>{unread}</span>{/if}
