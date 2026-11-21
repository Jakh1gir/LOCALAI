<script lang="ts">
  import SourceLink from './SourceLink.svelte';
  import type { Source } from './model.ts';
  let { sources }: { sources: Source[] } = $props();
</script>

<ul>
  {#each sources as item}<li>
      <SourceLink source={item} />
      {#if item.version != null}
        <span
          >{'Версия ' +
            item.version +
            (item.current_version === item.version
              ? ''
              : ' → ' + (item.current_version ?? 'удалён'))}</span
        >
      {/if}
    </li>{/each}
</ul>
