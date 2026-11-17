<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { api } from './api/client.ts';
  import Icon from './Icon.svelte';
  import { findSection, homePath, sections } from './routes.ts';
  import { text } from './text.ts';

  const mockMode = import.meta.env.DEV && import.meta.env.MODE === 'development';
  let pathname = $state(location.pathname);
  let current = $derived(findSection(pathname));
  let title = $derived(current?.title ?? text.notFound);
  let connection = $state<'pending' | 'ready' | 'error'>('pending');
  let connectionDetail = $state('');
  let heading: HTMLHeadingElement;
  let dialog: HTMLDialogElement;
  let searchInput: HTMLInputElement;
  let keywords = $state('');
  let load: AbortController | undefined;
  let found = $derived(sections.filter((item) =>
    item.title.toLocaleLowerCase('ru').includes(keywords.trim().toLocaleLowerCase('ru')),
  ));

  $effect(() => {
    document.title = `${title} · ${text.app}`;
  });

  async function checkConnection() {
    load?.abort();
    const request = new AbortController();
    load = request;
    connection = 'pending';
    connectionDetail = '';
    try {
      const result = await api.health(request.signal);
      if (result.status !== 'ok') throw new Error(text.invalidResponse);
      if (!request.signal.aborted) connection = 'ready';
    } catch (error) {
      if (request.signal.aborted) return;
      connection = 'error';
      connectionDetail = error instanceof Error ? error.message : text.networkError;
    }
  }

  onMount(() => {
    void checkConnection();
    return () => load?.abort();
  });

  function navigate(path: string) {
    dialog?.close();
    if (location.pathname !== path) history.pushState(null, '', path);
    pathname = location.pathname;
    void tick().then(() => heading.focus());
  }

  function follow(event: MouseEvent, path: string) {
    if (event.button || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    navigate(path);
  }

  function openSearch() {
    if (dialog.open) return;
    dialog.showModal();
    searchInput.focus();
  }

  function keyboard(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openSearch();
    }
  }
</script>

<svelte:window onkeydown={keyboard} onpopstate={() => { pathname = location.pathname; }} />

<a class="skip-link" href="#content">{text.skipToContent}</a>
<div class="app">
  <aside class="sidebar">
    <a class="brand" href={homePath} onclick={(event) => follow(event, homePath)}>
      <span class="brand-mark"><Icon name="logo" size={23} /></span>
      <span>{text.app}</span>
    </a>
    <nav aria-label={text.navigation}>
      <div class="nav-title" aria-hidden="true">{text.navigation}</div>
      {#each sections as item (item.path)}
        <a
          href={item.path}
          aria-current={current?.path === item.path ? 'page' : undefined}
          onclick={(event) => follow(event, item.path)}
        >
          <Icon name={item.icon} size={18} />
          <span>{item.title}</span>
        </a>
      {/each}
    </nav>
  </aside>

  <div class="workspace">
    <header class="topbar">
      <div class="connection">
        <span role="status" class="connection-state" data-state={connection} title={connectionDetail || undefined}>
          <span class="connection-dot" aria-hidden="true"></span>
          {#if connection === 'pending'}
            {text.checking}
          {:else if connection === 'error'}
            {text.unavailable}
          {:else}
            {mockMode ? text.mockReady : text.serverReady}
          {/if}
        </span>
        {#if connection === 'error'}
          <button class="retry" type="button" onclick={checkConnection}>{text.retry}</button>
        {/if}
      </div>
      <div class="top-actions">
        <button class="search-trigger" type="button" onclick={openSearch} aria-label={text.search}>
          <Icon name="search" size={18} />
          <span>{text.search}</span>
          <kbd aria-hidden="true">{text.searchShortcut}</kbd>
        </button>
        <div class="reserved">
          <span class="task-slot" title={text.tasks}><Icon name="tasks" size={19} /><span>{text.tasks}</span></span>
          <span class="profile-slot" title={text.profile}><Icon name="profile" size={19} /><span>{text.profile}</span></span>
        </div>
      </div>
    </header>

    <main id="content" tabindex="-1" class="content" aria-labelledby="page-title">
      <h1 id="page-title" bind:this={heading} tabindex="-1">{title}</h1>
      <div class="empty">
        <span class="empty-icon"><Icon name={current?.icon ?? 'missing'} size={30} /></span>
        {#if current}
          <p>{text.sectionPending}</p>
        {:else}
          <a href={homePath} onclick={(event) => follow(event, homePath)}>{text.backToRegistry}</a>
        {/if}
      </div>
    </main>
  </div>
</div>

<dialog bind:this={dialog} aria-labelledby="search-title" onclose={() => { keywords = ''; }}>
  <div class="dialog-head">
    <h2 id="search-title">{text.search}</h2>
    <button class="icon-button" type="button" onclick={() => dialog.close()} aria-label={text.close}>
      <Icon name="close" />
    </button>
  </div>
  <form onsubmit={(event) => { event.preventDefault(); if (found[0]) navigate(found[0].path); }}>
    <input type="search" bind:this={searchInput} bind:value={keywords} aria-label={text.searchSections} placeholder={text.searchSections} />
  </form>
  {#if found.length}
    <ul class="search-results">
      {#each found as item (item.path)}
        <li>
          <a href={item.path} onclick={(event) => follow(event, item.path)}>
            <Icon name={item.icon} size={18} />{item.title}
          </a>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="no-results" role="status">{text.noSections}</p>
  {/if}
</dialog>
