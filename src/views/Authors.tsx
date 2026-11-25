import { useEffect, useLayoutEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Users } from "lucide-react";

import { EmptyState } from "../components/common/EmptyState";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { AuthorRow } from "../types/ipc";

const SEARCH_DEBOUNCE_MS = 300;

/** 有本地头像就用图，没有再用首字母占位（`06-界面规格.md` §4） */
function Avatar({
  name,
  path,
  size = 40,
}: {
  name: string;
  path?: string | null;
  size?: number;
}) {
  // 只处理本地缓存文件；旧库里残留的 pixiv URL 在 WebView 里带不了 Referer，加载会失败
  const local = path && !path.startsWith("http") ? path : null;
  if (local) {
    return (
      <img
        src={convertFileSrc(local)}
        alt=""
        className="shrink-0 rounded-full object-cover"
        style={{ width: size, height: size }}
        loading="lazy"
        decoding="async"
      />
    );
  }
  return (
    <span
      className="flex shrink-0 items-center justify-center rounded-full bg-surface-2 text-text-2"
      style={{ width: size, height: size, fontSize: size >= 48 ? 18 : 14 }}
    >
      {name.slice(0, 1)}
    </span>
  );
}

/**
 * 作者列表卡片（参考图一）：顶部一条 3 张作品封面，下面头像 + 名字 + 件数。
 * 封面由后端在 `list_authors` 里一并取出，列表不再逐个发请求。
 */
function AuthorCard({ row, onOpen }: { row: AuthorRow; onOpen: () => void }) {
  const covers = row.covers.slice(0, 3);

  return (
    <button type="button" className="author-card" onClick={onOpen} title={row.name}>
      <div className="grid grid-cols-3 gap-px bg-line">
        {Array.from({ length: 3 }).map((_, index) => {
          const path = covers[index];
          return (
            <div key={index} className="aspect-square overflow-hidden bg-surface-2">
              {path && (
                <img
                  src={convertFileSrc(path)}
                  alt=""
                  className="h-full w-full object-cover"
                  loading="lazy"
                  decoding="async"
                />
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-3 px-3 py-2.5">
        <Avatar name={row.name} path={row.avatar_path} size={36} />
        <div className="min-w-0 flex-1">
          <p className="truncate-1 text-body text-text">{row.name}</p>
          <p className="truncate-1 text-micro text-text-3">Pixiv {row.pixiv_user_id}</p>
        </div>
        <span className="shrink-0 text-micro text-text-2">{row.artwork_count} 件</span>
      </div>
    </button>
  );
}

export function Authors() {
  const q = useUi((state) => state.pageSearch.authors);
  const setNestedView = useUi((state) => state.setNestedView);
  const [rows, setRows] = useState<AuthorRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<AuthorRow | null>(null);
  const pendingAuthor = useUi((state) => state.pendingAuthor);
  const consumePending = useUi((state) => state.consumePending);
  // 删除作品后 dataEpoch 递增，列表跟着重新拉一次
  const dataEpoch = useUi((state) => state.dataEpoch);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      setLoading(true);
      call<AuthorRow[]>(CMD.listAuthors, { q: q.trim() || undefined })
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

  // 打开某个作者的作品列表时，把顶栏交还给全局筛选（作者搜索框只在列表态显示）
  useLayoutEffect(() => {
    setNestedView(current !== null);
    return () => setNestedView(false);
  }, [current, setNestedView]);

  // 从查看器点作者名进来：直接打开该作者的作品列表。这里只用到 id 与 name，
  // 其余 AuthorRow 字段不会出现在作品列表里，给占位值即可。
  useEffect(() => {
    if (!pendingAuthor) return;
    setCurrent({
      id: pendingAuthor.id,
      pixiv_user_id: 0,
      name: pendingAuthor.name,
      artwork_count: 0,
      avatar_path: null,
      covers: [],
    });
    consumePending();
  }, [pendingAuthor, consumePending]);

  // 视图内切到该作者的作品，不再单独做作者信息头部
  if (current) {
    return (
      <ArtworkList
        extraQuery={{ author_id: current.id }}
        heading={current.name}
        onBack={() => setCurrent(null)}
      />
    );
  }

  return (
    <div className="px-6 py-4">
      <div>
        {error ? (
          <EmptyState title="加载失败" hint={error} />
        ) : loading ? (
          <p className="py-8 text-center text-micro text-text-2">加载中…</p>
        ) : rows.length === 0 ? (
          <EmptyState icon={Users} title="没有找到作者" hint="换个关键词，或先扫描/同步作品" />
        ) : (
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]">
            {rows.map((row) => (
              <AuthorCard key={row.id} row={row} onOpen={() => setCurrent(row)} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}