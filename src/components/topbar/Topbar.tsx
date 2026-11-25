import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Columns3, LayoutGrid, Search } from "lucide-react";

import { SORT_LABELS, TRI_LABELS, useUi, type SortKey } from "../../stores/ui";

const SORT_KEYS = Object.keys(SORT_LABELS) as SortKey[];

export function Topbar() {
  const q = useUi((s) => s.q);
  const sort = useUi((s) => s.sort);
  const order = useUi((s) => s.order);
  const r18 = useUi((s) => s.r18);
  const ai = useUi((s) => s.ai);
  const gif = useUi((s) => s.gif);
  const deletedOnly = useUi((s) => s.deletedOnly);
  const layout = useUi((s) => s.layout);

  const setQ = useUi((s) => s.setQ);
  const setSort = useUi((s) => s.setSort);
  const cycleTri = useUi((s) => s.cycleTri);
  const toggleDeletedOnly = useUi((s) => s.toggleDeletedOnly);
  const toggleLayout = useUi((s) => s.toggleLayout);

  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [menuOpen]);

  return (
    <header className="flex h-topbar shrink-0 items-center gap-3 border-b border-line bg-paper px-6">
      <div className="flex w-[320px] items-center gap-2">
        <Search size={16} className="shrink-0 text-text-3" />
        <input
          className="line-input"
          placeholder="搜索标题 / 作者 / 标签 / Pixiv ID…"
          value={q}
          onChange={(event) => setQ(event.currentTarget.value)}
        />
      </div>

      <div className="relative" ref={menuRef}>
        <button
          type="button"
          className="tri-btn inline-flex items-center gap-1"
          onClick={() => setMenuOpen((open) => !open)}
          aria-expanded={menuOpen}
        >
          排序：{SORT_LABELS[sort]}
          {order === "desc" ? <ArrowDown size={12} /> : <ArrowUp size={12} />}
        </button>

        {menuOpen && (
          <div className="absolute left-0 top-full z-20 mt-2 w-44 rounded-btn border border-line bg-surface p-1">
            {SORT_KEYS.map((key) => (
              <button
                key={key}
                type="button"
                className="flex w-full items-center justify-between rounded-btn px-3 py-2 text-label text-text-2 hover:bg-surface-2"
                onClick={() => {
                  setSort(key);
                  setMenuOpen(false);
                }}
              >
                <span>{SORT_LABELS[key]}</span>
                {sort === key && (
                  <span className="text-micro text-text-3">
                    {order === "desc" ? "降序" : "升序"}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      <button
        type="button"
        className="tri-btn"
        data-active={r18 !== "all"}
        onClick={() => cycleTri("r18")}
      >
        R18：{TRI_LABELS[r18]}
      </button>
      <button
        type="button"
        className="tri-btn"
        data-active={ai !== "all"}
        onClick={() => cycleTri("ai")}
      >
        AI：{TRI_LABELS[ai]}
      </button>
      <button
        type="button"
        className="tri-btn"
        data-active={gif !== "all"}
        onClick={() => cycleTri("gif")}
      >
        GIF：{TRI_LABELS[gif]}
      </button>
      <button
        type="button"
        className="tri-btn"
        data-active={deletedOnly}
        onClick={toggleDeletedOnly}
      >
        已删除
      </button>

      <button
        type="button"
        className="icon-btn ml-1"
        onClick={toggleLayout}
        title={layout === "grid" ? "切换为瀑布流布局" : "切换为网格布局"}
        aria-label="切换布局"
      >
        {layout === "grid" ? <Columns3 size={16} /> : <LayoutGrid size={16} />}
      </button>
    </header>
  );
}