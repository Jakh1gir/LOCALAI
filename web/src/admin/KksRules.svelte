<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { KksResolution, KksRules, Me, NamedEntity } from '../api/types.ts';
  import { text } from '../text.ts';
  import { problemText } from './model.ts';

  let { me }: { me: Me } = $props();
  const copy = text.kksRule;
  let canEdit = $derived(me.permissions.sections.includes('structure'));
  let current = $state<KksRules>();
  let sites = $state<NamedEntity[]>([]);
  let pattern = $state('');
  let siteId = $state('');
  let code = $state('');
  let saving = $state(false);
  let checking = $state(false);
  let loadProblem = $state('');
  let patternProblem = $state('');
  let checkProblem = $state('');
  let notice = $state('');
  let result = $state<KksResolution>();
  const controller = new AbortController();
  let checkRequest: AbortController | undefined;

  async function load() {
    loadProblem = '';
    try {
      const [rule, availableSites] = await Promise.all([
        api.getKksRules(controller.signal),
        api.listSites(controller.signal),
      ]);
      current = rule;
      pattern = rule.signal_pattern;
      sites = availableSites.items;
      siteId = sites[0]?.id ?? '';
    } catch (error) {
      if (!controller.signal.aborted) loadProblem = problemText(error);
    }
  }

  onMount(() => {
    void load();
    return () => {
      controller.abort();
      checkRequest?.abort();
    };
  });

  function clearResult() {
    checkRequest?.abort();
    result = undefined;
    checkProblem = '';
    checking = false;
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (saving || !canEdit) return;
    saving = true;
    patternProblem = '';
    notice = '';
    try {
      const rule = await api.setKksRules(pattern, controller.signal);
      current = rule;
      pattern = rule.signal_pattern;
      clearResult();
      notice = copy.saved;
    } catch (error) {
      if (!controller.signal.aborted) patternProblem = problemText(error);
    } finally {
      saving = false;
    }
  }

  async function check(event: SubmitEvent) {
    event.preventDefault();
    if (!siteId || !code.trim()) return;
    clearResult();
    const request = new AbortController();
    checkRequest = request;
    checking = true;
    try {
      const resolved = await api.resolveKks(siteId, code.trim(), request.signal);
      if (!request.signal.aborted) result = resolved;
    } catch (error) {
      if (!request.signal.aborted) checkProblem = problemText(error);
    } finally {
      if (!request.signal.aborted) checking = false;
    }
  }
</script>

<div class="access-toolbar"><h2>{text.access.kksRules}</h2></div>
{#if loadProblem}
  <p class="form-error" role="alert">{loadProblem}</p>
  <button type="button" onclick={load}>{text.retry}</button>
{:else if !current}
  <p role="status">{text.access.loading}</p>
{:else}
  <div class="access-columns rules-columns">
    <section class="access-panel editor" aria-labelledby="current-rule-title">
      <h3 id="current-rule-title">{copy.current}</h3>
      <p class="muted">{copy.scope}</p>
      <code class="rule-pattern">{current.signal_pattern}</code>
      <div class="rule-example" aria-label={copy.example}>
        <h4>{copy.example}</h4>
        <p class="mono">{current.example.code}</p>
        <dl class="rule-result">
          <dt>{copy.object}</dt>
          <dd class="mono">{current.example.object}</dd>
          <dt>{copy.signal}</dt>
          <dd class="mono">{current.example.signal ?? copy.noSignal}</dd>
        </dl>
      </div>
      {#if canEdit}
        <form class="rule-form" onsubmit={save} aria-label={copy.save}>
          <label
            >{copy.pattern}<input
              class="mono"
              bind:value={pattern}
              required
              maxlength="200"
              spellcheck="false"
              disabled={saving}
              aria-invalid={!!patternProblem}
              aria-describedby={patternProblem ? 'pattern-problem' : undefined}
              oninput={() => {
                patternProblem = '';
                notice = '';
              }}
            /></label
          >
          {#if patternProblem}<p id="pattern-problem" class="form-error" role="alert">
              {patternProblem}
            </p>{/if}
          {#if notice}<p role="status">{notice}</p>{/if}
          <button
            class="primary"
            type="submit"
            disabled={saving || pattern === current.signal_pattern}>{copy.save}</button
          >
        </form>
      {:else}<p class="muted">{copy.readOnly}</p>{/if}
    </section>
    <section class="access-panel editor" aria-labelledby="check-code-title">
      <h3 id="check-code-title">{copy.check}</h3>
      {#if !sites.length}<p role="status">{copy.noSites}</p>{/if}
      <form class="rule-form" onsubmit={check} aria-label={copy.check}>
        <label
          >{copy.site}<select
            aria-label={copy.site}
            bind:value={siteId}
            onchange={clearResult}
            disabled={!sites.length}
          >
            {#each sites as site (site.id)}<option value={site.id}>{site.name}</option>{/each}
          </select></label
        >
        <label
          >{copy.code}<input
            class="mono"
            bind:value={code}
            required
            spellcheck="false"
            oninput={clearResult}
            disabled={!sites.length}
            aria-invalid={!!checkProblem}
            aria-describedby={checkProblem ? 'resolve-problem' : undefined}
          /></label
        >
        {#if checkProblem}<p id="resolve-problem" class="form-error" role="alert">
            {checkProblem}
          </p>{/if}
        <button type="submit" disabled={checking || !siteId || !code.trim()}
          >{checking ? text.access.loading : copy.check}</button
        >
      </form>
      {#if result}
        <div class="rule-example" role="status" aria-label={copy.found}>
          <h4>{copy.found}</h4>
          <dl class="rule-result">
            <dt>{copy.object}</dt>
            <dd class="mono">{result.code}</dd>
            {#if result.signal}<dt>{copy.signal}</dt>
              <dd class="mono">{result.signal}</dd>{/if}
            <dt>{copy.foundBy}</dt>
            <dd>{copy.via[result.via]}</dd>
          </dl>
        </div>
      {/if}
    </section>
  </div>
{/if}
