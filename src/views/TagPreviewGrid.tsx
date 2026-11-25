import { useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Heart } from "lucide-react";

import { CMD, call } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { TagRow } from "../types/ipc";

const PAGE_SIZE = 30;

interface Props {
  q: string;
  onlyCollected: boolean;
  favoritesSynced: boolean;
  onlySubscribed: boolean;
  selectedTags: string[];
  onToggle: (name: string) => void;
}

/** 没选标签时的可视化标签区：每个标签一张代表封面。 */
export function TagPreviewGrid({
  q,
  onlyCollected,
  favoritesSynced,
  onlySubscribed,
  selectedTags,
  onToggle,
}: Props) {
  const dataEpoch = useUi((state) => state.dataEpoch);
  const [items, setItems] = useState<TagRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const offsetRef = useRef(0);
  const loadingRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    offsetRef.current = 0;
    loadingRef.current = true;
    setItems([]);
    setLoading(true);
    setMore(false);
    setDone(false);
    setError(null);
    call<TagRow[]>(CMD.listTagPreviews, {
      q: q.trim() || null,
      limit: PAGE_SIZE,
      offset: 0,
      favorited_only: onlyCollected && favoritesSynced,
      collected_only: onlyCollected && !favoritesSynced,
      subscribed_only: onlySubscribed,
    })
      .then((rows) => {
        if (cancelled) return;
        setItems(rows);
        offsetRef.current = rows.length;
        setDone(rows.length < PAGE_SIZE);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        loadingRef.current = false;
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [q, onlyCollected, onlySubscribed, favoritesSynced, dataEpoch]);

  const loadMore = () => {
    if (loadingRef.current || done) return;
    loadingRef.current = true;
    setMore(true);
    call<TagRow[]>(CMD.listTagPreviews, {
      q: q.trim() || null,
      limit: PAGE_SIZE,
      offset: offsetRef.current,
      favorited_only: onlyCollected && favoritesSynced,
      collected_only: onlyCollected && !favoritesSynced,
      subscribed_only: onlySubscribed,
    })
      .then((rows) => {
        setItems((prev) => [...prev, ...rows]);
        offsetRef.current += rows.length;
        if (rows.length < PAGE_SIZE) setDone(true);
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        loadingRef.current = false;
        setMore(false);
      });
  };

  if (loading) {
    return (
      <p className="py-10 text-center text-micro text-text-2">
        正在加载标签预览…
      </p>
    );
  }
  if (error) {
    return (
      <p className="py-10 text-center text-micro text-text-2">
        标签预览加载失败：{error}
      </p>
    );
  }
  if (items.length === 0) {
    return (
      <p className="py-10 text-center text-micro text-text-2">
        没有可以预览的标签。
      </p>
    );
  }

  return (
    <div className="px-6 pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="section-label">标签预览</span>
        <span className="text-micro text-text-3">
          点标签直接筛选；已加载 {items.length} 个
        </span>
      </div>
      <div className="mt-3 grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(150px,1fr))]">
        {items.map((tag) => {
          const label = tag.translated_name ?? tag.name;
          const selected = selectedTags.includes(tag.name);
          return (
            <button
              key={tag.id}
              type="button"
              className="tag-preview-card group relative aspect-square overflow-hidden rounded-card border border-line bg-surface-2 text-left data-[active=true]:border-accent"
              data-active={selected}
              onClick={() => onToggle(tag.name)}
              title={`筛选 #${label}`}
            >
              {tag.cover_path ? (
                <img
                  src={convertFileSrc(tag.cover_path)}
                  alt={label}
                  loading="lazy"
                  className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center p-2 text-center text-micro text-text-3">
                  #{label}
                </div>
              )}
              <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent p-2 pt-8">
                <p className="truncate text-label text-white"># {label}</p>
                <p className="text-micro text-white/70">{tag.count} 件</p>
              </div>
              {selected && (
                <span className="absolute left-2 top-2 rounded-full bg-accent px-1.5 py-0.5 text-micro text-white">
                  已选
                </span>
              )}
              <span
                className="pixiv-heart"
                data-active={tag.pixiv_favorited}
                title={
                  tag.pixiv_favorited
                    ? "预览作品已在 Pixiv 收藏"
                    : "预览作品不在 Pixiv 收藏"
                }
              >
                <Heart
                  size={11}
                  fill={tag.pixiv_favorited ? "currentColor" : "none"}
                />
              </span>
            </button>
          );
        })}
      </div>
      {!done && (
        <div className="mt-4 text-center">
          <button
            type="button"
            className="tri-btn"
            disabled={more}
            onClick={loadMore}
          >
            {more ? "加载中…" : "加载更多标签"}
          </button>
        </div>
      )}
    </div>
  );
}
