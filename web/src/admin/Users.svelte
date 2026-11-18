<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError } from '../api/client.ts';
  import type { Me, Role, TemporaryUser, User } from '../api/types.ts';
  import { text } from '../text.ts';
  import { problemText, showTime, userStates } from './model.ts';

  let { me, onChanged }: { me: Me; onChanged: () => Promise<void> } = $props();
  const copy = text.access;
  let users = $state<User[]>([]);
  let roles = $state<Role[]>([]);
  let loading = $state(true);
  let busy = $state(false);
  let problem = $state('');
  let problemAt = $state('list');
  let errorField = $state<string>();
  let editing = $state<User | 'new' | null>(null);
  let login = $state('');
  let name = $state('');
  let roleId = $state('');
  let secret = $state<{ login: string; password: string; own: boolean } | null>(null);
  let secretDialog = $state<HTMLDialogElement>();
  let copyNotice = $state('');
  const controller = new AbortController();

  $effect(() => {
    if (secret && secretDialog && !secretDialog.open) secretDialog.showModal();
  });

  async function load() {
    loading = true;
    if (problemAt === 'list') problem = '';
    try {
      const [nextUsers, nextRoles] = await Promise.all([
        api.listUsers(controller.signal),
        api.listRoles(controller.signal),
      ]);
      users = nextUsers.items;
      roles = nextRoles.items;
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

  function showProblem(error: unknown, target: string) {
    problem = problemText(error);
    problemAt = target;
    errorField = error instanceof ApiError ? error.field : undefined;
  }

  function edit(user: User | 'new') {
    editing = user;
    login = user === 'new' ? '' : user.login;
    name = user === 'new' ? '' : user.name;
    roleId = user === 'new' ? '' : user.role_id;
    problem = '';
    errorField = undefined;
  }

  function showSecret(user: TemporaryUser) {
    if (user.temporary_password) {
      secret = {
        login: user.login,
        password: user.temporary_password,
        own: user.id === me.user.id,
      };
      copyNotice = '';
    } else {
      problem = copy.secretUnavailable;
      problemAt = user.id;
    }
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !editing) return;
    busy = true;
    problem = '';
    try {
      if (editing === 'new')
        showSecret(await api.createUser({ login: login.trim(), name, role_id: roleId }));
      else await api.updateUser(editing.id, { name, role_id: roleId });
      editing = null;
      await onChanged();
      await load();
    } catch (error) {
      showProblem(error, 'form');
    } finally {
      busy = false;
    }
  }

  async function act(user: User, action: 'block' | 'unblock' | 'reset') {
    if (busy) return;
    busy = true;
    problem = '';
    try {
      if (action === 'reset') {
        showSecret(await api.resetPassword(user.id));
        // Своя сессия уже закрыта. Сначала даём забрать новый пароль.
        if (user.id === me.user.id) return;
      } else if (action === 'block') await api.blockUser(user.id);
      else await api.unblockUser(user.id);
      await onChanged();
      await load();
    } catch (error) {
      showProblem(error, user.id);
    } finally {
      busy = false;
    }
  }

  function closeSecret() {
    const own = secret?.own;
    secret = null;
    copyNotice = '';
    if (own) void onChanged();
  }

  async function copyPassword() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret.password);
      copyNotice = copy.copied;
    } catch {
      copyNotice = copy.copyFailed;
    }
  }
</script>

<div class="access-toolbar">
  <h2>{copy.users}</h2>
  <button class="primary" type="button" onclick={() => edit('new')} disabled={busy}
    >{copy.createUser}</button
  >
