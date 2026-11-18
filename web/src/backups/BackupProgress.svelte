<script lang="ts">
  import { onMount } from 'svelte';
  import { text } from '../text.ts';

  let { label }: { label: string } = $props();
  let elapsed = $state(0);
  onMount(() => {
    const started = Date.now();
    const timer = setInterval(() => {
      elapsed = Math.floor((Date.now() - started) / 1000);
    }, 1000);
    return () => clearInterval(timer);
  });
</script>

<div class="backup-progress" role="status">
  <p>{label}… <span class="muted">{text.backups.elapsed(elapsed)}</span></p>
  <progress aria-label={label}></progress>
</div>
