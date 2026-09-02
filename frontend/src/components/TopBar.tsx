import { useNavigate } from "react-router-dom";
import { PathBar } from "./PathBar";
import { ThemeToggle } from "./ThemeToggle";
import { CommandPalette } from "./CommandPalette";
import { useSegments } from "../lib/pathSegments";
import { useProjects } from "../hooks/useOkf";
import type { Manifest } from "../lib/api";

export function TopBar({ project, manifest }: { project: string; manifest?: Manifest }) {
  const navigate = useNavigate();
  const segments = useSegments();
  const { data: projects } = useProjects();

  return (
    <div className="topbar">
      <a className="brand" href={`/ui/${encodeURIComponent(project)}`} onClick={(e) => { e.preventDefault(); navigate(`/${encodeURIComponent(project)}`); }}>
        <svg className="mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M16 3 L28 10 V22 L16 29 L4 22 V10 Z" stroke="currentColor" strokeWidth="2" />
          <circle cx="16" cy="16" r="4" fill="currentColor" />
        </svg>
        Knowledge Forge
      </a>
      <select
        className="select"
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
      <div className="flex gap-2 ml-auto flex-none">
        <CommandPalette project={project} manifest={manifest} />
        <ThemeToggle />
      </div>
    </div>
  );
}
