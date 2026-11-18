<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../api/client.ts';
  import type {
    Dataset,
    NamedEntity,
    Permission,
    PermissionCatalog,
    RecordType,
    RoleDetails,
    TypeField,
  } from '../api/types.ts';
  import { text } from '../text.ts';
  import { permissionChanges, permissionError, permissionKey, problemText } from './model.ts';

  let {
    role,
    onSaved,
    onDirty,
    onBusy,
  }: {
    role: RoleDetails;
    onSaved: (role: RoleDetails) => Promise<void>;
    onDirty: (dirty: boolean) => void;
    onBusy: (busy: boolean) => void;
  } = $props();
  const copy = text.access;
  let before = $state<Permission[]>([]);
  let draft = $state<Permission[]>([]);
  let catalog = $state<PermissionCatalog>();
  let datasets = $state<Dataset[]>([]);
  let sites = $state<NamedEntity[]>([]);
  let types = $state<RecordType[]>([]);
  let typeId = $state('');
  let fields = $state<TypeField[]>([]);
  let fieldNames = $state<Record<string, string>>({});
  let extraDatasets = $state<string[]>([]);
  let addId = $state('');
  let busy = $state(false);
  let loadingFields = $state(false);
  let problem = $state('');
  let fieldProblem = $state('');
  let errorItem = $state<Permission>();
  let notice = $state('');
  const controller = new AbortController();
  let fieldRequest: AbortController | undefined;
  let changes = $derived(permissionChanges(before, draft));
  let dirty = $derived(changes.added.length + changes.removed.length > 0);
  let targets = $derived([
    ...new Set([
      ...before
        .filter((item) => item.scope === 'dataset' && item.target !== '*')
        .map((item) => item.target),
      ...draft
        .filter((item) => item.scope === 'dataset' && item.target !== '*')
        .map((item) => item.target),
      ...extraDatasets,
    ]),
  ]);
  let otherFields = $derived(
    [
      ...new Set(
        [...before, ...draft].filter((item) => item.scope === 'field').map((item) => item.target),
      ),
    ].filter((id) => !fields.some((field) => field.id === id)),
  );

  $effect(() => onDirty(dirty));

  onMount(() => {
    before = structuredClone($state.snapshot(role.permissions));
    draft = structuredClone($state.snapshot(before));
    void load();
    return () => {
      controller.abort();
      fieldRequest?.abort();
      onDirty(false);
    };
  });

  async function load() {
    problem = '';
    try {
      const [nextCatalog, nextDatasets, nextTypes, nextSites] = await Promise.all([
        api.permissionCatalog(controller.signal),
        api.listDatasets(undefined, controller.signal),
        api.listTypes(controller.signal),
        api.listSites(controller.signal),
      ]);
      catalog = nextCatalog;
      datasets = nextDatasets.items;
      types = nextTypes.items;
      sites = nextSites.items;
    } catch (error) {
      if (!controller.signal.aborted) problem = problemText(error);
    }
  }

  async function chooseType(id: string) {
    fieldRequest?.abort();
    typeId = id;
    fields = [];
    fieldProblem = '';
    loadingFields = false;
    if (!id) return;
    const request = new AbortController();
    fieldRequest = request;
    loadingFields = true;
    try {
      const details = await api.getType(id, request.signal);
      if (request.signal.aborted) return;
      fields = details.fields;
      fieldNames = {
        ...fieldNames,
        ...Object.fromEntries(fields.map((field) => [field.id, `${details.name} · ${field.name}`])),
      };
    } catch (error) {
      if (!request.signal.aborted) fieldProblem = problemText(error);
    } finally {
      if (!request.signal.aborted) loadingFields = false;
    }
  }

  function has(scope: Permission['scope'], target: string, action: string) {
    return draft.some(
      (item) => item.scope === scope && item.target === target && item.action === action,
    );
  }

  function toggle(scope: Permission['scope'], target: string, action: string, value: boolean) {
    notice = '';
    errorItem = undefined;
    problem = '';
    draft = draft.filter(
      (item) => !(item.scope === scope && item.target === target && item.action === action),
    );
    if (value) draft = [...draft, { scope, target, action }];
  }

  function setField(target: string, action: string) {
    draft = draft.filter((item) => !(item.scope === 'field' && item.target === target));
    if (action) draft = [...draft, { scope: 'field', target, action }];
    errorItem = undefined;
    problem = '';
    notice = '';
  }

  function datasetName(id: string) {
    const dataset = datasets.find((item) => item.id === id);
    if (!dataset) return `${copy.unavailableDataset} ${id}`;
    const site = sites.find((item) => item.id === dataset.site_id);
    return site ? `${dataset.name} · ${site.name}` : dataset.name;
  }

  function describe(item: Permission) {
    switch (item.scope) {
      case 'section':
        return catalog?.sections[item.target] ?? item.target;
      case 'dataset':
        return `${item.target === '*' ? copy.allDatasets : datasetName(item.target)} · ${catalog?.dataset_actions[item.action] ?? item.action}`;
      case 'field':
        return `${fieldNames[item.target] ?? `${copy.unavailableField} ${item.target}`} · ${catalog?.field_limits[item.action] ?? item.action}`;
      case 'doc_type':
        return `${copy.allDocuments} · ${catalog?.doc_type_actions[item.action] ?? item.action}`;
    }
  }

  function rowProblem(scope: Permission['scope'], target: string) {
    return errorItem?.scope === scope && errorItem.target === target ? problem : '';
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !dirty) return;
    busy = true;
    onBusy(true);
    problem = '';
    errorItem = undefined;
    notice = '';
    const sent = structuredClone($state.snapshot(draft));
    try {
      const saved = await api.setPermissions(role.id, sent);
      before = structuredClone(saved.permissions);
      draft = structuredClone(saved.permissions);
      notice = copy.saved;
      await onSaved(saved);
    } catch (error) {
      problem = problemText(error);
      errorItem = permissionError(error, sent);
    } finally {
      busy = false;
      onBusy(false);
    }
  }
