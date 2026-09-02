import { useLayoutData } from "./ProjectLayout";
import { useGraph } from "@/hooks/useOkf";
import { GraphCanvas } from "@/components/GraphCanvas";
import { StateMessage } from "@/components/StateMessage";
import { usePathSegments } from "@/lib/pathSegments";

export function GraphPage() {
  const { project } = useLayoutData();
  const { data, isLoading, error } = useGraph(project);
  usePathSegments([{ label: project, to: `/${encodeURIComponent(project)}` }, { label: "graph" }]);

  if (isLoading) return <StateMessage>불러오는 중…</StateMessage>;
  if (error) return <StateMessage>불러오기 실패: {(error as Error).message}</StateMessage>;
  if (!data) return null;

  return <GraphCanvas project={project} data={data} />;
}
