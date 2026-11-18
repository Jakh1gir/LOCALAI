<script lang="ts">
  import { onMount } from 'svelte';
  import { text } from '../text.ts';

  let {
    title,
    consequence,
    action,
    onConfirm,
    onCancel,
  }: {
    title: string;
    consequence: string;
    action: string;
    onConfirm: () => void;
    onCancel: () => void;
  } = $props();
  let dialog = $state<HTMLDialogElement>();
  let cancelButton = $state<HTMLButtonElement>();
  let answered = false;

  onMount(() => {
    const modal = dialog;
    const opener = document.activeElement;
    modal?.showModal();
    cancelButton?.focus();
    return () => {
      modal?.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  });

  function answer(confirmed: boolean) {
    if (answered) return;
    answered = true;
    if (confirmed) onConfirm();
    else onCancel();
  }
</script>

<dialog
  class="access confirm-dialog"
  bind:this={dialog}
  aria-labelledby="confirm-title"
  aria-describedby="confirm-consequence"
  oncancel={(event) => {
    event.preventDefault();
    answer(false);
  }}
  onclose={() => answer(false)}
>
  <h2 id="confirm-title">{title}</h2>
  <p id="confirm-consequence" class="confirm-consequence">{consequence}</p>
  <form
    class="form-actions"
    onsubmit={(event) => {
      event.preventDefault();
      answer(true);
    }}
  >
    <button bind:this={cancelButton} type="button" onclick={() => answer(false)}
      >{text.access.cancel}</button
    >
    <button class="primary" type="submit">{action}</button>
  </form>
</dialog>
