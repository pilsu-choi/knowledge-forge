import { NavLink } from "react-router-dom";
import type { Manifest } from "@/lib/api";
import { typeCounts } from "@/lib/api";
import { TypeDot } from "@/components/TypeBadge";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import { Network, Files, ListTree, History, FileText } from "lucide-react";

const navItemClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
    isActive && "bg-sidebar-accent font-medium text-sidebar-accent-foreground",
  );

export function Rail({ project, manifest }: { project: string; manifest: Manifest }) {
  const counts = typeCounts(manifest);
  const resCount = manifest.resources.length;
  const orphanCount = manifest.relations.length + manifest.links.length;
  const base = `/${encodeURIComponent(project)}`;

  return (
    <aside className="flex w-60 flex-none flex-col gap-5 overflow-y-auto border-r border-sidebar-border bg-sidebar px-3 py-4 text-sidebar-foreground">
      <div className="px-1 text-xs text-muted-foreground">
        {resCount} resources · {manifest.objects.length} objects
        {orphanCount > 0 && ` · ${orphanCount} orphan edges`}
      </div>

      <div>
        <div className="mb-1.5 px-1 text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">
          Objects
        </div>
        <div className="flex flex-col gap-0.5">
          {Object.keys(counts)
            .sort()
            .map((type) => (
              <NavLink key={type} to={`${base}/type/${encodeURIComponent(type)}`} className={navItemClass}>
                <TypeDot type={type} />
                <span className="truncate">{type}</span>
                <span className="ml-auto font-mono text-xs text-muted-foreground">{counts[type]}</span>
              </NavLink>
            ))}
        </div>
      </div>

      <Separator className="bg-sidebar-border" />

      <div className="flex flex-col gap-0.5">
        <NavLink to={`${base}/graph`} className={navItemClass}>
          <Network className="size-3.5 text-muted-foreground" />
          그래프
        </NavLink>
        <NavLink to={`${base}/resources`} className={navItemClass}>
          <Files className="size-3.5 text-muted-foreground" />
          resources
          <span className="ml-auto font-mono text-xs text-muted-foreground">{resCount}</span>
        </NavLink>
        <NavLink to={`${base}/f/views/by-topic.md`} className={navItemClass}>
          <ListTree className="size-3.5 text-muted-foreground" />
          주제별 보기
        </NavLink>
        <NavLink to={`${base}/f/log.md`} className={navItemClass}>
          <History className="size-3.5 text-muted-foreground" />
          변경 이력 (log)
        </NavLink>
        <NavLink to={`${base}/f/index.md`} className={navItemClass}>
          <FileText className="size-3.5 text-muted-foreground" />
          index.md
        </NavLink>
      </div>
    </aside>
  );
}
