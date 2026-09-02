import { useMemo, useState } from "react";
import { TypeDot } from "@/components/TypeBadge";
import { StateMessage } from "@/components/StateMessage";

export interface CardItem {
  path: string;
  title: string;
  desc?: string | null;
  tags?: string[];
  type?: string;
  badgeLabel?: string | null;
}

export function CardGrid({
  items,
  onSelect,
  emptyMessage,
  searchable = false,
}: {
  items: CardItem[];
  onSelect: (item: CardItem) => void;
  emptyMessage: string;
  searchable?: boolean;
}) {
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (it) =>
        (it.title || "").toLowerCase().includes(needle) ||
        (it.desc || "").toLowerCase().includes(needle) ||
        (it.tags || []).some((t) => t.toLowerCase().includes(needle)),
    );
  }, [items, q]);

  return (
    <div>
      {searchable && (
        <input
          className="mb-4 w-full max-w-sm rounded-md border border-input bg-background px-3 py-1.5 text-sm outline-none focus:border-ring"
          placeholder="제목·설명·태그 검색…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      {filtered.length === 0 ? (
        <StateMessage>{q ? "일치하는 항목이 없습니다." : emptyMessage}</StateMessage>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3">
          {filtered.map((item) => (
            <button
              type="button"
              key={item.path}
              onClick={() => onSelect(item)}
              className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3.5 text-left transition-colors hover:border-primary/50 hover:bg-accent/40"
            >
              <div className="flex items-center gap-2">
                {item.type && <TypeDot type={item.type} />}
                <div className="truncate text-sm font-medium">{item.title}</div>
              </div>
              {item.desc && <div className="line-clamp-2 text-xs text-muted-foreground">{item.desc}</div>}
              {item.tags && item.tags.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {item.tags.slice(0, 6).map((t) => (
                    <span key={t} className="rounded-full bg-muted px-2 py-0.5 font-mono text-[0.68rem] text-muted-foreground">
                      {t}
                    </span>
                  ))}
                </div>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
