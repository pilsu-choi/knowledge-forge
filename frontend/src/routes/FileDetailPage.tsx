import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useLayoutData } from "./ProjectLayout";
import { useFile } from "../hooks/useOkf";
import { FrontmatterPanel } from "../components/FrontmatterPanel";
import { MarkdownBody } from "../components/MarkdownBody";
import { usePathSegments } from "../lib/pathSegments";

export function FileDetailPage() {
  const { project } = useLayoutData();
  const params = useParams<{ "*": string }>();
  const relPath = params["*"] ?? "";
  const navigate = useNavigate();
  const { data, isLoading, error } = useFile(project, relPath);

  const segments = useMemo(
    () => [
      { label: project, to: `/${encodeURIComponent(project)}` },
      ...relPath.split("/").map((seg) => ({ label: seg })),
    ],
    [project, relPath],
  );
  usePathSegments(segments);

  if (isLoading) return <div className="empty">불러오는 중…</div>;
  if (error) return <div className="empty">불러오기 실패: {(error as Error).message}</div>;
  if (!data) return null;

  const title = (data.frontmatter.title as string) || (data.frontmatter.type as string) || relPath;

  return (
    <div>
      <a className="detail-link mb-3.5 inline-block" onClick={() => navigate(-1)}>
        ← 뒤로
      </a>
      <div className="detail-head">
        <h1>{title}</h1>
      </div>
      <div className="detail-grid">
        <FrontmatterPanel frontmatter={data.frontmatter} sticky />
        <MarkdownBody project={project} html={data.body_html} />
      </div>
    </div>
  );
}
