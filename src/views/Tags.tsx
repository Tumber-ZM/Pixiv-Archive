import { useEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Tag as TagIcon } from "lucide-react";

import { EmptyState } from "../components/common/EmptyState";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { TagRow } from "../types/ipc";

const SEARCH_DEBOUNCE_MS = 300;

/** 当前正在看的标签。跳转只需要名字，不必带完整 TagRow。 */
interface CurrentTag {
  name: string;
  label: string;
}

export function Tags() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<TagRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<CurrentTag | null>(null);

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
    let cancelled = false;
    const timer = setTimeout(() => {
      setLoading(true);
      call<TagRow[]>(CMD.listTags, { q: q.trim() || undefined })
        .then((data) => {
          if (cancelled) return;
          setRows(data);
          setError(null);
        })
        .catch((reason: unknown) => {
          if (cancelled) return;
          setError(isAppError(reason) ? reason.message : String(reason));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [q, dataEpoch]);

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
        ) : loading ? (
          <p className="py-8 text-center text-micro text-text-2">加载中…</p>
        ) : rows.length === 0 ? (
          <EmptyState icon={TagIcon} title="没有找到标签" hint="换个关键词，或先同步元数据" />
        ) : (
          <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(170px,1fr))]">
            {rows.map((row) => (
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
        )}
      </div>
    </div>
  );
}