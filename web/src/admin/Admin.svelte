<script lang="ts">
  import type { Me } from '../api/types.ts';
  import { text } from '../text.ts';
  import Users from './Users.svelte';
  import Roles from './Roles.svelte';
  import Networks from './Networks.svelte';
  import KksRules from './KksRules.svelte';
  import './admin.css';

  let {
    me,
    onChanged,
    onDirty,
  }: {
    me: Me;
    onChanged: () => Promise<void>;
    onDirty: (value: boolean) => void;
  } = $props();
  let tab = $state<'users' | 'roles' | 'networks' | 'kksRules'>('users');
  let dirty = $state(false);
  const tabs = ['users', 'roles', 'networks', 'kksRules'] as const;

  function changed(value: boolean) {
    dirty = value;
    onDirty(value);
  }

  function switchTab(next: typeof tab) {
    if (next === tab) return;
    if (dirty && !window.confirm(`${text.access.leaveTitle}. ${text.access.leaveHint}`)) return;
    changed(false);
    tab = next;
  }
</script>

<section class="access" aria-label={text.admin}>
  <nav class="access-tabs" aria-label={text.admin}>
    {#each tabs as item}
      <button
        type="button"
        aria-current={tab === item ? 'page' : undefined}
        onclick={() => switchTab(item)}>{text.access[item]}</button
      >
    {/each}
  </nav>
  {#if tab === 'users'}
    <Users {me} {onChanged} />
  {:else if tab === 'roles'}
    <Roles {onChanged} onDirty={changed} />
  {:else if tab === 'networks'}
    <Networks />
  {:else}
    <KksRules {me} />
  {/if}
</section>
