import type { SearchResult } from '../api/types.ts';
import type { sections } from '../routes.ts';
import { text } from '../text.ts';

export type Section = (typeof sections)[number];
export type ResultOption = {
  key: string;
  href: string;
  title: string;
  detail: string;
  badge?: string;
};
export type ResultGroup = { title: string; items: ResultOption[] };

export function kksUrl(id: string) {
  return `/kks?${new URLSearchParams({ object: id })}`;
}

export function recordUrl(dataset: string, record: string) {
  return `/registries?${new URLSearchParams({ dataset, record })}`;
}

export function resultGroups(
  result: SearchResult | null,
  sections: readonly Section[],
  query: string,
): ResultGroup[] {
  return [
    {
      title: text.find.kks,
      items: [...(result?.kks ?? [])]
        .sort((a, b) => Number(b.exact) - Number(a.exact))
        .map((item) => ({
          key: `kks-${item.id}`,
          href: kksUrl(item.id),
          title: item.code,
          detail: [item.site, item.name, item.archived ? text.find.archived : '']
            .filter(Boolean)
            .join(' · '),
          badge:
            item.via === 'signal'
              ? text.find.signal(item.signal ?? '')
              : item.via === 'alias'
                ? text.find.alias
                : item.via === 'glossary'
                  ? text.find.glossary
                  : item.exact
                    ? text.find.exact
                    : undefined,
        })),
    },
    {
      title: text.find.records,
      items: (result?.records ?? []).map((item) => ({
        key: `record-${item.id}`,
        href: recordUrl(item.dataset_id, item.id),
        title: item.label,
        detail: [item.dataset, item.match ? `${item.match.field}: ${item.match.text}` : '']
          .filter(Boolean)
          .join(' · '),
      })),
    },
    {
      title: text.find.commands,
      items: sections
        .filter((item) =>
          item.title.toLocaleLowerCase('ru').includes(query.trim().toLocaleLowerCase('ru')),
        )
        .map((item) => ({
          key: `section-${item.path.slice(1)}`,
          href: item.path,
          title: item.title,
          detail: '',
        })),
    },
  ];
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
