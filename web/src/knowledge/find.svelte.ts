import { api } from '../api/client.ts';
import { kksUrl } from '../search/target.ts';
import { knowledgeUrl, observe, type Results } from './model.ts';

export function knowledgeSearch(query: () => string) {
  let data = $state<Results | null>(null),
    error = $state('');
  $effect(() => {
    const q = query().trim();
    data = null;
    error = '';
    if (q)
      return observe(
        'knowledge_note',
        (s) => api.knowledge.search(q, s),
        (d) => (data = d),
        (msg) => (error = msg),
        160,
      );
  });
  const groups = $derived(knowledgeGroups(data));
  return {
    get groups() {
      return groups;
    },
    get error() {
      return error;
    },
  };
}

export function knowledgeGroups(data: Results | null) {
  return [
    {
      title: 'Заметки',
      items: (data?.notes ?? []).map((n) => ({
        href: knowledgeUrl({ note: n.id }),
        title: n.title,
        detail: n.snippet,
      })),
    },
    {
      title: 'Словарь',
      items: (data?.glossary ?? []).map((t) => ({
        href: knowledgeUrl({ tab: 'glossary', q: t.term }),
        title: t.term,
        detail: t.meaning,
      })),
    },
    {
      title: 'Объекты ККС',
      items: (data?.kks ?? [])
        .filter((k) => k.via === 'glossary')
        .map((k) => ({ href: kksUrl(k.id), title: k.code, detail: 'По словарю: ' + k.term })),
    },
  ].filter((g) => g.items.length);
}
