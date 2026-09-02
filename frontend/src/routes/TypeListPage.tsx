import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useLayoutData } from "./ProjectLayout";
import { useObjectsByType } from "../hooks/useOkf";
import { CardGrid, type CardItem } from "../components/CardGrid";
import { usePathSegments } from "../lib/pathSegments";

export function TypeListPage() {
  const { project } = useLayoutData();
  const { type } = useParams<{ type: string }>();
  const navigate = useNavigate();
  const { data: cards, isLoading, error } = useObjectsByType(project, type);

  usePathSegments([{ label: project, to: `/${encodeURIComponent(project)}` }, { label: `objects/${type}` }]);

  const items: CardItem[] = useMemo(
    () =>
      (cards ?? []).map((c) => ({
        path: c.path,
        title: c.title || c.path,
        desc: c.description,
        tags: c.tags,
        type,
      })),
    [cards, type],
  );

  if (isLoading) return <div className="empty">불러오는 중…</div>;
  if (error) return <div className="empty">불러오기 실패: {(error as Error).message}</div>;

  return (
    <CardGrid
      items={items}
      searchable
      emptyMessage="이 타입의 오브젝트가 없습니다."
      onSelect={(item) => navigate(`/${encodeURIComponent(project)}/f/${item.path}`)}
    />
  );
}
