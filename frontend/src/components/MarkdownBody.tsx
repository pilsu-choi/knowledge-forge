import { useNavigate } from "react-router-dom";

// body_html comes pre-rendered from the server (okf/reader.py:read_markdown_file
// via markdown-it) with internal `.md` links already normalized to
// project-root-relative hrefs - this only needs to intercept clicks on those
// so they navigate client-side instead of hitting the static file mount.
export function MarkdownBody({ project, html }: { project: string; html: string }) {
  const navigate = useNavigate();

  function onClick(e: React.MouseEvent<HTMLDivElement>) {
    const a = (e.target as HTMLElement).closest("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (!href || /^[a-z]+:\/\//i.test(href) || href.startsWith("#")) return;
    if (!href.endsWith(".md")) return;
    e.preventDefault();
    navigate(`/${encodeURIComponent(project)}/f/${decodeURIComponent(href)}`);
  }

  return <div className="body-html" onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}
