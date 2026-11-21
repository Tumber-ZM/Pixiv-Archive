import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, Images, Loader2 } from "lucide-react";

import { CMD, call, isAppError } from "../../lib/ipc";
import { TRI_LABELS, useUi } from "../../stores/ui";
import type { AppError, ArtworkCard, BatchResult, ListQuery, Page, RemoteIllust, SyncReport } from "../../types/ipc";
import { BatchBar } from "../batch/BatchBar";
import { ContextMenu } from "../common/ContextMenu";
import { EmptyState } from "../common/EmptyState";
import { Dialog } from "../dialog/Dialog";
import { FavoritePicker } from "../viewer/FavoritePicker";
import { Viewer } from "../viewer/Viewer";
import { SkeletonGrid } from "./SkeletonGrid";
import { WallGrid } from "./WallGrid";

const PAGE_SIZE = 60;
const SEARCH_DEBOUNCE_MS = 300;

interface ArtworkListProps {
  /** 固定筛选：作者页带 author_id、标签页带 tag、本地收藏带 favorite_id */
  extraQuery?: Partial<ListQuery>;
  /** 子视图的标题与返回入口 */
  heading?: string;
  /** 标题右边额外放的筛选按钮（例如作者页的「仅看收藏」） */
  headerExtra?: ReactNode;
  onBack?: () => void;
  /** 列表变化后通知外层（例如删空了要回列表） */
  onChanged?: () => void;
  /** 本地搜不到时去 Pixiv 搜作品（只给画廊用） */
  remoteSearch?: boolean;
  /** 忽略顶栏搜索词（标签页的图片区只按勾选标签过滤） */
  ignoreQuery?: boolean;
}

/**
 * 作品列表本体，所有作品视图共用。
 *
 * 无限滚动：每批 60 条，底部哨兵进入视口就取下一批并追加。
 * 筛选条件变化会清空累积结果、退回第 1 批——否则新旧条件的结果会混在一起。
 */
