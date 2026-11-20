<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { api } from '../api/client.ts';
  import type { SearchResult } from '../api/types.ts';
  import Icon from '../Icon.svelte';
  import { text } from '../text.ts';
  import { resultGroups, type Section } from './model.ts';
  import './search.css';

  let {
    sections,
    onNavigate,
    onClose,
  }: {
    sections: readonly Section[];
    onNavigate: (path: string) => void;
    onClose: () => void;
  } = $props();
  let dialog: HTMLDialogElement;
  let input: HTMLInputElement;
  let keywords = $state('');
  let result = $state<SearchResult | null>(null);
  let pending = $state(false);
  let problem = $state('');
  let active = $state(0);
  let groups = $derived(resultGroups(result, sections, keywords));
  let options = $derived(groups.flatMap((group) => group.items));

  onMount(() => {
    const opener = document.activeElement;
    dialog.showModal();
    input.focus();
    return () => {
      dialog.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  });

  $effect(() => {
    const query = keywords.trim();
    result = null;
    problem = '';
    active = 0;
    pending = !!query;
    if (!query) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const next = await api.search(query, controller.signal);
        if (!controller.signal.aborted) {
          result = next;
          active = 0;
        }
      } catch (error) {
        if (!controller.signal.aborted)
          problem = error instanceof Error ? error.message : text.networkError;
      } finally {
        if (!controller.signal.aborted) pending = false;
      }
    }, 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  });

  function keyboard(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!options.length) return;
      active = (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      void tick().then(() =>
        document
          .getElementById(`option-${options[active]?.key}`)
          ?.scrollIntoView({ block: 'nearest' }),
      );
    } else if (event.key === 'Enter' && event.target === input) {
      event.preventDefault();
      if (options[active]) onNavigate(options[active].href);
    }
  }

  function follow(event: MouseEvent, href: string) {
    if (event.button || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    onNavigate(href);
  }
</script>

<dialog
  class="global-search"
  bind:this={dialog}
  aria-labelledby="search-title"
  oncancel={(event) => {
    event.preventDefault();
    onClose();
  }}
  onkeydown={keyboard}
>
  <div class="dialog-head">
    <h2 id="search-title">{text.search}</h2>
    <button class="icon-button" type="button" onclick={onClose} aria-label={text.close}
      ><Icon name="close" /></button
    >
  </div>
  <input
    bind:this={input}
    type="search"
    role="combobox"
    bind:value={keywords}
    maxlength="200"
    aria-label={text.searchSections}
    placeholder={text.searchSections}
    aria-autocomplete="list"
    aria-expanded="true"
    aria-controls="global-search-results"
    aria-activedescendant={options[active] ? `option-${options[active].key}` : undefined}
  />
  <p class="search-hint">{text.find.hint}</p>
  {#if pending}<p role="status" class="search-status">{text.find.loading}</p>{/if}
  {#if problem}<p role="alert" class="form-error">{problem}</p>{/if}
  <div
    id="global-search-results"
    role="listbox"
    aria-label={text.search}
    aria-busy={pending}
    data-result-query={result?.query}
  >
    {#each groups as group}
      {#if group.items.length}<section role="group" aria-label={group.title}>
          <h3>{group.title}</h3>
          <ul class="search-results">
            {#each group.items as item (item.key)}
              <li
                id={`option-${item.key}`}
                role="option"
                aria-selected={options[active]?.key === item.key}
              >
                <a
                  href={item.href}
                  tabindex="-1"
                  onclick={(event) => follow(event, item.href)}
                  onpointermove={() => {
                    active = options.findIndex((option) => option.key === item.key);
                  }}
                >
                  <span class="search-option"
                    ><span class="search-title">{item.title}</span>
                    {#if item.badge}<span class="search-badge">{item.badge}</span>{/if}
                    {#if item.detail}<span class="search-detail">{item.detail}</span>{/if}</span
                  ></a
                >
              </li>
            {/each}
          </ul>
        </section>{/if}
    {/each}
  </div>
  {#if result?.more.records}<p class="search-hint">{text.find.more(result.records.length)}</p>{/if}
  {#if !pending && !problem && !options.length}<p role="status" class="no-results">
      {text.find.empty}
    </p>{/if}
</dialog>
