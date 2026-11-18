<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type { Backup, BackupEntry } from '../api/types.ts';
  import { problemText, showTime } from '../admin/model.ts';
  import { text } from '../text.ts';
  import BackupProgress from './BackupProgress.svelte';
  import RestoreDialog from './RestoreDialog.svelte';
  import '../admin/admin.css';
  import './backups.css';

  let { onRestored }: { onRestored: (createdAt: string) => void } = $props();
  const copy = text.backups;
  let items = $state<BackupEntry[]>([]);
  let password = $state('');
  let repeat = $state('');
  let label = $state('');
  let loading = $state(true);
  let creating = $state(false);
  let loadProblem = $state('');
  let formProblem = $state('');
  let notice = $state('');
  let selected = $state<Backup | null>(null);
  const controller = new AbortController();

  async function load() {
    loading = true;
    loadProblem = '';
    try {
      items = (await api.listBackups(controller.signal)).items;
    } catch (error) {
      if (!controller.signal.aborted) loadProblem = problemText(error);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void load();
    return () => controller.abort();
  });

  function size(bytes: number) {
    const mib = bytes >= 1024 * 1024;
    return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(bytes / (mib ? 1024 * 1024 : 1024))} ${mib ? copy.mib : copy.kib}`;
  }

  async function create(event: SubmitEvent) {
    event.preventDefault();
    if (creating) return;
    formProblem = '';
    notice = '';
    if (password.length < 12) {
      formProblem = copy.passwordShort;
      return;
    }
    if (password !== repeat) {
      formProblem = copy.passwordMismatch;
      return;
    }
    creating = true;
    try {
      await api.createBackup(password, label.trim());
      password = repeat = label = '';
      notice = copy.created;
      await load();
    } catch (error) {
      formProblem = problemText(error);
    } finally {
      creating = false;
    }
  }
</script>

<section class="access backups" aria-label={text.backupsTitle}>
  <div class="access-columns with-editor">
    <section class="access-panel table-scroll" aria-labelledby="backups-list-title">
      <div class="backup-list-head">
        <h2 id="backups-list-title">{copy.list}</h2>
        <button type="button" disabled={loading || creating} onclick={load}
          >{text.access.refresh}</button
        >
      </div>
      {#if loadProblem}<div class="backup-feedback">
          <p class="form-error" role="alert">{loadProblem}</p>
          <button type="button" onclick={load}>{text.retry}</button>
        </div>{/if}
      {#if loading}<p class="backup-feedback" role="status">{text.access.loading}</p>{/if}
      <table class="access-table" aria-label={copy.list}>
        <thead
          ><tr
            ><th>{copy.date}</th><th>{copy.label}</th><th>{copy.size}</th><th>{copy.schema}</th><th
              >{copy.actions}</th
            ></tr
          ></thead
        >
        <tbody>
          {#each items as item (item.name)}
            {#if 'error' in item}
              <tr class="backup-invalid"
                ><td>—</td><th scope="row"
                  ><span class="mono">{item.name}</span>
                  <p class="form-error">{item.error}</p></th
                ><td>{size(item.size)}</td><td>—</td><td>—</td></tr
              >
            {:else}
              <tr class:backup-protective={item.label === copy.protectiveLabel}>
                <td class="date-cell"
                  ><time datetime={item.created_at}>{showTime(item.created_at)}</time></td
                >
                <th scope="row"
                  >{item.label ?? copy.unknownLabel}
                  {#if item.label === copy.protectiveLabel}<span class="state-badge"
                      >{copy.protective}</span
                    >{/if}
                  <span class="cell-description mono">{item.name}</span></th
                >
                <td>{size(item.size)}</td><td>{item.schema_version}</td>
                <td
                  ><div class="row-actions">
                    <a
                      class="backup-download"
                      href={api.backupDownloadUrl(item.name)}
                      download={item.name}>{copy.download}</a
                    >
                    <button
                      type="button"
                      disabled={creating}
                      onclick={() => {
                        selected = item;
                      }}>{copy.restore}</button
                    >
                  </div></td
                >
              </tr>
            {/if}
          {:else}<tr><td colspan="5">{loading ? text.access.loading : copy.empty}</td></tr>{/each}
        </tbody>
      </table>
    </section>
    <form
      class="access-panel editor"
      onsubmit={create}
      aria-label={copy.create}
      aria-busy={creating}
    >
      <h2>{copy.create}</h2>
      <p class="backup-warning">{copy.passwordHint}</p>
      <label>{copy.label}<input bind:value={label} disabled={creating} /></label>
      <label
        >{copy.password}<input
          type="password"
          required
          minlength="12"
          autocomplete="new-password"
          bind:value={password}
          disabled={creating}
        /></label
      >
      <label
        >{copy.repeat}<input
          type="password"
          required
          minlength="12"
          autocomplete="new-password"
          bind:value={repeat}
          disabled={creating}
          aria-invalid={!!formProblem}
        /></label
      >
      {#if formProblem}<p class="form-error" role="alert">{formProblem}</p>{/if}
      {#if notice}<p role="status">{notice}</p>{/if}
      {#if creating}<BackupProgress label={copy.creating} />{/if}
      <button class="primary" type="submit" disabled={creating}>{copy.create}</button>
    </form>
  </div>
  {#if selected}<RestoreDialog
      backup={selected}
      {onRestored}
      onCancel={() => {
        selected = null;
      }}
    />{/if}
</section>
