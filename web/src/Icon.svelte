<script lang="ts">
  const paths = {
    logo: 'M5 4v15h14M9 14h4v-4h4V6',
    registry: 'M3 5h18v14H3zM3 10h18M9 10v9M15 10v9',
    kks: 'M7 7h10M7 7v10M17 7v10M7 17h10M9 7a2 2 0 1 0-4 0 2 2 0 0 0 4 0M19 7a2 2 0 1 0-4 0 2 2 0 0 0 4 0M9 17a2 2 0 1 0-4 0 2 2 0 0 0 4 0M19 17a2 2 0 1 0-4 0 2 2 0 0 0 4 0',
    documents: 'M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6',
    imports: 'M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4',
    tools: 'm4 19 7-7m-4 3 3 3m2-7a5 5 0 0 1 6-6l-3 3 1 2 2 1 3-3a5 5 0 0 1-6 6l-7 7',
    checks: 'M4 6h16v14H4zM4 10h16M8 4v4m8-4v4m-7 7 2 2 4-4',
    knowledge: 'M3 5h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v14h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3zM12 9v12',
    chat: 'M3 4h14v10H9l-4 3v-3H3zM10 17h5l4 3v-3h2V8',
    assistant: 'M5 6h14v10h-8l-4 4v-4H5zM9 10h6m-6 3h4',
    admin: 'M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-6 2v4',
    search: 'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0m-2 4 5 5',
    close: 'm6 6 12 12M18 6 6 18',
    tasks: 'M4 6h16v14H4zM8 4v4m8-4v4M8 13h8m-8 3h5',
    profile: 'M15 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0M5 20v-2a7 7 0 0 1 14 0v2',
    missing: 'M6 3h8l4 4v14H6zM14 3v5h4m-7 4 2 2m0-2-2 2',
  } as const;

  let { name, size = 20 }: { name: keyof typeof paths; size?: number } = $props();
</script>

<svg
  aria-hidden="true"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.55"
  stroke-linecap="round"
  stroke-linejoin="round"
>
  <path d={paths[name]} />
</svg>
