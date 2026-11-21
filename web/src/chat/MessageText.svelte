<script lang="ts">
  import { kksUrl, recordUrl } from '../search/target.ts';
  import type { Message } from './types.ts';

  let { message }: { message: Message } = $props();
  let parts = $derived.by(() => {
    // Сервер считает позиции в кодовых точках Python, включая символы вне BMP.
    const chars = Array.from(message.text ?? '');
    const items: { label: string; href?: string; denied?: boolean }[] = [];
    let offset = 0;
    for (const ref of message.refs) {
      items.push({ label: chars.slice(offset, ref.start).join('') });
      if (ref.access && ref.kind === 'kks')
        items.push({ label: chars.slice(ref.start, ref.end).join(''), href: kksUrl(ref.id) });
      else if (ref.access && ref.kind === 'record' && ref.dataset_id)
        items.push({ label: ref.dataset ?? 'Запись', href: recordUrl(ref.dataset_id, ref.id) });
      else items.push({ label: 'нет доступа', denied: true });
      offset = ref.end;
    }
    return [...items, { label: chars.slice(offset).join('') } as (typeof items)[number]];
  });
</script>

<p class="message-text">
  {#each parts as part}{#if part.href}<a href={part.href}>{part.label}</a
      >{:else if part.denied}<span class="muted">[{part.label}]</span
      >{:else}{part.label}{/if}{/each}
</p>
