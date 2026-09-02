import { Command } from "cmdk";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { useSearchIndex } from "@/hooks/useSearchIndex";
import { TypeDot } from "@/components/TypeBadge";
import { Button } from "@/components/ui/button";
import type { Manifest } from "@/lib/api";

export function CommandPalette({ project, manifest }: { project: string; manifest?: Manifest }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const { entries } = useSearchIndex(project, manifest);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <>
      <Button variant="ghost" size="icon" title="전역 검색 (⌘K)" onClick={() => setOpen(true)}>
        <Search size={15} />
      </Button>
      <Command.Dialog
        open={open}
        onOpenChange={setOpen}
        label="전역 검색"
        shouldFilter
        loop
        overlayClassName="fixed inset-0 z-50 bg-black/50 backdrop-blur-[1px]"
        contentClassName="fixed top-[14vh] left-1/2 z-50 w-[min(560px,92vw)] -translate-x-1/2 overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-2xl"
      >
        <div className="flex items-center border-b border-border px-3">
          <Search className="mr-2 size-4 shrink-0 text-muted-foreground" />
          <Command.Input
            placeholder="오브젝트 제목·설명·태그로 검색…"
            autoFocus
            className="h-11 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>
        <Command.List className="max-h-90 overflow-y-auto p-1.5">
          <Command.Empty className="py-6 text-center text-sm text-muted-foreground">
            일치하는 오브젝트가 없습니다.
          </Command.Empty>
          <Command.Group
            heading={`${project} · objects`}
            className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:text-[0.65rem] [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-muted-foreground [&_[cmdk-group-heading]]:uppercase"
          >
            {entries.map((entry) => (
              <Command.Item
                key={entry.path}
                value={[entry.title, entry.description, ...(entry.tags ?? [])].filter(Boolean).join(" ")}
                onSelect={() => {
                  setOpen(false);
                  navigate(`/${encodeURIComponent(project)}/f/${entry.path}`);
                }}
                className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground"
              >
                <TypeDot type={entry.type} />
                <span className="truncate">{entry.title || entry.path}</span>
                <span className="ml-auto font-mono text-xs text-muted-foreground">{entry.type}</span>
              </Command.Item>
            ))}
          </Command.Group>
        </Command.List>
      </Command.Dialog>
    </>
  );
}
