import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useLayoutData } from "./ProjectLayout";
import { typeCounts } from "@/lib/api";
import { CardGrid, type CardItem } from "@/components/CardGrid";
import { usePathSegments } from "@/lib/pathSegments";

export function OverviewPage() {
  const { project, manifest } = useLayoutData();
  const navigate = useNavigate();
  usePathSegments([{ label: project }]);

  const items: CardItem[] = useMemo(() => {
    const counts = typeCounts(manifest);
    return Object.keys(counts)
      .sort()
      .map((type) => ({ path: type, type, title: type, desc: `${counts[type]}개` }));
  }, [manifest]);

  return (
    <CardGrid
      items={items}
      emptyMessage="오브젝트가 없습니다."
      onSelect={(item) => navigate(`/${encodeURIComponent(project)}/type/${encodeURIComponent(item.path)}`)}
    />
  );
}
