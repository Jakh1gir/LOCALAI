<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { api, sessionEvents } from './api/client.ts';
  import type { Me } from './api/types.ts';
  import Icon from './Icon.svelte';
  import Knowledge from './knowledge/Knowledge.svelte';
  import DocumentAction from './knowledge/DocumentAction.svelte';
  import { observe, type Queue } from './knowledge/model.ts';
  import { knowledgeSearch } from './knowledge/find.svelte.ts';
  import Chat from './chat/Chat.svelte';
  import Unread from './chat/Unread.svelte';
  import Registry from './registry/Registry.svelte';
  import TargetView from './search/TargetView.svelte';
  import { searchTarget } from './search/target.ts';
  import { events } from './chat/events.ts';
  import Login from './Login.svelte';
  import PasswordChange from './PasswordChange.svelte';
  import Admin from './admin/Admin.svelte';
  import Wizard from './imports/Wizard.svelte';
  import { allowed } from './imports/model.ts';
  import Backups from './backups/Backups.svelte';
  import { showTime } from './admin/model.ts';
  import { findSection, homePath, sections } from './routes.ts';
  import { gateForError, gateForMe, type Gate } from './session.ts';
  import { text } from './text.ts';

  const mockMode = import.meta.env.DEV && import.meta.env.MODE === 'development';
  let pathname = $state(location.pathname);
  let query = $state(location.search);
  let target = $derived(searchTarget(pathname, query));
  let datasetId = $derived(new URLSearchParams(query).get('dataset') ?? '');
  let draft = $state('');
  const readEvents = new EventTarget();
  let current = $derived(findSection(pathname));
  let title = $derived(current?.title ?? text.notFound);
  let connection = $state<'pending' | 'ready' | 'error'>('pending');
  let connectionDetail = $state('');
  let heading = $state<HTMLHeadingElement>();
  let dialog = $state<HTMLDialogElement>();
  let searchInput = $state<HTMLInputElement>();
  let keywords = $state('');
  let load: AbortController | undefined;
  let sessionLoad: AbortController | undefined;
  let gate = $state<Gate>('checking');
  let me = $state<Me | null>(null);
  let setupRequired = $state(false);
  let notice = $state('');
  let adminDirty = $state(false);
  let canImport = $derived(gate === 'ready' && !!me && allowed(me.permissions));
  let canAdmin = $derived(gate === 'ready' && me?.permissions.sections.includes('admin') === true);
  let canBackups = $derived(gate === 'ready' && me?.permissions.sections.includes('backups') === true);
  let canChat = $derived(gate === 'ready' && !!me?.permissions.sections.includes('chat'));
  let canWrite = $derived(canChat && !!me?.permissions.sections.includes('chat_write'));
  let sessionKey = $derived(gate === 'ready' && me ? JSON.stringify([me.user.id, me.permissions]) : '');
  $effect(() => {
    if (!sessionKey) return;
    const controller = new AbortController();
    void events.listen(controller.signal);
    return () => controller.abort();
  });
  let visibleSections = $derived(sections.filter((item) =>
    (item.path !== '/imports' || canImport) &&
    (item.path !== '/chat' || canChat) && (item.path !== '/admin' || canAdmin) && (item.path !== '/backups' || canBackups),
  ));
  let commands = $derived(visibleSections.filter((item) =>
    item.title.toLocaleLowerCase('ru').includes(keywords.trim().toLocaleLowerCase('ru')),
  ));

  $effect(() => {
    document.title = `${title} · ${text.app}`;
  });

  let canReview = $derived(gate === 'ready' && !!me?.permissions.sections.includes('knowledge'));
  let reviewQueue = $state<Queue | null>(null);
  let reviewError = $state('');
  let documentContext = $derived(new URLSearchParams(query));
  const noteSearch = knowledgeSearch(() => sessionKey ? keywords : '');
  $effect(() => {
    const key = sessionKey;
    reviewQueue = null;
    reviewError = '';
    if (key && canReview) return observe('knowledge_note', api.knowledge.queue,
      result => { reviewQueue = result; reviewError = ''; },
      message => { reviewQueue = null; reviewError = message; });
  });

  let found = $derived([
    ...noteSearch.groups.flatMap(group => group.items.map((item, i) => ({
      path: item.href, title: item.title, detail: item.detail, heading: i === 0 ? group.title : '',
      icon: 'knowledge' as const,
    }))),
    ...commands.map(item => ({ ...item, detail: '', heading: '' })),
  ]);

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
    sessionLoad?.abort();
    const request = new AbortController();
    sessionLoad = request;
    try {
      const next = await api.me(request.signal);
      if (!request.signal.aborted) signedIn(next);
    } catch (error) {
      if (request.signal.aborted) return;
      if (gateForError(error) === 'offline' && gate === 'ready') {
        connection = 'error';
        connectionDetail = error instanceof Error ? error.message : text.networkError;
        return;
      }
      me = null;
      gate = gateForError(error);
    }
  }

  function signedIn(next: Me) {
    sessionLoad?.abort();
    if (me?.user.id !== next.user.id || !next.permissions.sections.includes('chat_write')) draft = '';
    me = next;
    notice = '';
    gate = gateForMe(next);
  }

  function restored(createdAt: string) {
    sessionLoad?.abort();
    me = null;
    adminDirty = false;
    notice = text.backups.restoredNotice(showTime(createdAt));
    gate = 'login';
  }

  async function signOut() {
    sessionLoad?.abort();
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
    const permissionsChanged = () => { void loadSession(); };
    sessionEvents.addEventListener('permissions-changed', permissionsChanged);
    return () => {
      load?.abort();
      sessionLoad?.abort();
      sessionEvents.removeEventListener('signed-out', lost);
      sessionEvents.removeEventListener('permissions-changed', permissionsChanged);
    };
  });

  function navigate(path: string) {
    if (location.pathname + location.search === path) { dialog?.close(); return; }
    if (adminDirty && !window.confirm(`${text.access.leaveTitle}. ${text.access.leaveHint}`)) return;
    adminDirty = false;
    dialog?.close();
    history.pushState(null, '', path);
    pathname = location.pathname;
    query = location.search;
    void tick().then(() => heading?.focus());
  }

  function share(id: string) {
    if (!canWrite) return;
    draft += (draft ? ' ' : '') + '[[record:' + id + ']]';
    navigate('/chat');
  }

  function follow(event: MouseEvent, path: string) {
    if (event.button || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    navigate(path);
  }

  function followTarget(event: MouseEvent) {
    if (event.defaultPrevented || !(event.target instanceof Element)) return;
    const link = event.target.closest('a');
    if (!link || link.target || link.origin !== location.origin || !searchTarget(link.pathname, link.search)) return;
    follow(event, link.pathname + link.search);
  }

  function openSearch() {
    if (!dialog || dialog.open) return; // на экране входа поиска нет
    dialog.showModal();
    searchInput?.focus();
  }

  function keyboard(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openSearch();
    }
  }
