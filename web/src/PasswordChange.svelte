<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './api/client.ts';
  import type { Me } from './api/types.ts';
  import { newPasswordProblem } from './session.ts';
  import { text } from './text.ts';

  let { me, onChanged }: { me: Me; onChanged: (me: Me) => void } = $props();

  let current = $state('');
  let fresh = $state('');
  let repeat = $state('');
  let busy = $state(false);
  let problem = $state('');
  let currentInput: HTMLInputElement;

  onMount(() => currentInput.focus());

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    problem = newPasswordProblem(fresh, repeat, me.user.login);
    if (problem) return;
    busy = true;
    try {
      const updated = await api.changePassword(current, fresh);
      current = fresh = repeat = '';
      onChanged(updated);
    } catch (error) {
      problem = error instanceof Error ? error.message : text.networkError;
    } finally {
      busy = false;
    }
  }
</script>

<main class="auth" aria-labelledby="auth-title">
  <form class="auth-card" onsubmit={submit}>
    <h1 id="auth-title">{text.changeTitle}</h1>
    <p class="auth-notice">{text.changeHint}</p>
    <input type="text" name="username" autocomplete="username" value={me.user.login} hidden readonly />
    <label class="auth-field">
      <span>{text.currentPassword}</span>
      <input bind:this={currentInput} type="password" autocomplete="current-password" required bind:value={current} />
    </label>
    <label class="auth-field">
      <span>{text.newPassword}</span>
      <input type="password" autocomplete="new-password" required minlength="12" bind:value={fresh} />
    </label>
    <label class="auth-field">
      <span>{text.repeatPassword}</span>
      <input type="password" autocomplete="new-password" required minlength="12" bind:value={repeat} />
    </label>
    {#if problem}
      <p class="auth-error" role="alert">{problem}</p>
    {/if}
    <button class="auth-submit" type="submit" disabled={busy}>{busy ? text.savingPassword : text.savePassword}</button>
  </form>
</main>