</div>
{#if problem && problemAt === 'list'}<p class="form-error" role="alert">{problem}</p>
  <button type="button" onclick={load}>{text.retry}</button>{/if}
{#if loading}<p role="status">{copy.loading}</p>{/if}
<div class:with-editor={editing !== null} class="access-columns">
  <div class="access-panel table-scroll">
    <table class="access-table" aria-label={copy.users}>
      <thead
        ><tr
          ><th>{copy.login}</th><th>{copy.name}</th><th>{copy.role}</th><th>{copy.state}</th><th
            >{copy.lastLogin}</th
          ><th>{copy.actions}</th></tr
        ></thead
      >
      <tbody>
        {#each users as user (user.id)}
          <tr>
            <th scope="row"><span class="mono">{user.login}</span></th>
            <td>{user.name}</td><td>{user.role_name}</td>
            <td
              ><div class="state-list">
                {#each userStates(user, !!roles.find((role) => role.id === user.role_id)?.archived_at) as state}
                  <span class:muted={state === copy.active} class="state-badge">{state}</span>
                {/each}
              </div></td
            >
            <td class="date-cell">{showTime(user.last_login_at)}</td>
            <td
              ><div class="row-actions">
                <button type="button" onclick={() => edit(user)} disabled={busy}>{copy.edit}</button
                >
                <button
                  type="button"
                  onclick={() => act(user, user.blocked_at ? 'unblock' : 'block')}
                  disabled={busy}>{user.blocked_at ? copy.unblock : copy.block}</button
                >
                <button type="button" onclick={() => act(user, 'reset')} disabled={busy}
                  >{copy.resetPassword}</button
                >
              </div>
              {#if problem && problemAt === user.id}<p class="form-error" role="alert">
                  {problem}
                </p>{/if}</td
            >
          </tr>
        {:else}
          <tr><td colspan="6">{loading ? copy.loading : copy.noUsers}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if editing}
    <form
      class="access-panel editor"
      onsubmit={save}
      aria-label={editing === 'new' ? copy.createUser : copy.editUser}
    >
      <h3>{editing === 'new' ? copy.createUser : copy.editUser}</h3>
      <label
        >{copy.login}<input
          bind:value={login}
          required
          disabled={editing !== 'new' || busy}
          maxlength="64"
          autocomplete="off"
          aria-invalid={errorField === 'login' && !!problem}
        /></label
      >
      <label
        >{copy.name}<input
          bind:value={name}
          required
          disabled={busy}
          aria-invalid={errorField === 'name' && !!problem}
        /></label
      >
      <label
        >{copy.role}<select
          aria-label={copy.role}
          bind:value={roleId}
          required
          disabled={busy}
          aria-invalid={errorField === 'role_id' && !!problem}
        >
          <option value="" disabled>{copy.chooseRole}</option>
          {#each roles as role (role.id)}<option value={role.id} disabled={!!role.archived_at}
              >{role.name}{role.archived_at ? ` · ${copy.disabled}` : ''}</option
            >{/each}
        </select></label
      >
      {#if problem && problemAt === 'form'}<p class="form-error" role="alert">{problem}</p>{/if}
      <div class="form-actions">
        <button class="primary" type="submit" disabled={busy}>{copy.save}</button><button
          type="button"
          onclick={() => {
            editing = null;
          }}
          disabled={busy}>{copy.cancel}</button
        >
      </div>
    </form>
  {/if}
</div>

<dialog
  class="access secret-dialog"
  bind:this={secretDialog}
  aria-labelledby="secret-title"
  onclose={closeSecret}
>
  {#if secret}
    <h2 id="secret-title">{copy.temporaryPassword}</h2>
    <p class="mono">{secret.login}</p>
    <p>{copy.oneTimeHint}</p>
    <label
      >{copy.temporaryPassword}<input
        class="mono"
        value={secret.password}
        readonly
        autocomplete="off"
      /></label
    >
    {#if copyNotice}<p role="status">{copyNotice}</p>{/if}
    <div class="form-actions">
      <button class="primary" type="button" onclick={copyPassword}>{copy.copy}</button><button
        type="button"
        onclick={() => secretDialog?.close()}>{copy.close}</button
      >
    </div>
  {/if}
</dialog>
