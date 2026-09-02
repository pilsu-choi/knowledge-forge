import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useLayoutData } from "./ProjectLayout";
import { useFile } from "@/hooks/useOkf";
import { FrontmatterPanel } from "@/components/FrontmatterPanel";
import { MarkdownBody } from "@/components/MarkdownBody";
import { StateMessage } from "@/components/StateMessage";
import { usePathSegments } from "@/lib/pathSegments";

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

  if (isLoading) return <StateMessage>불러오는 중…</StateMessage>;
  if (error) return <StateMessage>불러오기 실패: {(error as Error).message}</StateMessage>;
  if (!data) return null;

  const title = (data.frontmatter.title as string) || (data.frontmatter.type as string) || relPath;

  return (
    <div className="mx-auto max-w-[760px]">
      <button
        type="button"
        onClick={() => navigate(-1)}
        className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        뒤로
      </button>
      <h1 className="mb-4 text-2xl font-semibold tracking-tight">{title}</h1>
      <FrontmatterPanel frontmatter={data.frontmatter} />
      <MarkdownBody project={project} html={data.body_html} />
    </div>
  );
}