export function ArtworkList({ extraQuery, heading, headerExtra, onBack, onChanged, remoteSearch, ignoreQuery }: ArtworkListProps) {
  const q = useUi((state) => state.q);
  const sort = useUi((state) => state.sort);
  const order = useUi((state) => state.order);
  const r18 = useUi((state) => state.r18);
  const ai = useUi((state) => state.ai);
  const gif = useUi((state) => state.gif);
  const deletedOnly = useUi((state) => state.deletedOnly);
  const layout = useUi((state) => state.layout);
  const clearFilters = useUi((state) => state.clearFilters);
  const shuffleToken = useUi((state) => state.shuffleToken);
  const cycleTheme = useUi((state) => state.cycleTheme);
  const setSettingsOpen = useUi((state) => state.setSettingsOpen);
  const bumpData = useUi((state) => state.bumpData);
  const setScanTask = useUi((state) => state.setScanTask);
  const dataEpoch = useUi((state) => state.dataEpoch);

  const [debouncedQ, setDebouncedQ] = useState(q);
  const [items, setItems] = useState<ArtworkCard[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<AppError | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  // 随机排序的种子只生成一次，翻页时保持不变，否则每页顺序都会变
  const [seed] = useState(() => Math.floor(Math.random() * 999_999) + 1);
  // 「随机」按钮触发的一次性乱序：和排序下拉框互不影响
  const [shuffle, setShuffle] = useState<{ token: number; seed: number } | null>(null);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [viewer, setViewer] = useState<{ ids: number[]; index: number } | null>(null);
  const [batchFavoriteOpen, setBatchFavoriteOpen] = useState(false);
  const [favoriteIds, setFavoriteIds] = useState<number[]>([]);
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number; card: ArtworkCard } | null>(null);
  const [blankMenu, setBlankMenu] = useState<{ x: number; y: number } | null>(null);
  const [syncNotice, setSyncNotice] = useState<string | null>(null);
  // 远端作品搜索：本地为空时才请求，失败只提示不弹错
  const [remoteItems, setRemoteItems] = useState<RemoteIllust[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [remoteFor, setRemoteFor] = useState("");
  const [remoteBusy, setRemoteBusy] = useState<number | null>(null);
  const [remoteDone, setRemoteDone] = useState<Set<number>>(new Set());
  const [remoteNotice, setRemoteNotice] = useState<string | null>(null);

  const sentinelRef = useRef<HTMLDivElement>(null);
  // 防止同一批被重复请求（快速滚动时哨兵会连续触发）
  const inFlight = useRef(false);

  // extraQuery 每次渲染都是新对象，用序列化结果做依赖
  const extraKey = JSON.stringify(extraQuery ?? {});

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(q), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [q]);

  // 顶栏「随机」按钮：只换一次乱序种子，不改排序下拉框
  useEffect(() => {
    if (shuffleToken === 0) return;
    setShuffle({
      token: shuffleToken,
      seed: Math.floor(Math.random() * 999_999) + 1,
    });
  }, [shuffleToken]);

  // 用户改了排序 / 筛选 / 搜索词：退出一次性乱序，回到所选排序
  useEffect(() => {
    setShuffle(null);
  }, [sort, order, r18, ai, gif, deletedOnly, debouncedQ]);

  // 本地没有结果时，非阻塞地补一次 Pixiv 作品搜索
  useEffect(() => {
    if (!remoteSearch || ignoreQuery || !debouncedQ.trim() || loading || items.length > 0) {
      setRemoteItems([]);
      setRemoteError(null);
      setRemoteLoading(false);
      setRemoteFor("");
      return undefined;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setRemoteLoading(true);
      setRemoteError(null);
      call<RemoteIllust[]>(CMD.searchPixivIllusts, {
        word: debouncedQ.trim(),
        sort: "date_desc",
      })
        .then((list) => {
          if (cancelled) return;
          setRemoteItems(list);
          setRemoteFor(debouncedQ.trim());
        })
        .catch((reason: unknown) => {
          if (cancelled) return;
          setRemoteError(isAppError(reason) ? reason.message : String(reason));
          setRemoteItems([]);
          setRemoteFor(debouncedQ.trim());
        })
        .finally(() => {
          if (!cancelled) setRemoteLoading(false);
        });
    }, 450);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [remoteSearch, ignoreQuery, debouncedQ, loading, items.length]);

  // 条件变了：清空累积结果，退回第 1 批
  useEffect(() => {
    setItems([]);
    setTotal(0);
    setTotalPages(1);
    setPage(1);
  }, [debouncedQ, sort, order, r18, ai, gif, deletedOnly, extraKey, refreshKey, dataEpoch, shuffle?.token]);

  useEffect(() => {
    let cancelled = false;
    const effectiveSort = shuffle ? "random" : sort;
    const effectiveOrder = shuffle ? "desc" : order;

    const query: ListQuery = {
      page,
      page_size: PAGE_SIZE,
      q: ignoreQuery ? undefined : debouncedQ.trim() || undefined,
      sort: effectiveSort,
      order: effectiveOrder,
      r18,
      ai,
      gif,
      deleted_only: deletedOnly,
      seed: shuffle ? shuffle.seed : sort === "random" ? seed : undefined,
      ...(extraQuery ?? {}),
    };

    const command = query.q ? CMD.searchArtworks : CMD.listArtworks;

    if (page === 1) setLoading(true);
    else setLoadingMore(true);

    call<Page<ArtworkCard>>(command, { query })
      .then((result) => {
        if (cancelled) return;
        setItems((previous) => {
          // 删除后重取时可能出现重复 id，按 id 去重
          const seen = new Set(page === 1 ? [] : previous.map((item) => item.id));
          const fresh = result.items.filter((item) => !seen.has(item.id));
          return page === 1 ? fresh : [...previous, ...fresh];
        });
        setTotal(result.total);
        setTotalPages(result.total_pages);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(isAppError(reason) ? reason : { code: "INTERNAL", message: String(reason) });
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
    // extraQuery 通过 extraKey 参与依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, debouncedQ, sort, order, r18, ai, gif, deletedOnly, seed, refreshKey, extraKey, dataEpoch, shuffle?.seed]);

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

  // Ctrl+A 全选当前已加载的作品（输入框里不抢键）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (viewer) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.preventDefault();
        setSelected(new Set(items.map((card) => card.id)));
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [items, viewer]);

  const keyword = debouncedQ.trim();
  const filtered =
    Boolean(keyword) || r18 !== "all" || ai !== "all" || gif !== "all" || deletedOnly;

  const summary = [
    keyword ? `关键词「${keyword}」` : null,
    r18 !== "all" ? `R18 ${TRI_LABELS[r18]}` : null,
    ai !== "all" ? `AI ${TRI_LABELS[ai]}` : null,
    gif !== "all" ? `GIF ${TRI_LABELS[gif]}` : null,
    deletedOnly ? "仅已隐藏" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const toggleSelect = (card: ArtworkCard) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(card.id)) next.delete(card.id);
      else next.add(card.id);
      return next;
    });
  };

  const openViewer = (card: ArtworkCard) => {
    const index = items.findIndex((item) => item.id === card.id);
    setViewer({ ids: items.map((item) => item.id), index: index < 0 ? 0 : index });
  };

  const refresh = useCallback(() => {
    setItems([]);
    setPage(1);
    setRefreshKey((key) => key + 1);
    bumpData();
    onChanged?.();
  }, [onChanged, bumpData]);

  // 重新生成缩略图（覆盖已有文件）
  const regenerateThumbnailsFor = async (ids: number[]) => {
    if (ids.length === 0) return;
    try {
      await call(CMD.regenerateThumbnails, { artworkIds: ids });
      setSelected(new Set());
      refresh();
    } catch (reason: unknown) {
      setSyncNotice(isAppError(reason) ? reason.message : String(reason));
    }
  };

  // 强制重新同步这些作品的元数据；服务器返回已删除时只打标记、保留元数据
  const syncMetadataFor = async (ids: number[]) => {
    if (ids.length === 0) return;
    setScanTask({ current: 0, total: ids.length, imported: 0, message: "同步元数据…", kind: "sync" });
    try {
      const report = await call<SyncReport>(CMD.syncArtworks, { artworkIds: ids });
      setSyncNotice(`成功 ${report.synced} · 已隐藏 ${report.deleted} · 失败 ${report.failed}`);
      setSelected(new Set());
      refresh();
    } catch (reason: unknown) {
      setSyncNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setScanTask(null);
    }
  };

  // 查看器滚到最后一件时继续加载，加载完由 effect 把新 id 同步过去
  const loadMoreForViewer = useCallback(() => {
    if (inFlight.current) return;
    if (loading || page >= totalPages) return;
    inFlight.current = true;
    setPage((current) => current + 1);
  }, [loading, page, totalPages]);

  // 累加后把新的作品 id 补进查看器的导航列表，保持当前 index 不动
  useEffect(() => {
    if (!viewer) return;
    const ids = items.map((item) => item.id);
    setViewer((current) => {
      if (!current) return current;
      if (current.ids.length === ids.length) return current;
      return { ...current, ids };
    });
  }, [items]);

  const runBatchDelete = async () => {
    setBusy(true);
    try {
      await call<BatchResult>(CMD.batchDeleteArtworks, {
        artworkIds: [...selected],
        deleteFiles,
      });
      setBatchDeleteOpen(false);
      setSelected(new Set());
      refresh();
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason : { code: "INTERNAL", message: String(reason) });
    } finally {
      setBusy(false);
    }
  };

  const downloadRemote = async (item: RemoteIllust) => {
    if (remoteBusy !== null) return;
    setRemoteBusy(item.pixiv_id);
    setRemoteNotice(null);
    try {
      await call<number>(CMD.downloadPixivIllust, { pixivId: item.pixiv_id });
      setRemoteDone((prev) => new Set(prev).add(item.pixiv_id));
      setRemoteNotice(`「${item.title || item.pixiv_id}」已下载入库`);
      bumpData();
    } catch (reason: unknown) {
      setRemoteNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setRemoteBusy(null);
    }
  };

  if (error) {
    return <EmptyState title="加载失败" hint={error.message} />;
  }

  const showBar = Boolean(onBack || heading || filtered);
  const hasMore = page < totalPages;

  return (
    <div
      className="px-6 py-4"
      onContextMenu={(event) => {
        const target = event.target as HTMLElement | null;
        if (target?.closest(".wall-card, .wall-tile")) return;
        event.preventDefault();
        setBlankMenu({ x: event.clientX, y: event.clientY });
      }}
    >
      {showBar && (
        <div className="mb-4 flex h-10 items-center gap-3">
          {onBack && (
            <button
              type="button"
              className="btn inline-flex items-center gap-2"
              onClick={onBack}
            >
              <ArrowLeft size={14} />
              返回
            </button>
          )}
          {heading && <span className="text-body text-text">{heading}</span>}
          {headerExtra}
          {filtered && <span className="text-micro text-text-2">{summary}</span>}
          {filtered && (
            <button type="button" className="quiet-link" onClick={clearFilters}>
              清除筛选
            </button>
          )}
        </div>
      )}

      {loading && items.length === 0 ? (
        <SkeletonGrid />
      ) : items.length === 0 ? (
        filtered ? (
          <div>
            <EmptyState title="没有符合条件的作品" hint="换个关键词，或清除筛选条件">
              <button type="button" className="btn mt-2" onClick={clearFilters}>
                清除筛选
              </button>
            </EmptyState>
            {remoteSearch && debouncedQ.trim() && (
              <div className="mt-6 border-t border-line pt-4">
                <div className="flex items-center gap-2">
                  <span className="text-micro text-text-3">Pixiv 上的作品</span>
                  {remoteLoading && <span className="text-micro text-text-3">搜索中…</span>}
                </div>
                {remoteError ? (
                  <p className="mt-2 text-micro text-text-2">
                    远端搜索不可用：{remoteError}。本地结果不受影响。
                  </p>
                ) : remoteLoading ? (
                  <p className="mt-2 text-micro text-text-2">
                    正在 Pixiv 上搜索「{debouncedQ.trim()}」…
                  </p>
                ) : remoteFor === debouncedQ.trim() && remoteItems.length > 0 ? (
                  <div className="mt-2 flex flex-col gap-1">
                    {remoteItems.map((item) => (
                      <div
                        key={item.pixiv_id}
                        className="flex items-center gap-3 rounded-btn border border-line bg-surface px-3 py-2"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-label text-text">
                            {item.title || "(无标题)"}
                          </p>
                          <p className="truncate text-micro text-text-3">
                            {item.author_name} · Pixiv {item.pixiv_id} · {item.page_count} 页
                          </p>
                        </div>
                        <button
                          type="button"
                          className="btn shrink-0"
                          disabled={remoteBusy !== null || remoteDone.has(item.pixiv_id)}
                          onClick={() => void downloadRemote(item)}
                        >
                          {remoteDone.has(item.pixiv_id)
                            ? "已入库"
                            : remoteBusy === item.pixiv_id
                              ? "下载中…"
                              : "下载"}
                        </button>
                      </div>
                    ))}
                  </div>
                ) : remoteFor === debouncedQ.trim() ? (
                  <p className="mt-2 text-micro text-text-2">Pixiv 上也没有找到作品。</p>
                ) : null}
                {remoteNotice && (
                  <p className="mt-2 text-micro text-text-2">{remoteNotice}</p>
                )}
              </div>
            )}
          </div>
        ) : (
          <EmptyState
            icon={Images}
            title="还没有作品"
            hint="先扫描本地图片目录，或导入旧项目数据"
          />
        )
      ) : (
        <>
          <WallGrid
            items={items}
            layout={layout}
            selected={selected}
            onOpen={openViewer}
            onToggle={toggleSelect}
            onFavorite={(card) => {
              setFavoriteIds([card.id]);
              setBatchFavoriteOpen(true);
            }}
            onDelete={(card) => {
              setSelected(new Set([card.id]));
              setBatchDeleteOpen(true);
            }}
            onContextMenu={(card, event) => {
              event.preventDefault();
              setMenu({ x: event.clientX, y: event.clientY, card });
            }}
          />

          {/* 哨兵：进入视口就取下一批 */}
          <div ref={sentinelRef} className="h-px w-full" aria-hidden="true" />

          <div className="flex h-14 items-center justify-center gap-2 text-micro text-text-2">
            {loadingMore ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                加载中…
              </>
            ) : hasMore ? (
              /* 哨兵还没触发时的兜底入口，键盘与读屏用户也能继续 */
              <button type="button" className="quiet-link" onClick={loadMoreForViewer}>
                加载更多
              </button>
            ) : (
              <span className="text-text-3">已全部加载 · 共 {total} 件</span>
            )}
          </div>
        </>
      )}

      <BatchBar
        count={selected.size}
        onSelectAll={() =>
          setSelected((current) => {
            const next = new Set(current);
            for (const card of items) next.add(card.id);
            return next;
          })
        }
        onClear={() => setSelected(new Set())}
        onFavorite={() => {
          setFavoriteIds([...selected]);
          setBatchFavoriteOpen(true);
        }}
        onDelete={() => setBatchDeleteOpen(true)}
        onRegenerateThumbnails={() => regenerateThumbnailsFor([...selected])}
        onSyncMetadata={() => syncMetadataFor([...selected])}
      />

      {viewer && (
        <Viewer
          artworkIds={viewer.ids}
          initialIndex={viewer.index}
          hasMore={hasMore}
          onLoadMore={loadMoreForViewer}
          onClose={() => setViewer(null)}
          onChanged={refresh}
        />
      )}

      {batchFavoriteOpen && (
        <FavoritePicker
          artworkIds={favoriteIds}
          onClose={() => setBatchFavoriteOpen(false)}
          onSaved={() => {
            setBatchFavoriteOpen(false);
            refresh();
          }}
        />
      )}

      {blankMenu && (
        <ContextMenu
          x={blankMenu.x}
          y={blankMenu.y}
          onClose={() => setBlankMenu(null)}
          items={[
            { label: "切换主题", onSelect: cycleTheme },
            { label: "打开设置", onSelect: () => setSettingsOpen(true) },
          ]}
        />
      )}

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
          items={[
            { label: "查看", onSelect: () => openViewer(menu.card) },
            {
              label: "在文件夹中定位",
              disabled: !menu.card.source_path,
              onSelect: () => {
                const path = menu.card.source_path;
                if (!path) return;
                call(CMD.openInFolder, { path }).catch(() => undefined);
              },
            },
            {
              label: "收藏到本地",
              onSelect: () => {
                setFavoriteIds([menu.card.id]);
                setBatchFavoriteOpen(true);
              },
            },
            {
              label: "复制 Pixiv ID",
              disabled: menu.card.pixiv_id < 0,
              onSelect: () => {
                navigator.clipboard?.writeText(String(menu.card.pixiv_id)).catch(() => undefined);
              },
            },
            {
              label: "重新生成缩略图",
              onSelect: () => regenerateThumbnailsFor([menu.card.id]),
            },
            {
              label: "重新同步元数据",
              disabled: menu.card.pixiv_id < 0,
              onSelect: () => syncMetadataFor([menu.card.id]),
            },
            {
              label: "删除",
              danger: true,
              separatorBefore: true,
              onSelect: () => {
                setSelected(new Set([menu.card.id]));
                setBatchDeleteOpen(true);
              },
            },
          ]}
        />
      )}

      <Dialog
        open={batchDeleteOpen}
        title={`删除 ${selected.size} 件作品`}
        danger
        busy={busy}
        confirmLabel="删除"
        onCancel={() => setBatchDeleteOpen(false)}
        onConfirm={runBatchDelete}
      >
        <p>将从库中移除选中的 {selected.size} 件作品。</p>
        <label className="mt-3 flex items-center gap-2">
          <input
            type="checkbox"
            checked={deleteFiles}
            onChange={(event) => setDeleteFiles(event.currentTarget.checked)}
          />
          <span>同时删除本地源文件</span>
        </label>
      </Dialog>

      <Dialog
        open={syncNotice !== null}
        title="重新同步元数据"
        confirmLabel="知道了"
        onCancel={() => setSyncNotice(null)}
        onConfirm={() => setSyncNotice(null)}
      >
        <p className="text-micro text-text-2">{syncNotice}</p>
      </Dialog>
    </div>
  );
}