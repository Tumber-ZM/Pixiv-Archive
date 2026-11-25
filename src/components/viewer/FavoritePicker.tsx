import { useEffect, useState } from "react";

import { CMD, call, isAppError } from "../../lib/ipc";
import type { ArtworkDetail, FavoriteRow } from "../../types/ipc";
import { Dialog } from "../dialog/Dialog";

interface FavoritePickerProps {
  /** 单张或批量。批量时不预勾选，因此只做「新增」不做「移除」。 */
  artworkIds: number[];
  onClose: () => void;
  onSaved: () => void;
}

/**
 * 收藏夹选择器（`06-界面规格.md` §8）。
 *
 * 单张时会预勾选该作品已在的收藏夹，保存时**对比初始状态**：
 * 新勾的走 add、取消勾的走 remove，所以这里也能取消收藏。
 */
export function FavoritePicker({ artworkIds, onClose, onSaved }: FavoritePickerProps) {
  const [favorites, setFavorites] = useState<FavoriteRow[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [initial, setInitial] = useState<Set<number>>(new Set());
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    call<FavoriteRow[]>(CMD.listFavorites)
      .then((rows) => {
        if (!cancelled) setFavorites(rows);
      })
      .catch(() => undefined);

    // 只有单张时才有明确归属，可以预勾选并支持取消
    if (artworkIds.length === 1) {
      call<ArtworkDetail>(CMD.getArtwork, { artworkId: artworkIds[0] })
        .then((detail) => {
          if (cancelled) return;
          const current = new Set(detail.favorites.map((f) => f.id));
          setSelected(current);
          setInitial(current);
        })
        .catch(() => undefined);
    }

    return () => {
      cancelled = true;
    };
  }, [artworkIds]);

  const toggle = (id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const createdNew = newName.trim().length > 0;
      const added = [...selected].filter((id) => !initial.has(id));
      // 只有预勾选过的收藏夹才谈得上「取消」，批量模式 initial 为空，这里自然是空集
      const removed = [...initial].filter((id) => !selected.has(id));

      if (!createdNew && added.length === 0 && removed.length === 0) {
        setError("没有变化");
        return;
      }

      if (createdNew) {
        const created = await call<FavoriteRow>(CMD.createFavorite, { name: newName.trim() });
        added.push(created.id);
      }

      for (const favoriteId of added) {
        await call(CMD.addToFavorite, { favoriteId, artworkIds });
      }
      for (const favoriteId of removed) {
        await call(CMD.batchRemoveFromFavorite, { favoriteId, artworkIds });
      }

      onSaved();
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      title={artworkIds.length > 1 ? `将 ${artworkIds.length} 件加入本地收藏` : "本地收藏"}
      confirmLabel="保存"
      busy={busy}
      onConfirm={save}
      onCancel={onClose}
    >
      {favorites.length === 0 ? (
        <p className="text-body text-text-2">还没有本地收藏，输入名称即可新建。</p>
      ) : (
        <div className="flex max-h-[240px] flex-col gap-1 overflow-y-auto">
          {favorites.map((favorite) => (
            <label key={favorite.id} className="flex items-center gap-2 py-1">
              <input
                type="checkbox"
                checked={selected.has(favorite.id)}
                onChange={() => toggle(favorite.id)}
              />
              <span className="text-body text-text">{favorite.name}</span>
              <span className="text-micro text-text-3">{favorite.artwork_count} 件</span>
            </label>
          ))}
        </div>
      )}

      <div className="mt-3">
        <input
          className="line-input"
          placeholder="新本地收藏名称（可留空）"
          value={newName}
          onChange={(event) => setNewName(event.currentTarget.value)}
        />
      </div>

      {artworkIds.length === 1 && initial.size > 0 && (
        <p className="mt-2 text-micro text-text-3">取消勾选即可从该本地收藏移除。</p>
      )}
      {error && <p className="mt-2 text-micro text-danger">{error}</p>}
    </Dialog>
  );
}