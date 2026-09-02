import { useMemo, useState } from "react";
import { TypeHallmark } from "./TypeBadge";

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
        <div style={{ marginBottom: 16 }}>
          <input
            className="filter-input"
            placeholder="제목·설명·태그 검색…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      )}
      {filtered.length === 0 ? (
        <div className="empty">{q ? "일치하는 항목이 없습니다." : emptyMessage}</div>
      ) : (
        <div className="grid">
          {filtered.map((item) => (
            <button type="button" className="card" key={item.path} onClick={() => onSelect(item)}>
              <div className="row">
                {item.type && <TypeHallmark type={item.type} />}
                <div className="title">{item.title}</div>
              </div>
              {item.desc && <div className="desc">{item.desc}</div>}
              {item.tags && item.tags.length > 0 && (
                <div className="chips">
                  {item.tags.slice(0, 6).map((t) => (
                    <span className="chip" key={t}>
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
