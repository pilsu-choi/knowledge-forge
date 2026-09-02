import { Outlet, useOutletContext, useParams } from "react-router-dom";
import { useManifest } from "../hooks/useOkf";
import { TopBar } from "../components/TopBar";
import { Rail } from "../components/Rail";
import type { Manifest } from "../lib/api";

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
    <div className="h-screen flex flex-col overflow-hidden">
      <TopBar project={project as string} manifest={manifest} />
      <div className="flex-1 flex min-h-0">
        {manifest && <Rail project={project as string} manifest={manifest} />}
        <main className="main">
          {isLoading && <div className="empty">불러오는 중…</div>}
          {error && <div className="empty">불러오기 실패: {(error as Error).message}</div>}
          {manifest && <Outlet context={{ project: project as string, manifest } satisfies LayoutContext} />}
        </main>
      </div>
    </div>
  );
}
