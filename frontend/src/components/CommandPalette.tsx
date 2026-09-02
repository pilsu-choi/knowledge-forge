import { Command } from "cmdk";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { useSearchIndex } from "../hooks/useSearchIndex";
import { TypeHallmark } from "./TypeBadge";
import type { Manifest } from "../lib/api";

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
      <button type="button" className="icon-btn" title="전역 검색 (⌘K)" onClick={() => setOpen(true)}>
        <Search size={15} />
      </button>
      <Command.Dialog
        open={open}
        onOpenChange={setOpen}
        label="전역 검색"
        shouldFilter
        loop
      >
        <Command.Input placeholder="오브젝트 제목·설명·태그로 검색…" autoFocus />
        <Command.List>
          <Command.Empty>일치하는 오브젝트가 없습니다.</Command.Empty>
          <Command.Group heading={`${project} · objects`}>
            {entries.map((entry) => (
              <Command.Item
                key={entry.path}
                value={[entry.title, entry.description, ...(entry.tags ?? [])].filter(Boolean).join(" ")}
                onSelect={() => {
                  setOpen(false);
                  navigate(`/${encodeURIComponent(project)}/f/${entry.path}`);
                }}
              >
                <TypeHallmark type={entry.type} size="sm" />
                <span>{entry.title || entry.path}</span>
                <span className="hint">{entry.type}</span>
              </Command.Item>
            ))}
          </Command.Group>
        </Command.List>
      </Command.Dialog>
    </>
  );
}
