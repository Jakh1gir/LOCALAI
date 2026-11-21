<script lang="ts">
  import { api, ApiError } from '../api/client.ts';
  import { problemText } from '../admin/model.ts';
  import ConfirmAction from '../admin/ConfirmAction.svelte';
  import { kksUrl } from '../search/target.ts';
  import { observe, type Kks, type Term } from './model.ts';
  import KksPicker from './KksPicker.svelte';
  let { manage, initial }: { manage: boolean; initial: string } = $props();
  let filter = $state(''),
    terms = $state<Term[]>([]),
    edit = $state<string | null>(null);
  let name = $state(''),
    meaning = $state(''),
    objects = $state<Kks[]>([]);
  let removing = $state<Term | null>(null),
    error = $state(''),
    nameError = $state(''),
    loadError = $state(''),
    busy = $state(false),
    rev = $state(0);
  $effect(() => {
    filter = initial;
  });
  $effect(() => {
    const q = filter;
    void rev;
    return observe(
      'glossary_term',
      (s) => api.knowledge.terms(q, s),
      (data) => {
        terms = data.items;
        loadError = '';
      },
      (msg) => (loadError = msg),
    );
  });
  function start(item?: Term) {
    edit = item?.id ?? '';
    name = item?.term ?? '';
    meaning = item?.meaning ?? '';
    objects = item?.kks ?? [];
    error = '';
    nameError = '';
  }
  async function save() {
    if (busy || !manage) return;
    busy = true;
    error = '';
    nameError = '';
    try {
      await api.knowledge.saveTerm(edit || undefined, {
        term: name.trim(),
        meaning: meaning.trim(),
        kks_ids: objects.map((k) => k.id),
      });
      edit = null;
      rev++;
    } catch (e) {
      if (e instanceof ApiError && e.code === 'term_taken') nameError = e.message;
      else error = problemText(e);
    } finally {
      busy = false;
    }
  }
  async function remove() {
    const target = removing;
    removing = null;
    if (!target || busy || !manage) return;
    busy = true;
    error = '';
    try {
      await api.knowledge.deleteTerm(target.id);
      rev++;
    } catch (e) {
      error = problemText(e);
    } finally {
      busy = false;
    }
  }
</script>

<h2>Словарь станции</h2>
<div class="knowledge-toolbar">
  <label>Найти термин<input type="search" bind:value={filter} /></label>
  {#if manage}<button onclick={() => start()}>Добавить термин</button>{/if}
</div>
{#if loadError}<p role="alert">{loadError}</p>{/if}
{#if manage && edit !== null}
  <form
    class="access-panel editor"
    onsubmit={(e) => {
      e.preventDefault();
      void save();
    }}
  >
    <label
      >Термин<input
        required
        bind:value={name}
        aria-invalid={!!nameError}
        aria-describedby={nameError ? 'term-error' : undefined}
        oninput={() => (nameError = '')}
      /></label
    >
    {#if nameError}<p id="term-error" class="form-error" role="alert">{nameError}</p>{/if}
    <label>Значение<textarea required rows="2" bind:value={meaning}></textarea></label>
    <KksPicker
      label="Найти ККС для термина"
      select={(item) => {
        if (!objects.some((k) => k.id === item.id)) objects = [...objects, item];
      }}
    />
    {#each objects as item}<div>
        {item.code}
        <button type="button" onclick={() => (objects = objects.filter((k) => k.id !== item.id))}
          >Убрать ККС</button
        >
      </div>{/each}
    <div class="form-actions">
      <button class="primary" disabled={busy || !name.trim() || !meaning.trim()}
        >Сохранить термин</button
      ><button type="button" disabled={busy} onclick={() => (edit = null)}>Отмена</button>
    </div>
  </form>{/if}
{#if error}<p class="form-error" role="alert">{error}</p>{/if}
<ul class="knowledge-list">
  {#each terms as item (item.id)}<li class="access-panel editor">
      <h3>{item.term}</h3>
      <p>{item.meaning}</p>
      <div>
        {#each item.kks as k}<a href={kksUrl(k.id)}>{k.code}</a>
        {/each}
      </div>
      {#if manage}<div class="row-actions">
          <button disabled={busy} onclick={() => start(item)}>Изменить термин</button><button
            disabled={busy}
            onclick={() => {
              removing = item;
            }}>Удалить термин</button
          >
        </div>{/if}
    </li>
  {:else}<li>Термины не найдены</li>{/each}
</ul>
{#if removing}<ConfirmAction
    title={'Удалить термин «' + removing.term + '»?'}
    consequence="Термин исчезнет из словаря станции и поиска."
    action="Удалить"
    onCancel={() => (removing = null)}
    onConfirm={remove}
  />{/if}
