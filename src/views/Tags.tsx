import { useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Tag as TagIcon } from "lucide-react";

import { EmptyState } from "../components/common/EmptyState";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { Page, TagRow } from "../types/ipc";

const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZE = 60;

/** 当前正在看的标签。跳转只需要名字，不必带完整 TagRow。 */
interface CurrentTag {
  name: string;
  label: string;
}

/**
 * 标签墙。和画廊一致用无限滚动：每批 60 条，底部哨兵进入视口就取下一批并追加。
 */
export function Tags() {
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [items, setItems] = useState<TagRow[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<CurrentTag | null>(null);

  const sentinelRef = useRef<HTMLDivElement>(null);
  // 防止同一批被重复请求（快速滚动时哨兵会连续触发）
  const inFlight = useRef(false);

  // 查看器里点标签会写 pendingTag，这里消费一次后清空
  const pendingTag = useUi((state) => state.pendingTag);
  const consumePending = useUi((state) => state.consumePending);
  // 删除作品后 dataEpoch 递增，列表跟着重新拉一次
  const dataEpoch = useUi((state) => state.dataEpoch);

  useEffect(() => {
    if (!pendingTag) return;
    setCurrent({ name: pendingTag, label: pendingTag });
    consumePending();
  }, [pendingTag, consumePending]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(q), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [q]);

  // 搜索词或数据版本变了：清空累积结果，退回第 1 批
  useEffect(() => {
    setItems([]);
    setTotal(0);
    setTotalPages(1);
    setPage(1);
  }, [debouncedQ, dataEpoch]);

  useEffect(() => {
    let cancelled = false;

    if (page === 1) setLoading(true);
    else setLoadingMore(true);

    call<Page<TagRow>>(CMD.listTags, {
      query: { q: debouncedQ.trim() || undefined, page, page_size: PAGE_SIZE },
    })
      .then((result) => {
        if (cancelled) return;
        setItems((previous) => {
          // 数据变动后可能重复出现同一个标签，按 id 去重
          const seen = new Set(page === 1 ? [] : previous.map((item) => item.id));
          const fresh = result.items.filter((item) => !seen.has(item.id));
          return page === 1 ? fresh : [...previous, ...fresh];
        });
        setTotal(result.total);
        setTotalPages(Math.max(1, result.total_pages));
        setError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(isAppError(reason) ? reason.message : String(reason));
      })
      .finally(() => {
        if (cancelled) return;
        inFlight.current = false;
        setLoading(false);
        setLoadingMore(false);
      });

    return () => {
      cancelled = true;
      // 注意：不要在这里重置 inFlight。cleanup 发生在下一次请求发出之前，
      // 提前放开会让哨兵在请求完成前再次通过检查、把 page 多推一格。
    };
  }, [debouncedQ, page, dataEpoch]);

  // 底部哨兵：出现即加载下一批。提前 800px 预取，滚动时才不会卡顿。
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries[0]?.isIntersecting) return;
        if (inFlight.current) return;
        if (loading || page >= totalPages) return;
        inFlight.current = true;
        setPage((current) => current + 1);
      },
      { rootMargin: "800px 0px" },
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [loading, page, totalPages]);

  if (current) {
    return (
      <ArtworkList
        extraQuery={{ tag: current.name }}
        heading={`# ${current.label}`}
        onBack={() => setCurrent(null)}
      />
    );
  }

  return (
    <div className="px-6 py-4">
      <input
        className="line-input w-[320px]"
        placeholder="搜索标签…"
        value={q}
        onChange={(event) => setQ(event.currentTarget.value)}
      />

      <div className="mt-4">
        {error ? (
          <EmptyState title="加载失败" hint={error} />
        ) : loading && items.length === 0 ? (
          <p className="py-8 text-center text-micro text-text-2">加载中…</p>
        ) : items.length === 0 ? (
          <EmptyState icon={TagIcon} title="没有找到标签" hint="换个关键词，或先同步元数据" />
        ) : (
          <>
            <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(170px,1fr))]">
              {items.map((row) => (
                <button
                  key={row.id}
                  type="button"
                  className="tag-tile"
                  onClick={() =>
                    setCurrent({ name: row.name, label: row.translated_name ?? row.name })
                  }
                  title={`# ${row.translated_name ?? row.name}（${row.count} 件）`}
                >
                  {row.cover_path && (
                    <img
                      src={convertFileSrc(row.cover_path)}
                      alt=""
                      loading="lazy"
                      decoding="async"
                    />
                  )}
                  <span className="tag-tile-count">{row.count}</span>
                  <span className="tag-tile-label">
                    <span># {row.translated_name ?? row.name}</span>
                    {row.translated_name && <em>{row.name}</em>}
                  </span>
                </button>
              ))}
            </div>

            <div ref={sentinelRef} className="h-12" />

            {loadingMore && (
              <p className="pb-4 text-center text-micro text-text-2">加载中…</p>
            )}
            {!loading && !loadingMore && page >= totalPages && (
              <p className="pb-4 text-center text-micro text-text-3">共 {total} 个标签</p>
            )}
          </>
        )}
      </div>
    </div>
  );
}