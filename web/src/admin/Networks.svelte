<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError } from '../api/client.ts';
  import type { AllowedNetwork } from '../api/types.ts';
  import { text } from '../text.ts';
  import { problemText } from './model.ts';

  const copy = text.access;
  let items = $state<AllowedNetwork[]>([]);
  let network = $state('');
  let note = $state('');
  let busy = $state(false);
  let loading = $state(true);
  let problem = $state('');
  let problemAt = $state('list');
  const controller = new AbortController();

  function showProblem(error: unknown, target: string) {
    problem = problemText(error);
    if (error instanceof ApiError && error.code === 'network_lockout')
      problem += ` ${copy.networkLockout}`;
    problemAt = target;
  }

  async function load() {
    if (problemAt === 'list') problem = '';
    try {
      items = (await api.listNetworks(controller.signal)).items;
    } catch (error) {
      if (!controller.signal.aborted) showProblem(error, 'list');
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    void load();
    return () => controller.abort();
  });

  async function add(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    problem = '';
    try {
      await api.addNetwork({ network: network.trim(), note: note.trim() || null });
      network = note = '';
      await load();
    } catch (error) {
      showProblem(error, 'form');
    } finally {
      busy = false;
    }
  }

  async function remove(item: AllowedNetwork) {
    if (busy) return;
    busy = true;
    problem = '';
    try {
      await api.removeNetwork(item.id);
      await load();
    } catch (error) {
      showProblem(error, item.id);
    } finally {
      busy = false;
    }
  }
</script>

<div class="access-toolbar"><h2>{copy.networks}</h2></div>
<div class="access-columns with-editor">
  <div class="access-panel table-scroll">
    {#if problem && problemAt === 'list'}<p class="form-error" role="alert">{problem}</p>
      <button type="button" onclick={load}>{text.retry}</button>{/if}
    {#if loading}<p role="status">{copy.loading}</p>{/if}
    <table class="access-table" aria-label={copy.networks}>
      <thead><tr><th>{copy.network}</th><th>{copy.note}</th><th>{copy.actions}</th></tr></thead>
      <tbody
        >{#each items as item (item.id)}
          <tr
            ><th scope="row" class="mono">{item.network}</th><td>{item.note ?? '—'}</td><td>
              <button type="button" onclick={() => remove(item)} disabled={busy}
                >{copy.remove}</button
              >
              {#if problem && problemAt === item.id}<p class="form-error" role="alert">
                  {problem}
                </p>{/if}
            </td></tr
          >
        {:else}<tr><td colspan="3">{loading ? copy.loading : copy.unrestricted}</td></tr
          >{/each}</tbody
      >
    </table>
  </div>
  <form class="access-panel editor" onsubmit={add} aria-label={copy.addNetwork}>
    <h3>{copy.addNetwork}</h3>
    <p class="muted">{copy.networkHint}</p>
    <label
      >{copy.network}<input
        required
        bind:value={network}
        disabled={busy}
        placeholder="10.0.5.0/24"
        aria-invalid={problemAt === 'form' && !!problem}
      /></label
    >
    <label>{copy.note}<input bind:value={note} disabled={busy} /></label>
    {#if problem && problemAt === 'form'}<p class="form-error" role="alert">{problem}</p>{/if}
    <button class="primary" type="submit" disabled={busy}>{copy.addNetwork}</button>
  </form>
</div>
