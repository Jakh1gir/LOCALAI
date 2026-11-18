<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Role, RoleDetails } from '../api/types.ts';
  import { text } from '../text.ts';
  import { problemText } from './model.ts';
  import Permissions from './Permissions.svelte';
  import ConfirmAction from './ConfirmAction.svelte';

  let {
    onChanged,
    onDirty,
  }: {
    onChanged: () => Promise<void>;
    onDirty: (dirty: boolean) => void;
  } = $props();
  const copy = text.access;
  let roles = $state<Role[]>([]);
  let selected = $state<RoleDetails | null>(null);
  let editing = $state<Role | 'new' | null>(null);
  let name = $state('');
  let description = $state('');
  let copyFrom = $state('');
  let dirty = $state(false);
  let loading = $state(true);
  let busy = $state(false);
  let problem = $state('');
  let problemAt = $state('list');
  let pending = $state<Role | null>(null);

  function changed(value: boolean) {
    dirty = value;
    onDirty(value);
  }
  const controller = new AbortController();
  let selection: AbortController | undefined;

  function canLeave() {
    return !dirty || window.confirm(`${copy.leaveTitle}. ${copy.leaveHint}`);
  }

  async function load() {
    loading = true;
    if (problemAt === 'list') problem = '';
    try {
      roles = (await api.listRoles(controller.signal)).items;
    } catch (error) {
      if (!controller.signal.aborted) showProblem(error, 'list');
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    void load();
    return () => {
      controller.abort();
      selection?.abort();
    };
  });

  function showProblem(error: unknown, target: string) {
    problem = problemText(error);
    problemAt = target;
  }

  function edit(role: Role | 'new', source = '') {
    editing = role;
    name = role === 'new' ? '' : role.name;
    description = role === 'new' ? '' : (role.description ?? '');
    copyFrom = source;
    problem = '';
  }

  async function select(role: Role) {
    if (busy || !canLeave()) return;
    selection?.abort();
    const request = new AbortController();
    selection = request;
    selected = null;
    changed(false);
    problem = '';
    try {
      const details = await api.getRole(role.id, request.signal);
      if (!request.signal.aborted) selected = details;
    } catch (error) {
      if (!request.signal.aborted) showProblem(error, role.id);
    }
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !editing) return;
    busy = true;
    problem = '';
    try {
      const role =
        editing === 'new'
          ? await api.createRole({ name, description, copy_from: copyFrom || null })
          : await api.updateRole(editing.id, { name, description });
      if (selected?.id === role.id)
        selected = { ...selected, name: role.name, description: role.description };
      editing = null;
      await onChanged();
      await load();
    } catch (error) {
      showProblem(error, 'form');
    } finally {
      busy = false;
    }
  }

  async function toggle(role: Role) {
    if (busy) return;
    busy = true;
    problem = '';
    try {
      const updated = role.archived_at
        ? await api.restoreRole(role.id)
        : await api.archiveRole(role.id);
      if (selected?.id === role.id) selected = { ...selected, archived_at: updated.archived_at };
      await onChanged();
      await load();
    } catch (error) {
      showProblem(error, role.id);
    } finally {
      busy = false;
    }
  }

  function requestToggle(role: Role) {
    if (busy) return;
    if (role.archived_at) void toggle(role);
    else pending = role;
  }

  function confirmToggle() {
    if (!pending) return;
    const role = pending;
    pending = null;
    void toggle(role);
  }

  async function saved(role: RoleDetails) {
    selected = role;
    await onChanged();
    await load();
  }
</script>

<div class="access-toolbar">
  <h2>{copy.roles}</h2>
  <button class="primary" type="button" onclick={() => edit('new')} disabled={busy}
    >{copy.createRole}</button
  >
</div>
{#if problem && problemAt === 'list'}<p class="form-error" role="alert">{problem}</p>
  <button type="button" onclick={load}>{text.retry}</button>{/if}
{#if loading}<p role="status">{copy.loading}</p>{/if}
<div class="access-columns" class:with-editor={editing !== null}>
  <div class="access-panel table-scroll">
    <table class="access-table" aria-label={copy.roles}>
      <thead
        ><tr
          ><th>{copy.role}</th><th>{copy.userCount}</th><th>{copy.state}</th><th>{copy.actions}</th
          ></tr
        ></thead
      >
      <tbody
        >{#each roles as role (role.id)}
          <tr class:selected={role.id === selected?.id}>
            <th scope="row"
              ><button
                class="text-button"
                type="button"
                onclick={() => select(role)}
                disabled={busy}>{role.name}</button
              >
              <p class="cell-description">{role.description ?? ''}</p></th
            >
            <td>{role.users}</td><td
              ><span class="state-badge" class:muted={!role.archived_at}
                >{role.archived_at ? copy.disabled : copy.enabled}</span
              ></td
            >
            <td
              ><div class="row-actions">
                <button type="button" onclick={() => select(role)} disabled={busy}
                  >{copy.permissions}</button
                >
                <button type="button" onclick={() => edit(role)} disabled={busy}>{copy.edit}</button
                >
                <button type="button" onclick={() => edit('new', role.id)} disabled={busy}
                  >{copy.copy}</button
                >
                <button type="button" onclick={() => requestToggle(role)} disabled={busy}
                  >{role.archived_at ? copy.restore : copy.disable}</button
                >
              </div>
              {#if problem && problemAt === role.id}<p class="form-error" role="alert">
                  {problem}
                </p>{/if}</td
            >
          </tr>
        {/each}</tbody
      >
    </table>
  </div>
  {#if editing}
    <form
      class="access-panel editor"
      onsubmit={save}
      aria-label={editing === 'new' ? copy.createRole : copy.editRole}
    >
      <h3>{editing === 'new' ? copy.createRole : copy.editRole}</h3>
      <label>{copy.roleName}<input bind:value={name} required disabled={busy} /></label>
      <label
        >{copy.description}<textarea bind:value={description} rows="3" disabled={busy}
        ></textarea></label
      >
      {#if editing === 'new'}<label
          >{copy.copyFrom}<select aria-label={copy.copyFrom} bind:value={copyFrom} disabled={busy}>
            <option value="">{copy.noCopy}</option>
            {#each roles as role (role.id)}<option value={role.id}>{role.name}</option>{/each}
          </select></label
        >{/if}
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
{#if selected}
  {#key selected.id}<Permissions
      role={selected}
      onSaved={saved}
      onDirty={changed}
      onBusy={(value) => {
        busy = value;
      }}
    />{/key}
{/if}

{#if pending}
  <ConfirmAction
    title={copy.confirmDisableRole(pending.name)}
    consequence={copy.disableRoleConsequence(pending.users)}
    action={copy.disableRoleAction}
    onConfirm={confirmToggle}
    onCancel={() => {
      pending = null;
    }}
  />
{/if}
