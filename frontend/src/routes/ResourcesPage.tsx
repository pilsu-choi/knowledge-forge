import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useLayoutData } from "./ProjectLayout";
import { CardGrid, type CardItem } from "@/components/CardGrid";
import { usePathSegments } from "@/lib/pathSegments";

export function ResourcesPage() {
  const { project, manifest } = useLayoutData();
  const navigate = useNavigate();
  usePathSegments([{ label: project, to: `/${encodeURIComponent(project)}` }, { label: "resources" }]);

  const items: CardItem[] = useMemo(
    () =>
      manifest.resources.map((r) => ({
        path: r.path,
        title: r.path.split("/").pop()?.replace(/\.md$/, "") ?? r.path,
        desc: r.type,
      })),
    [manifest],
  );

  return (
    <CardGrid
      items={items}
      emptyMessage="리소스가 없습니다."
      onSelect={(item) => navigate(`/${encodeURIComponent(project)}/f/${item.path}`)}
    />
  );
}
