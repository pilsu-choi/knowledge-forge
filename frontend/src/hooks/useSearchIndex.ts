import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";
import * as api from "../lib/api";
import type { Manifest, ObjectCard } from "../lib/api";

export interface SearchEntry extends ObjectCard {
  type: string;
}

// Cmd+K needs titles/tags the manifest doesn't carry (it's just id+type+path),
// so this fetches every type's card list once the palette is used. OKF trees
// are documented as "hundreds of objects" (wiki/okf.md), so a handful of
// parallel per-type fetches is cheap - no new backend endpoint needed.
export function useSearchIndex(project: string | undefined, manifest: Manifest | undefined) {
  const types = useMemo(
    () => [...new Set((manifest?.objects ?? []).map((o) => o.type))].sort(),
    [manifest],
  );

  const results = useQueries({
    queries: types.map((type) => ({
      queryKey: ["objects", project, type],
      queryFn: () => api.listObjectsByType(project as string, type),
      enabled: !!project,
      staleTime: 60_000,
    })),
  });

  const entries: SearchEntry[] = useMemo(
    () =>
      results.flatMap((r, i) => ((r.data as ObjectCard[] | undefined) ?? []).map((c) => ({ ...c, type: types[i] }))),
    [results, types],
  );

  return { entries, isLoading: results.some((r) => r.isLoading) };
}