</script>

<section class="access-panel matrix-panel" aria-label={`${copy.permissions} · ${role.name}`}>
  <div class="access-toolbar"><h3>{copy.permissions} · {role.name}</h3></div>
  {#if !catalog}
    {#if problem}<p class="form-error" role="alert">{problem}</p>
      <button type="button" onclick={load}>{text.retry}</button>
    {:else}<p role="status">{copy.loading}</p>{/if}
  {:else}
    <form onsubmit={save}>
      <fieldset disabled={busy}>
        <legend>{copy.sections}</legend>
        <div class="section-options">
          {#each Object.entries(catalog.sections) as [key, label]}
            <div class="permission-line">
              <label class="check-label"
                ><input
                  type="checkbox"
                  checked={has('section', key, 'use')}
                  onchange={(event) => toggle('section', key, 'use', event.currentTarget.checked)}
                />{label}</label
              >
              {#if rowProblem('section', key)}<p class="form-error" role="alert">{problem}</p>{/if}
            </div>
          {/each}
        </div>
      </fieldset>
      <fieldset disabled={busy}>
        <legend>{copy.datasets}</legend>
        <p class="muted">{copy.wildcardHint}</p>
        <div class="table-scroll">
          <table class="access-table permission-table" aria-label={copy.datasets}>
            <thead
              ><tr
                ><th>{copy.datasets}</th
                >{#each Object.entries(catalog.dataset_actions) as [, label]}<th>{label}</th
                  >{/each}</tr
              ></thead
            >
            <tbody
              >{#each ['*', ...targets] as target (target)}
                <tr
                  ><th scope="row"
                    >{target === '*' ? copy.allDatasets : datasetName(target)}
                    {#if rowProblem('dataset', target)}<p class="form-error" role="alert">
                        {problem}
                      </p>{/if}</th
                  >
                  {#each Object.entries(catalog.dataset_actions) as [action, label]}
                    {@const inherited = target !== '*' && has('dataset', '*', action)}
                    <td
                      ><input
                        type="checkbox"
                        aria-label={`${target === '*' ? copy.allDatasets : datasetName(target)} · ${label}`}
                        checked={inherited || has('dataset', target, action)}
                        disabled={inherited}
                        title={inherited ? copy.inherited : undefined}
                        onchange={(event) =>
                          toggle('dataset', target, action, event.currentTarget.checked)}
                      /></td
                    >
                  {/each}
                </tr>
              {/each}</tbody
            >
          </table>
        </div>
        <div class="add-dataset">
          <label
            >{copy.addDataset}<select aria-label={copy.addDataset} bind:value={addId}
              ><option value="">{copy.chooseDataset}</option>
              {#each datasets.filter((item) => !targets.includes(item.id)) as item}<option
                  value={item.id}>{datasetName(item.id)}</option
                >{/each}
            </select></label
          ><button
            type="button"
            disabled={!addId}
            onclick={() => {
              extraDatasets = [...extraDatasets, addId];
              addId = '';
            }}>{copy.addDataset}</button
          >
        </div>
      </fieldset>
      <fieldset disabled={busy}>
        <legend>{copy.fields}</legend>
        <label class="type-picker"
          >{copy.recordType}<select
            aria-label={copy.recordType}
            value={typeId}
            onchange={(event) => chooseType(event.currentTarget.value)}
            ><option value="">{copy.chooseType}</option>
            {#each types as type}<option value={type.id}>{type.name}</option>{/each}
          </select></label
        >
        {#if fieldProblem}<p class="form-error" role="alert">{fieldProblem}</p>
          <button type="button" onclick={() => chooseType(typeId)}>{text.retry}</button>{/if}
        {#if loadingFields}<p role="status">{copy.loading}</p>{/if}
        {#if typeId && !loadingFields && !fieldProblem}
          <div class="table-scroll">
            <table class="access-table" aria-label={copy.fields}>
              <thead><tr><th>{copy.field}</th><th>{copy.permissions}</th></tr></thead><tbody>
                {#each fields as field (field.id)}<tr
                    ><th scope="row"
                      >{field.name}<span class="cell-description mono">{field.key}</span></th
                    ><td>
                      <select
                        aria-label={`${copy.permissions} · ${field.name}`}
                        value={has('field', field.id, 'hidden')
                          ? 'hidden'
                          : has('field', field.id, 'readonly')
                            ? 'readonly'
                            : ''}
                        onchange={(event) => setField(field.id, event.currentTarget.value)}
                      >
                        <option value="">{copy.noLimit}</option
                        >{#each Object.entries(catalog.field_limits) as [action, label]}<option
                            value={action}>{label}</option
                          >{/each}
                      </select>{#if rowProblem('field', field.id)}<p
                          class="form-error"
                          role="alert"
                        >
                          {problem}
                        </p>{/if}
                    </td></tr
                  >{:else}<tr><td colspan="2">{copy.noFields}</td></tr>{/each}
              </tbody>
            </table>
          </div>
        {/if}
        {#if otherFields.length}
          <h4>{copy.otherFieldLimits}</h4>
          <div class="table-scroll">
            <table class="access-table" aria-label={copy.otherFieldLimits}>
              <thead><tr><th>{copy.field}</th><th>{copy.permissions}</th></tr></thead><tbody>
                {#each otherFields as id (id)}
                  {@const label = fieldNames[id] ?? `${copy.unavailableField} ${id}`}
                  <tr
                    ><th scope="row">{label}</th><td
                      ><select
                        aria-label={`${copy.permissions} · ${label}`}
                        value={has('field', id, 'hidden')
                          ? 'hidden'
                          : has('field', id, 'readonly')
                            ? 'readonly'
                            : ''}
                        onchange={(event) => setField(id, event.currentTarget.value)}
                      >
                        <option value="">{copy.noLimit}</option
                        >{#each Object.entries(catalog.field_limits) as [action, caption]}<option
                            value={action}>{caption}</option
                          >{/each}
                      </select>{#if rowProblem('field', id)}<p class="form-error" role="alert">
                          {problem}
                        </p>{/if}</td
                    ></tr
                  >
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </fieldset>
      <fieldset disabled={busy}>
        <legend>{copy.documents}</legend>
        {#each Object.entries(catalog.doc_type_actions) as [action, label]}<label
            class="check-label"
            ><input
              type="checkbox"
              checked={has('doc_type', '*', action)}
              onchange={(event) => toggle('doc_type', '*', action, event.currentTarget.checked)}
            />{copy.allDocuments} · {label}</label
          >{/each}
        {#if rowProblem('doc_type', '*')}<p class="form-error" role="alert">{problem}</p>{/if}
      </fieldset>
      <div class="permission-review" aria-live="polite">
        {#if dirty}
          {#if changes.added.length}<div>
              <h4>{copy.added}</h4>
              <ul>
                {#each changes.added as item (permissionKey(item))}<li>{describe(item)}</li>{/each}
              </ul>
            </div>{/if}
          {#if changes.removed.length}<div>
              <h4>{copy.removed}</h4>
              <ul>
                {#each changes.removed as item (permissionKey(item))}<li>
                    {describe(item)}
                  </li>{/each}
              </ul>
            </div>{/if}
        {:else}<p class="muted">{copy.noChanges}</p>{/if}
      </div>
      {#if problem && !errorItem}<p class="form-error" role="alert">{problem}</p>{/if}
      {#if notice}<p role="status">{notice}</p>{/if}
      <div class="form-actions">
        <button class="primary" type="submit" disabled={busy || !dirty}
          >{copy.savePermissions}</button
        >
        <button
          type="button"
          disabled={busy || !dirty}
          onclick={() => {
            draft = structuredClone($state.snapshot(before));
            problem = '';
            notice = '';
            errorItem = undefined;
          }}>{copy.discard}</button
        >
      </div>
    </form>
  {/if}
</section>
