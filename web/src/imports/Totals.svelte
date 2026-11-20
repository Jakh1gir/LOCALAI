<script lang="ts">
  import { actions, type Summary } from './model.ts';
  let { counts }: { counts: Summary['counts'] } = $props();
</script>

<dl class="import-totals">
  {#each Object.entries(actions) as [k, v]}<div>
      <dt>{v}</dt>
      <dd>{counts[k as keyof typeof actions] ?? 0}</dd>
    </div>{/each}
</dl>
