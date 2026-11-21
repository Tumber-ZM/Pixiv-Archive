import { useEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Heart, Plus } from "lucide-react";

import { ContextMenu } from "../components/common/ContextMenu";
import { EmptyState } from "../components/common/EmptyState";
import { Dialog } from "../components/dialog/Dialog";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { ArtworkCard, BatchResult, FavoriteRow, Page } from "../types/ipc";

/** 当前正在看的本地收藏。跳转只需要 id + 名称。 */
interface CurrentFavorite {
  id: number;
  name: string;
}

/** 封面拼图：取该本地收藏前 4 张缩略图铺 2×2（`06-界面规格.md` §6）。 */
function Cover({ paths }: { paths: string[] }) {
  return (
    <div className="grid grid-cols-2 gap-px bg-line">
      {Array.from({ length: 4 }).map((_, index) => {
        const path = paths[index];
        return (
          <div key={index} className="aspect-square overflow-hidden bg-surface-2">
            {path ? (
              <img
                src={convertFileSrc(path)}
                alt=""
                className="h-full w-full object-cover"
                loading="lazy"
                decoding="async"
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export function Favorites() {
  const [rows, setRows] = useState<FavoriteRow[]>([]);
  const [covers, setCovers] = useState<Record<number, string[]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<CurrentFavorite | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  // 查看器里点本地收藏会写 pendingFavorite，这里消费一次后清空
  const pendingFavorite = useUi((state) => state.pendingFavorite);
  const consumePending = useUi((state) => state.consumePending);

  useEffect(() => {
    if (!pendingFavorite) return;
    setCurrent(pendingFavorite);
    consumePending();
  }, [pendingFavorite, consumePending]);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<FavoriteRow | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; row: FavoriteRow } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    call<FavoriteRow[]>(CMD.listFavorites)
      .then(async (favorites) => {
        if (cancelled) return;
        setRows(favorites);
        setError(null);

        // 封面单独拉，本地收藏数量很少，逐个取 4 张可接受
        for (const favorite of favorites) {
          const page = await call<Page<ArtworkCard>>(CMD.listArtworks, {
            query: { favorite_id: favorite.id, page_size: 4 },
          }).catch(() => null);
          if (cancelled || !page) continue;
          setCovers((current) => ({
            ...current,
            [favorite.id]: page.items
              .map((item) => item.cover_path)
              .filter((path): path is string => Boolean(path)),
          }));
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(isAppError(reason) ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  const create = async () => {
    setBusy(true);
    try {
      await call(CMD.createFavorite, { name: newName.trim() });
      setCreateOpen(false);
      setNewName("");
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await call<BatchResult>(CMD.deleteFavorite, { favoriteId: deleteTarget.id });
      setDeleteTarget(null);
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  if (current) {
    return (
      <ArtworkList
        extraQuery={{ favorite_id: current.id }}
        heading={current.name}
        onBack={() => {
          setCurrent(null);
          setRefreshKey((key) => key + 1);
        }}
      />
    );
  }

  return (
    <div className="px-6 py-4">
      <div className="mb-4 flex h-10 items-center">
        <span className="text-body text-text">本地收藏</span>
        <button
          type="button"
          className="btn ml-auto inline-flex items-center gap-2"
          onClick={() => setCreateOpen(true)}
        >
          <Plus size={14} />
          新建本地收藏
        </button>
      </div>

      {error ? (
        <EmptyState title="加载失败" hint={error} />
      ) : loading ? (
        <p className="py-8 text-center text-micro text-text-2">加载中…</p>
      ) : rows.length === 0 ? (
        <EmptyState icon={Heart} title="还没有本地收藏" hint="新建本地收藏后可以把作品归档进去" />
      ) : (
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(240px,1fr))]">
          {rows.map((row) => (
            <div
              key={row.id}
              className="overflow-hidden rounded-card border border-line bg-surface"
              onContextMenu={(event) => {
                event.preventDefault();
                setMenu({ x: event.clientX, y: event.clientY, row });
              }}
            >
              <button type="button" className="block w-full" onClick={() => setCurrent({ id: row.id, name: row.name })}>
                <Cover paths={covers[row.id] ?? []} />
                <div className="flex items-baseline gap-2 px-3 py-2.5 text-left">
                  <span className="truncate-1 text-body text-text">{row.name}</span>
                  <span className="ml-auto shrink-0 text-micro text-text-2">
                    {row.artwork_count} 件
                  </span>
                </div>
              </button>
            </div>
          ))}
        </div>
      )}

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
          items={[
            { label: "打开", onSelect: () => setCurrent({ id: menu.row.id, name: menu.row.name }) },
            { label: "删除", danger: true, onSelect: () => setDeleteTarget(menu.row) },
          ]}
        />
      )}

      <Dialog
        open={createOpen}
        title="新建本地收藏"
        confirmLabel="创建"
        busy={busy}
        onCancel={() => setCreateOpen(false)}
        onConfirm={create}
      >
        <input
          className="line-input"
          placeholder="本地收藏名称"
          value={newName}
          onChange={(event) => setNewName(event.currentTarget.value)}
          autoFocus
        />
      </Dialog>

      <Dialog
        open={deleteTarget !== null}
        title="删除本地收藏"
        danger
        busy={busy}
        confirmLabel="删除"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={remove}
      >
        <p>将删除「{deleteTarget?.name}」。本地收藏里的作品不会被删除。</p>
      </Dialog>
    </div>
  );
}