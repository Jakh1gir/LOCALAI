<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './api/client.ts';
  import type { Me } from './api/types.ts';
  import { text } from './text.ts';

  let { setupRequired = false, notice = '', onSignedIn }: {
    setupRequired?: boolean;
    notice?: string;
    onSignedIn: (me: Me) => void;
  } = $props();

  let login = $state('');
  let password = $state('');
  let busy = $state(false);
  let problem = $state('');
  let loginInput: HTMLInputElement;

  onMount(() => loginInput.focus());

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    problem = '';
    try {
      const me = await api.login(login.trim(), password);
      password = '';
      onSignedIn(me);
    } catch (error) {
      // Сервер отвечает по-русски: неверный пароль, блокировка, ожидание.
      problem = error instanceof Error ? error.message : text.networkError;
    } finally {
      busy = false;
    }
  }
</script>

<main class="auth" aria-labelledby="auth-title">
  <form class="auth-card" onsubmit={submit}>
    <h1 id="auth-title">{text.loginTitle}</h1>
    {#if notice}
      <p class="auth-notice" role="status">{notice}</p>
    {/if}
    {#if setupRequired}
      <p class="auth-notice">{text.setupRequired}</p>
      <code class="auth-command">{text.setupCommand}</code>
    {/if}
    <label class="auth-field">
      <span>{text.loginField}</span>
      <input bind:this={loginInput} name="username" autocomplete="username" required bind:value={login} />
    </label>
    <label class="auth-field">
      <span>{text.passwordField}</span>
      <input name="password" type="password" autocomplete="current-password" required bind:value={password} />
    </label>
    {#if problem}
      <p class="auth-error" role="alert">{problem}</p>
    {/if}
    <button class="auth-submit" type="submit" disabled={busy}>{busy ? text.signingIn : text.signIn}</button>
  </form>
</main>
