import { useState } from "react";

import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi } from "../../stores/ui";
import type { FavoriteSyncReport } from "../../types/ipc";
import { TopbarButton } from "./TopbarButton";

/**
 * 作者列表的两个维护动作。放在顶栏搜索框后面，和原来的 .btn 按钮形式一致。
 *
 * 只负责触发和提示；列表刷新通过 bumpData 触发 Authors 重新拉取。
 */
export function AuthorActions() {
  const bumpData = useUi((state) => state.bumpData);
  const setScanTask = useUi((state) => state.setScanTask);

  const [refreshing, setRefreshing] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const flash = (message: string, ms = 5000) => {
    setNotice(message);
    window.setTimeout(() => setNotice(null), ms);
  };

  // 用 pixiv_user_id 逐个拉头像并覆盖本地缓存（进度走右下角浮窗）
  const refreshAvatars = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setNotice(null);
    setScanTask({
      current: 0,
      total: 0,
      imported: 0,
      message: "更新作者头像…",
      kind: "avatars",
    });
    try {
      const updated = await call<number>(CMD.refreshAuthorAvatars);
      bumpData();
      flash(`已更新 ${updated} 个头像`, 4000);
    } catch (reason: unknown) {
      flash(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setRefreshing(false);
      setScanTask(null);
    }
  };

  // 同步「我的 Pixiv 收藏」：只读作品 ID，写完刷新列表让收藏筛选立刻生效
  const syncFavorites = async () => {
    if (syncing) return;
    setSyncing(true);
    setNotice(null);
    setScanTask({
      current: 0,
      total: 0,
      imported: 0,
      message: "准备同步 Pixiv 收藏…",
      kind: "favorites",
    });
    try {
      const report = await call<FavoriteSyncReport>(CMD.syncPixivFavorites);
      bumpData();
      flash(
        report.cancelled
          ? "Pixiv 收藏同步已取消"
          : `已同步 ${report.total} 件 Pixiv 收藏（公开 ${report.public} · 私密 ${report.private}）`,
      );
    } catch (reason: unknown) {
      flash(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setSyncing(false);
      setScanTask(null);
    }
  };

  return (
    <>
      <TopbarButton
        onClick={refreshAvatars}
        disabled={refreshing}
        title="用作者 ID 从 Pixiv 重新获取并覆盖本地头像"
      >
        {refreshing ? "更新中…" : "更新作者头像"}
      </TopbarButton>
      <TopbarButton
        onClick={syncFavorites}
        disabled={syncing}
        title="读取你 Pixiv 账号的公开 + 私密收藏，只在本机记录作品 ID，用来筛选作者"
      >
        {syncing ? "同步中…" : "同步 Pixiv 收藏"}
      </TopbarButton>
      {notice && (
        <span className="max-w-[220px] truncate text-micro text-text-3" title={notice}>
          {notice}
        </span>
      )}
    </>
  );
}
