import { Outlet, useOutletContext, useParams } from "react-router-dom";
import { useManifest } from "@/hooks/useOkf";
import { TopBar } from "@/components/TopBar";
import { Rail } from "@/components/Rail";
import { StateMessage } from "@/components/StateMessage";
import type { Manifest } from "@/lib/api";

interface LayoutContext {
  project: string;
  manifest: Manifest;
}

export function useLayoutData() {
  return useOutletContext<LayoutContext>();
}

export function ProjectLayout() {
  const { project } = useParams<{ project: string }>();
  const { data: manifest, isLoading, error } = useManifest(project);

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background text-foreground">
      <TopBar project={project as string} manifest={manifest} />
      <div className="flex min-h-0 flex-1">
        {manifest && <Rail project={project as string} manifest={manifest} />}
        <main className="flex-1 overflow-y-auto px-8 py-6">
          {isLoading && <StateMessage>불러오는 중…</StateMessage>}
          {error && <StateMessage>불러오기 실패: {(error as Error).message}</StateMessage>}
          {manifest && <Outlet context={{ project: project as string, manifest } satisfies LayoutContext} />}
        </main>
      </div>
    </div>
  );
}
