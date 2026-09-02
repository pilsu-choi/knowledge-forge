import { useLayoutData } from "./ProjectLayout";
import { useGraph } from "../hooks/useOkf";
import { GraphCanvas } from "../components/GraphCanvas";
import { usePathSegments } from "../lib/pathSegments";

export function GraphPage() {
  const { project } = useLayoutData();
  const { data, isLoading, error } = useGraph(project);
  usePathSegments([{ label: project, to: `/${encodeURIComponent(project)}` }, { label: "graph" }]);

  if (isLoading) return <div className="empty">불러오는 중…</div>;
  if (error) return <div className="empty">불러오기 실패: {(error as Error).message}</div>;
  if (!data) return null;

  return <GraphCanvas project={project} data={data} />;
}
