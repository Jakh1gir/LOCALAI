<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { api, sessionEvents } from './api/client.ts';
  import type { Me } from './api/types.ts';
  import Icon from './Icon.svelte';
  import Login from './Login.svelte';
  import PasswordChange from './PasswordChange.svelte';
  import { findSection, homePath, sections } from './routes.ts';
  import { gateForError, gateForMe, type Gate } from './session.ts';
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
  let gate = $state<Gate>('checking');
  let me = $state<Me | null>(null);
  let setupRequired = $state(false);
  let notice = $state('');
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
      if (!request.signal.aborted) {
        connection = 'ready';
        setupRequired = result.setup_required === true;
      }
    } catch (error) {
      if (request.signal.aborted) return;
      connection = 'error';
      connectionDetail = error instanceof Error ? error.message : text.networkError;
    }
  }

  async function loadSession() {
    try {
      signedIn(await api.me());
    } catch (error) {
      me = null;
      gate = gateForError(error);
    }
  }

  function signedIn(next: Me) {
    me = next;
    notice = '';
    gate = gateForMe(next);
  }

  async function signOut() {
    try {
      await api.logout();
    } catch {
      // Сессия могла уже закрыться на сервере; экран входа нужен в любом случае.
    }
    me = null;
    gate = 'login';
  }

  onMount(() => {
    void checkConnection();
    void loadSession();
    // Сессия истекла или закрыта администратором посреди работы.
    const lost = () => {
      if (gate !== 'ready') return;
      me = null;
      notice = text.sessionLost;
      gate = 'login';
    };
    sessionEvents.addEventListener('signed-out', lost);
    return () => {
      load?.abort();
      sessionEvents.removeEventListener('signed-out', lost);
    };
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
    if (!dialog || dialog.open) return; // на экране входа поиска нет
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

{#if gate === 'checking'}
<p class="auth-checking" role="status">{text.checking}</p>
{:else if gate === 'login'}
<Login {setupRequired} {notice} onSignedIn={signedIn} />
{:else if gate === 'change-password' && me}
<PasswordChange {me} onChanged={signedIn} />
{:else}
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
          {#if me}
            <span class="profile-slot" title={me.role.name}><Icon name="profile" size={19} /><span>{me.user.name}</span></span>
            <button class="sign-out" type="button" onclick={signOut}>{text.signOut}</button>
          {:else}
            <span class="profile-slot" title={text.profile}><Icon name="profile" size={19} /><span>{text.profile}</span></span>
          {/if}
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
{/if}
