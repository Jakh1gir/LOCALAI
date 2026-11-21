export function kksUrl(id: string) {
  return `/kks?${new URLSearchParams({ object: id })}`;
}

export function recordUrl(dataset: string, record: string) {
  return `/registries?${new URLSearchParams({ dataset, record })}`;
}

export type SearchTarget =
  | { kind: 'kks'; id: string }
  | { kind: 'record'; id: string; dataset: string };
export function searchTarget(path: string, query: string): SearchTarget | null {
  const params = new URLSearchParams(query);
  if (path === '/kks' && params.get('object')) return { kind: 'kks', id: params.get('object')! };
  if (path === '/registries' && params.get('record') && params.get('dataset'))
    return {
      kind: 'record',
      id: params.get('record')!,
      dataset: params.get('dataset')!,
    };
  return null;
}
