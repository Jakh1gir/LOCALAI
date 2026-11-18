<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Backup } from '../api/types.ts';
  import { problemText, showTime } from '../admin/model.ts';
  import { text } from '../text.ts';
  import BackupProgress from './BackupProgress.svelte';

  let {
    backup,
    onRestored,
    onCancel,
  }: {
    backup: Backup;
    onRestored: (createdAt: string) => void;
    onCancel: () => void;
  } = $props();
  const copy = text.backups;
  let dialog = $state<HTMLDialogElement>();
  let passwordInput = $state<HTMLInputElement>();
  let password = $state('');
  let busy = $state(false);
  let problem = $state('');

  onMount(() => {
    const modal = dialog;
    const opener = document.activeElement;
    modal?.showModal();
    passwordInput?.focus();
    return () => {
      modal?.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  });

  function cancel() {
    if (busy) return;
    password = '';
    onCancel();
  }

  async function restore(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    problem = '';
    try {
      const result = await api.restoreBackup(backup.name, password);
      password = '';
      onRestored(result.restored.created_at);
    } catch (error) {
      problem = problemText(error);
    } finally {
      busy = false;
    }
  }
</script>

<dialog
  class="access backup-restore"
  bind:this={dialog}
  aria-labelledby="restore-title"
  aria-describedby="restore-warning"
  oncancel={(event) => {
    event.preventDefault();
    cancel();
  }}
  onclose={cancel}
  onkeydown={(event) => event.stopPropagation()}
>
  <h2 id="restore-title">{copy.restoreTitle(showTime(backup.created_at))}</h2>
  <p class="backup-warning" id="restore-warning">{copy.restoreWarning}</p>
  <p class="muted">{copy.sessionsWarning}</p>
  <form onsubmit={restore} aria-busy={busy}>
    <label
      >{copy.password}<input
        bind:this={passwordInput}
        type="password"
        required
        autocomplete="off"
        bind:value={password}
        disabled={busy}
        aria-invalid={!!problem}
        aria-describedby={problem ? 'restore-problem' : undefined}
      /></label
    >
    {#if problem}<p id="restore-problem" class="form-error" role="alert">{problem}</p>{/if}
    {#if busy}<BackupProgress label={copy.restoring} />{/if}
    <div class="form-actions">
      <button type="button" disabled={busy} onclick={cancel}>{text.access.cancel}</button>
      <button class="primary" type="submit" disabled={busy}>{copy.restore}</button>
    </div>
  </form>
</dialog>
