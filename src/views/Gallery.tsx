import { useEffect, useState } from "react";

import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { FavoriteSyncStatus } from "../types/ipc";

/** 画廊：顶部是和作者/标签页一致的筛选行，下面接作品列表。 */
export function Gallery() {
  const [onlyCollected, setOnlyCollected] = useState(false);
  const [onlySubscribed, setOnlySubscribed] = useState(false);
  const [favoriteStatus, setFavoriteStatus] = useState<FavoriteSyncStatus | null>(null);
  const dataEpoch = useUi((state) => state.dataEpoch);
  const favoritesSynced = (favoriteStatus?.count ?? 0) > 0;

  useEffect(() => {
    let cancelled = false;
    call<FavoriteSyncStatus>(CMD.pixivFavoritesStatus)
      .then((status) => {
        if (!cancelled) setFavoriteStatus(status);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [dataEpoch]);

  const extraQuery = {
    ...(onlyCollected && favoritesSynced ? { favorited_only: true } : {}),
    ...(onlyCollected && !favoritesSynced ? { collected_only: true } : {}),
    ...(onlySubscribed ? { subscribed_only: true } : {}),
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 px-6 pt-4">
        <span className="text-micro text-text-3">筛选</span>
        <button
          type="button"
          className="tri-btn"
          data-active={onlyCollected}
          title={
            favoritesSynced
              ? "只看 Pixiv 收藏里的作品"
              : "还没同步 Pixiv 收藏，先按订阅前就有的作品筛选"
          }
          onClick={() => setOnlyCollected((value) => !value)}
        >
          仅看 Pixiv 收藏
        </button>
        <button
          type="button"
          className="tri-btn"
          data-active={onlySubscribed}
          title="只看已订阅作者的作品"
          onClick={() => setOnlySubscribed((value) => !value)}
        >
          仅看已订阅
        </button>
        <span className="text-micro text-text-3">
          {favoritesSynced
            ? `Pixiv 收藏 ${favoriteStatus?.count ?? 0} 件`
            : "Pixiv 收藏未同步（按订阅前作品算）"}
        </span>
        {(onlyCollected || onlySubscribed) && (
          <button
            type="button"
            className="text-micro text-text-3 hover:text-text"
            onClick={() => {
              setOnlyCollected(false);
              setOnlySubscribed(false);
            }}
          >
            清除筛选
          </button>
        )}
      </div>

      <ArtworkList remoteSearch extraQuery={extraQuery} />
    </div>
  );
}
