import { useEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Bell, Users } from "lucide-react";

import { EmptyState } from "../components/common/EmptyState";
import { Dialog } from "../components/dialog/Dialog";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type {
  AuthorRow,
  FavoriteSyncStatus,
  RemoteAuthor,
  Sub,
  SubResult,
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

/** 已订阅的作者置顶；同一组内保持后端原有的作品数排序。 */
function pinSubscribed(rows: AuthorRow[]): AuthorRow[] {
  return [...rows].sort((a, b) => Number(b.subscribed) - Number(a.subscribed));
}

export function Authors() {
  const q = useUi((state) => state.q);
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
  const [refreshKey, setRefreshKey] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  // 正在切换订阅的作者（Pixiv 用户 ID），避免连点时重复请求
  const [pendingSub, setPendingSub] = useState<number | null>(null);
  // 作者列表筛选：默认「仅看收藏」，把作者订阅带进来的新作者先收起来
  const [onlyCollected, setOnlyCollected] = useState(true);
  const [onlySubscribed, setOnlySubscribed] = useState(false);
  // Pixiv 收藏概况（同步过才按收藏筛选，没同步时退回「订阅前就有」的口径）
  const [favoriteStatus, setFavoriteStatus] = useState<FavoriteSyncStatus | null>(null);
  // 订阅确认：作者卡上点「订阅」先问是否补全历史
  const [subscribeTarget, setSubscribeTarget] = useState<AuthorRow | null>(null);
  const [subscribeBackfill, setSubscribeBackfill] = useState(false);
  // 本地搜不到作者时，非阻塞地补一次 Pixiv 远端作者搜索
  const [remoteAuthors, setRemoteAuthors] = useState<RemoteAuthor[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [remoteQuery, setRemoteQuery] = useState("");
  const [remoteSubscribed, setRemoteSubscribed] = useState<Set<number>>(new Set());
  const [remotePending, setRemotePending] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      setLoading(true);
      call<AuthorRow[]>(CMD.listAuthors, { q: q.trim() || undefined })
        .then((data) => {
          if (cancelled) return;
          setRows(pinSubscribed(data));
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

  // 本地作者为空时才去 Pixiv 搜；远端请求不阻塞本地列表，失败只提示不弹错
  useEffect(() => {
    const word = q.trim();
    if (!word) {
      setRemoteAuthors([]);
      setRemoteError(null);
      setRemoteLoading(false);
      setRemoteQuery("");
      return undefined;
    }
    if (loading || rows.length > 0) {
      if (rows.length > 0) {
        setRemoteAuthors([]);
        setRemoteError(null);
        setRemoteLoading(false);
      }
      return undefined;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setRemoteLoading(true);
      setRemoteError(null);
      call<RemoteAuthor[]>(CMD.searchPixivAuthors, { word })
        .then((list) => {
          if (cancelled) return;
          setRemoteAuthors(list);
          setRemoteQuery(word);
        })
        .catch((reason: unknown) => {
          if (cancelled) return;
          setRemoteError(isAppError(reason) ? reason.message : String(reason));
          setRemoteAuthors([]);
          setRemoteQuery(word);
        })
        .finally(() => {
          if (!cancelled) setRemoteLoading(false);
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [q, rows.length, loading]);

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
  }, [refreshKey, dataEpoch]);

  // 在作者卡上订阅/退订：作者订阅默认只追新，新作品等「订阅更新」时下载
  const removeSubscription = async (row: AuthorRow) => {
    if (pendingSub !== null) return;
    setPendingSub(row.pixiv_user_id);
    setNotice(null);
    try {
      await call<boolean>(CMD.removeSub, {
        kind: "author",
        targetPixivId: row.pixiv_user_id,
      });
      setRows((prev) =>
        prev.map((item) => (item.id === row.id ? { ...item, subscribed: false } : item)),
      );
      setNotice(`已退订「${row.name}」`);
      setTimeout(() => setNotice(null), 4000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setPendingSub(null);
    }
  };

  // 远端作者列表里的「订阅」：默认只订阅新作品，不自动补全
  const subscribeRemote = async (author: RemoteAuthor) => {
    if (remotePending !== null) return;
    setRemotePending(author.pixiv_user_id);
    setNotice(null);
    try {
      await call<Sub>(CMD.addSub, {
        kind: "author",
        targetPixivId: author.pixiv_user_id,
        backfill: false,
      });
      setRemoteSubscribed((prev) => new Set(prev).add(author.pixiv_user_id));
      setNotice(`已订阅「${author.name}」，新作品会在订阅更新时下载`);
      setTimeout(() => setNotice(null), 6000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setRemotePending(null);
    }
  };

  const confirmSubscribe = async () => {    const row = subscribeTarget;
    if (!row || pendingSub !== null) return;
    setPendingSub(row.pixiv_user_id);
    setNotice(null);
    try {
      await call<Sub>(CMD.addSub, {
        kind: "author",
        targetPixivId: row.pixiv_user_id,
        backfill: subscribeBackfill,
      });
      setRows((prev) =>
        pinSubscribed(
          prev.map((item) => (item.id === row.id ? { ...item, subscribed: true } : item)),
        ),
      );
      setSubscribeTarget(null);

      if (subscribeBackfill) {
        // 勾了补全就顺手跑一次检查，把该作者的历史作品拉进来
        setScanTask({
          current: 0,
          total: 0,
          imported: 0,
          message: "准备补全该作者的作品…",
          kind: "subs",
        });
        const result = await call<SubResult>(CMD.checkSub, {
          kind: "author",
          targetPixivId: row.pixiv_user_id,
          full: true,
        });
        bumpData();
        setRefreshKey((key) => key + 1);
        setNotice(
          `已订阅「${row.name}」：新增 ${result.new_found} · 下载 ${result.downloaded}` +
            (result.failed > 0 ? ` · 失败 ${result.failed}` : ""),
        );
      } else {
        setNotice(`已订阅「${row.name}」，只下载新作品；可在订阅列表点「补全」下载历史`);
      }
      setTimeout(() => setNotice(null), 8000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setPendingSub(null);
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

  const favoritesSynced = (favoriteStatus?.count ?? 0) > 0;

  // 视图内切到该作者的作品，不再单独做作者信息头部
  if (current) {
    return (
      <ArtworkList
        extraQuery={{
          author_id: current.id,
          favorited_only: onlyCollected && favoritesSynced ? true : undefined,
        }}
        heading={current.name}
        headerExtra={
          <button
            type="button"
            className="tri-btn"
            data-active={onlyCollected}
            disabled={!favoritesSynced}
            title={
              favoritesSynced
                ? "只显示你在 Pixiv 收藏的作品"
                : "先点「同步收藏」后才能按 Pixiv 收藏筛选"
            }
            onClick={() => setOnlyCollected((value) => !value)}
          >
            仅看收藏
          </button>
        }
        onBack={() => setCurrent(null)}
      />
    );
  }

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
      {notice && <p className="mb-2 text-micro text-text-2">{notice}</p>}

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
          {favoriteStatus?.last_synced
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
                onToggleSubscribe={() => {
                  if (row.subscribed) {
                    void removeSubscription(row);
                  } else {
                    setSubscribeTarget(row);
                    setSubscribeBackfill(false);
                  }
                }}
                pending={pendingSub === row.pixiv_user_id}
              />
            ))}
          </div>
        )}
      </div>

      {q.trim() && !loading && rows.length === 0 && (
        <div className="mt-6 border-t border-line pt-4">
          <div className="flex items-center gap-2">
            <span className="text-micro text-text-3">Pixiv 上的作者</span>
            {remoteLoading && <span className="text-micro text-text-3">搜索中…</span>}
          </div>
          {remoteError ? (
            <p className="mt-2 text-micro text-text-2">
              远端搜索不可用：{remoteError}。本地结果不受影响。
            </p>
          ) : remoteLoading ? (
            <p className="mt-2 text-micro text-text-2">正在 Pixiv 上搜索「{q.trim()}」…</p>
          ) : remoteQuery === q.trim() && remoteAuthors.length > 0 ? (
            <div className="mt-2 grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]">
              {remoteAuthors.map((author) => {
                const subscribed = remoteSubscribed.has(author.pixiv_user_id);
                return (
                  <div
                    key={author.pixiv_user_id}
                    className="flex items-center gap-3 rounded-card border border-line bg-surface p-3"
                  >
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface-2 text-text-3">
                      <Users size={14} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-label text-text">{author.name}</p>
                      <p className="truncate text-micro text-text-3">
                        @{author.account} · Pixiv {author.pixiv_user_id}
                      </p>
                    </div>
                    <button
                      type="button"
                      className="btn shrink-0"
                      disabled={subscribed || remotePending !== null}
                      onClick={() => void subscribeRemote(author)}
                      title="只订阅新作品；要补全历史请到订阅列表点「补全」"
                    >
                      {subscribed
                        ? "已订阅"
                        : remotePending === author.pixiv_user_id
                          ? "订阅中…"
                          : "订阅"}
                    </button>
                  </div>
                );
              })}
            </div>
          ) : remoteQuery === q.trim() ? (
            <p className="mt-2 text-micro text-text-2">Pixiv 上也没有找到这个作者。</p>
          ) : null}
        </div>
      )}

      <Dialog
        open={subscribeTarget !== null}
        title="订阅作者"
        confirmLabel="订阅"
        busy={pendingSub !== null}
        onCancel={() => setSubscribeTarget(null)}
        onConfirm={confirmSubscribe}
      >
        <p className="text-body text-text">{subscribeTarget?.name}</p>
        <label className="mt-3 flex items-start gap-2 text-micro text-text-2">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={subscribeBackfill}
            onChange={(event) => setSubscribeBackfill(event.currentTarget.checked)}
          />
          <span>
            补全该作者已有作品
            <span className="mt-0.5 block text-micro text-text-3">
              勾选后会立即扫描并下载该作者的全部历史作品，可能很多；不勾选只收录订阅之后发布的新作品。
            </span>
          </span>
        </label>
      </Dialog>
    </div>
  );
}