</script>

<svelte:window onclick={followTarget} onkeydown={keyboard} onfocus={() => { if (gate === 'ready') void loadSession(); }}
  onbeforeunload={(event) => { if (adminDirty) { event.preventDefault(); event.returnValue = ''; } }}
  onpopstate={() => {
    if (pathname === location.pathname && query === location.search) return;
    if (adminDirty && !window.confirm(`${text.access.leaveTitle}. ${text.access.leaveHint}`)) {
      history.pushState(null, '', pathname + query);
      return;
    }
    adminDirty = false;
    pathname = location.pathname;
    query = location.search;
  }} />

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
      {#each visibleSections as item (item.path)}
        <a
          href={item.path}
          aria-current={current?.path === item.path ? 'page' : undefined}
          onclick={(event) => follow(event, item.path)}
        >
          <Icon name={item.icon} size={18} />
          <span>{item.title}</span>
          {#if item.path === '/knowledge' && canReview && reviewQueue?.items.length}
            <span class="chat-badge" aria-label={'В очереди проверки: ' + reviewQueue.items.length}>{reviewQueue.items.length}{reviewQueue.more ? '+' : ''}</span>
          {/if}
          {#if item.path === '/chat'}{#key sessionKey}<Unread {readEvents} />{/key}{/if}
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
      {#if current?.path === '/imports'}
        {#if canImport && me}{#key sessionKey + query}<Wizard {me} preferred={datasetId} />{/key}
        {:else}<p role="status">{text.accessDenied}</p>{/if}
      {:else if current?.path === '/chat'}
        {#if canChat}{#key sessionKey}<Chat {canWrite} bind:draft onRead={() => readEvents.dispatchEvent(new Event('read'))} />{/key}
        {:else}<p role="status">{text.accessDenied}</p>{/if}
      {:else if target && gate === 'ready'}
        {#key sessionKey + pathname + query}<TargetView {target} share={canWrite ? share : undefined} />{/key}
      {:else if current?.path === '/registries' && gate === 'ready' && me}
        {#key sessionKey}<Registry {datasetId} permissions={me.permissions} {navigate} />{/key}
      {:else if current?.path === '/admin'}
        {#if canAdmin && me}<Admin {me} onChanged={loadSession} onDirty={(value) => { adminDirty = value; }} />
        {:else}<p role="status">{text.accessDenied}</p>{/if}
      {:else if current?.path === '/knowledge' && me && gate === 'ready'}
        {#key sessionKey + query}<Knowledge {query} manage={canReview} user={me.user.name} queue={reviewQueue} queueError={reviewError} {navigate} />{/key}
      {:else if current?.path === '/backups'}
        {#if canBackups}<Backups onRestored={restored} />
        {:else}<p role="status">{text.accessDenied}</p>{/if}
      {:else}
      <div class="empty">
        <span class="empty-icon"><Icon name={current?.icon ?? 'missing'} size={30} /></span>
        {#if current}
          <p>{text.sectionPending}</p>
        {:else}
          <a href={homePath} onclick={(event) => follow(event, homePath)}>{text.backToRegistry}</a>
        {/if}
      </div>
      {/if}
      {#if current?.path === '/documents' && gate === 'ready' && documentContext.get('document')}
        {#key sessionKey + query}<DocumentAction id={documentContext.get('document')!} page={documentContext.get('page') ?? undefined} />{/key}
      {/if}
    </main>
  </div>
</div>

<dialog bind:this={dialog} aria-labelledby="search-title" onclose={() => { keywords = ''; }}>
  <div class="dialog-head">
    <h2 id="search-title">{text.search}</h2>
    <button class="icon-button" type="button" onclick={() => dialog?.close()} aria-label={text.close}>
      <Icon name="close" />
    </button>
  </div>
  <form onsubmit={(event) => { event.preventDefault(); if (found[0]) navigate(found[0].path); }}>
    <input type="search" bind:this={searchInput} bind:value={keywords} aria-label={text.searchSections} placeholder={text.searchSections} />
  </form>
  {#if found.length}
    <ul class="search-results">
      {#each found as item (item.path)}
        {#if item.heading}<li><h3>{item.heading}</h3></li>{/if}
        <li>
          <a href={item.path} onclick={(event) => follow(event, item.path)}>
            <Icon name={item.icon} size={18} /><span>{item.title}{#if item.detail}<br />{item.detail}{/if}</span>
          </a>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="no-results" role="status">{text.noSections}</p>
  {/if}
  {#if !keywords}<p class="no-results">По местному обозначению можно найти официальный ККС в словаре станции.</p>{/if}
  {#if noteSearch.error}<p role="alert">{noteSearch.error}</p>{/if}
</dialog>
{/if}
