import { useNavigate } from "react-router-dom";
import { PathBar } from "@/components/PathBar";
import { ThemeToggle } from "@/components/ThemeToggle";
import { CommandPalette } from "@/components/CommandPalette";
import { useSegments } from "@/lib/pathSegments";
import { useProjects } from "@/hooks/useOkf";
import type { Manifest } from "@/lib/api";

export function TopBar({ project, manifest }: { project: string; manifest?: Manifest }) {
  const navigate = useNavigate();
  const segments = useSegments();
  const { data: projects } = useProjects();

  return (
    <div className="flex flex-none items-center gap-4 border-b border-border bg-sidebar px-5 py-2.5">
      <a
        className="flex flex-none items-center gap-2 text-sm font-semibold tracking-tight"
        href={`/ui/${encodeURIComponent(project)}`}
        onClick={(e) => {
          e.preventDefault();
          navigate(`/${encodeURIComponent(project)}`);
        }}
      >
        <svg className="size-[18px] text-primary" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M16 3 L28 10 V22 L16 29 L4 22 V10 Z" stroke="currentColor" strokeWidth="2" />
          <circle cx="16" cy="16" r="4" fill="currentColor" />
        </svg>
        Knowledge Forge
      </a>
      <select
        className="rounded-md border border-input bg-background px-2 py-1 font-mono text-xs text-foreground"
        value={project}
        onChange={(e) => navigate(`/${encodeURIComponent(e.target.value)}`)}
      >
        {(projects ?? [project]).map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
      <PathBar segments={segments} />
      <div className="ml-auto flex flex-none gap-1">
        <CommandPalette project={project} manifest={manifest} />
        <ThemeToggle />
      </div>
    </div>
  );
}
