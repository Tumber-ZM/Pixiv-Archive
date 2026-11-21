import { useEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Bell, Users } from "lucide-react";

import { EmptyState } from "../components/common/EmptyState";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type {
  AuthorRow,
  FavoriteSyncReport,
  FavoriteSyncStatus,
  Sub,
} from "../types/ipc";

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
 *
 * 整卡是 role=button 的 div：卡片里还有订阅按钮，`<button>` 不能嵌套 `<button>`。
 */
function AuthorCard({
  row,
  onOpen,
  onToggleSubscribe,
  pending,
}: {
  row: AuthorRow;
  onOpen: () => void;
  onToggleSubscribe: () => void;
  pending: boolean;
}) {
  const covers = row.covers.slice(0, 3);

  return (
    <div
      className="author-card"
      role="button"
      tabIndex={0}
      title={row.name}
      onClick={onOpen}
      onKeyDown={(event) => {
        // 焦点在订阅按钮上时，回车/空格应该触发订阅，而不是打开作者页
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
    >
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
          <p className="mt-0.5 flex items-center gap-1.5 text-micro text-text-3">
            <span className="truncate-1">Pixiv {row.pixiv_user_id}</span>
            <button
              type="button"
              className="author-sub shrink-0"
              data-on={row.subscribed}
              disabled={pending}
              title={row.subscribed ? "已订阅该作者，点击退订" : "订阅该作者的新作品"}
              onClick={(event) => {
                event.stopPropagation();
                onToggleSubscribe();
              }}
            >
              <Bell size={9} aria-hidden />
              {pending ? "…" : row.subscribed ? "已订阅" : "订阅"}
            </button>
          </p>
        </div>
        <span className="shrink-0 text-micro text-text-2">{row.artwork_count} 件</span>
      </div>
    </div>
  );
}

export function Authors() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AuthorRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<AuthorRow | null>(null);
  const pendingAuthor = useUi((state) => state.pendingAuthor);
  const consumePending = useUi((state) => state.consumePending);
  const setScanTask = useUi((state) => state.setScanTask);
  const bumpData = useUi((state) => state.bumpData);
  // 删除作品后 dataEpoch 递增，列表跟着重新拉一次
  const dataEpoch = useUi((state) => state.dataEpoch);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  // 正在切换订阅的作者（Pixiv 用户 ID），避免连点时重复请求
  const [pendingSub, setPendingSub] = useState<number | null>(null);
  // 作者列表筛选：默认「仅看收藏」，把作者订阅带进来的新作者先收起来
  const [onlyCollected, setOnlyCollected] = useState(true);
  const [onlySubscribed, setOnlySubscribed] = useState(false);
  // Pixiv 收藏概况（同步过才按收藏筛选，没同步时退回「订阅前就有」的口径）
  const [favoriteStatus, setFavoriteStatus] = useState<FavoriteSyncStatus | null>(null);
  const [syncingFavorites, setSyncingFavorites] = useState(false);

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
  }, [q, refreshKey, dataEpoch]);

  // Pixiv 收藏概况：同步过才按真实收藏筛选，没同步时退回「订阅前就有」的口径
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
  }, [refreshKey]);

  // 在作者卡上订阅/退订：作者订阅默认只追新，新作品等「订阅更新」时下载
  const toggleSubscribe = async (row: AuthorRow) => {
    if (pendingSub !== null) return;
    setPendingSub(row.pixiv_user_id);
    setNotice(null);
    try {
      if (row.subscribed) {
        await call<boolean>(CMD.removeSub, {
          kind: "author",
          targetPixivId: row.pixiv_user_id,
        });
        setRows((prev) =>
          prev.map((item) => (item.id === row.id ? { ...item, subscribed: false } : item)),
        );
        setNotice(`已退订「${row.name}」`);
      } else {
        await call<Sub>(CMD.addSub, {
          kind: "author",
          targetPixivId: row.pixiv_user_id,
          backfill: false,
        });
        setRows((prev) =>
          prev.map((item) => (item.id === row.id ? { ...item, subscribed: true } : item)),
        );
        setNotice(`已订阅「${row.name}」，新作品会在「订阅更新」时下载`);
      }
      setTimeout(() => setNotice(null), 4000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setPendingSub(null);
    }
  };

  // 同步「我的 Pixiv 收藏」：只读作品 ID，写完刷新列表让收藏筛选立刻生效
  const syncFavorites = async () => {
    if (syncingFavorites) return;
    setSyncingFavorites(true);
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
      setRefreshKey((key) => key + 1);
      setNotice(
        report.cancelled
          ? "收藏同步已取消"
          : `已同步 ${report.total} 件收藏（公开 ${report.public} · 私密 ${report.private}）`,
      );
      setTimeout(() => setNotice(null), 5000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setSyncingFavorites(false);
      setScanTask(null);
    }
  };

  // 用 pixiv_user_id 逐个拉头像并覆盖本地缓存（进度走右下角浮窗）
  const refreshAvatars = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setNotice(null);
    setScanTask({ current: 0, total: 0, imported: 0, message: "更新作者头像…", kind: "subs" });
    try {
      const updated = await call<number>(CMD.refreshAuthorAvatars);
      setRefreshKey((key) => key + 1);
      bumpData();
      setNotice(`已更新 ${updated} 个头像`);
      setTimeout(() => setNotice(null), 4000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setRefreshing(false);
      setScanTask(null);
    }
  };

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
      subscribed: false,
      collected: true,
      favorited: false,
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

  const favoritesSynced = (favoriteStatus?.count ?? 0) > 0;
  const visibleRows = rows.filter((row) => {
    if (onlyCollected) {
      // 同步过收藏就严格按 Pixiv 收藏；没同步时退回「订阅前就有」的判断
      const collected = favoritesSynced ? row.favorited : row.collected;
      if (!collected) return false;
    }
    return !onlySubscribed || row.subscribed;
  });

  return (
    <div className="px-6 py-4">
      <div className="flex items-center gap-3">
        {/* .line-input 自带 width:100%，必须给它一个定宽父级，否则会把按钮挤出去 */}
        <div className="w-[320px]">
          <input
            className="line-input"
            placeholder="搜索作者…"
            value={q}
            onChange={(event) => setQ(event.currentTarget.value)}
          />
        </div>
        <button
          type="button"
          className="btn shrink-0"
          onClick={refreshAvatars}
          disabled={refreshing}
          title="用作者 ID 从 Pixiv 重新获取并覆盖本地头像"
        >
          {refreshing ? "更新中…" : "更新作者头像"}
        </button>
        <button
          type="button"
          className="btn shrink-0"
          onClick={syncFavorites}
          disabled={syncingFavorites}
          title="读取你 Pixiv 账号的公开 + 私密收藏，只在本机记录作品 ID，用来筛选作者"
        >
          {syncingFavorites ? "同步中…" : "同步收藏"}
        </button>
        {notice && <span className="text-micro text-text-2">{notice}</span>}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-micro text-text-3">筛选</span>
        <button
          type="button"
          className="tri-btn"
          data-active={onlyCollected}
          title={
            favoritesSynced
              ? "只看本地作品在 Pixiv 收藏里的作者"
              : "只看订阅前就在库里的作者；先「同步收藏」可改为按 Pixiv 收藏筛选"
          }
          onClick={() => setOnlyCollected((value) => !value)}
        >
          仅看收藏
        </button>
        <button
          type="button"
          className="tri-btn"
          data-active={onlySubscribed}
          title="只看已订阅的作者"
          onClick={() => setOnlySubscribed((value) => !value)}
        >
          仅看已订阅
        </button>
        {!loading && !error && (
          <span className="text-micro text-text-3">共 {visibleRows.length} 位作者</span>
        )}
        <span className="text-micro text-text-3">
          {syncingFavorites
            ? "收藏同步中…"
            : favoriteStatus?.last_synced
              ? `Pixiv 收藏 ${favoriteStatus.count} 件`
              : "Pixiv 收藏未同步"}
        </span>
        {visibleRows.length !== rows.length && (
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

      <div className="mt-4">
        {error ? (
          <EmptyState title="加载失败" hint={error} />
        ) : loading ? (
          <p className="py-8 text-center text-micro text-text-2">加载中…</p>
        ) : rows.length === 0 ? (
          <EmptyState icon={Users} title="没有找到作者" hint="换个关键词，或先扫描/同步作品" />
        ) : visibleRows.length === 0 ? (
          <EmptyState
            icon={Users}
            title="没有符合筛选的作者"
            hint="取消「仅看收藏」或「仅看已订阅」查看全部"
          />
        ) : (
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]">
            {visibleRows.map((row) => (
              <AuthorCard
                key={row.id}
                row={row}
                onOpen={() => setCurrent(row)}
                onToggleSubscribe={() => toggleSubscribe(row)}
                pending={pendingSub === row.pixiv_user_id}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
