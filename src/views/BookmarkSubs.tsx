import { useEffect, useState } from "react";
import { Bookmark, Plus, Search } from "lucide-react";

import { Dialog } from "../components/dialog/Dialog";
import { EmptyState } from "../components/common/EmptyState";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { BookmarkSub, SubPreview, SubResult } from "../types/ipc";

function formatTime(raw: string | null): string {
  if (!raw) return "从未检查";
  return raw.replace("T", " ").slice(0, 16);
}

/** 订阅用户名：库里还没补上昵称时退回「未知用户 + id」，不显示空白。 */
function displayName(name: string | null | undefined, id: number): string {
  const trimmed = name?.trim();
  return trimmed ? trimmed : `未知用户 ${id}`;
}

/** 自动下载开关（`06-界面规格.md` §7） */
function Switch({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onToggle}
      className="inline-flex items-center gap-2"
      style={{ color: on ? "var(--color-accent)" : "var(--color-text-3)" }}
    >
      <span
        className="inline-block h-3 w-3 rounded-full border"
        style={{
          borderColor: "currentColor",
          background: on ? "currentColor" : "transparent",
        }}
      />
      <span className="text-micro">自动下载</span>
    </button>
  );
}

export function BookmarkSubs() {
  const q = useUi((state) => state.pageSearch.bookmarkSubs);
  const [rows, setRows] = useState<BookmarkSub[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const setScanTask = useUi((state) => state.setScanTask);
  const bumpData = useUi((state) => state.bumpData);
  // 保存 token 后后端会自动加一条「自己的私密收藏」订阅，这里跟着刷新
  const dataEpoch = useUi((state) => state.dataEpoch);

  const [addOpen, setAddOpen] = useState(false);
  const [userId, setUserId] = useState("");
  const [preview, setPreview] = useState<SubPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [removeTarget, setRemoveTarget] = useState<BookmarkSub | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState<number | null>(null);
  const [checkResult, setCheckResult] = useState<SubResult | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);
  /** 右键「检查」时的目标：用户确认后才走全量检查 */
  const [fullTarget, setFullTarget] = useState<BookmarkSub | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    call<BookmarkSub[]>(CMD.listBookmarkSubs)
      .then((data) => {
        if (cancelled) return;
        setRows(data);
        setError(null);
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
  }, [refreshKey, dataEpoch]);

  const doPreview = async () => {
    const id = Number(userId.trim());
    if (!Number.isInteger(id) || id <= 0) {
      setPreviewError("请输入正的 Pixiv 用户 ID");
      setPreview(null);
      return;
    }
    setPreviewError(null);
    try {
      // T08 之前后端返回本地占位（用户名未知、收藏数未知）
      const data = await call<SubPreview>(CMD.previewBookmarkSub, { pixivUserId: id });
      setPreview(data);
    } catch (reason: unknown) {
      setPreview(null);
      setPreviewError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const subscribe = async () => {
    const id = Number(userId.trim());
    setBusy(true);
    try {
      await call<BookmarkSub>(CMD.addBookmarkSub, { pixivUserId: id, checkNow: false });
      setAddOpen(false);
      setUserId("");
      setPreview(null);
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setPreviewError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const toggleAuto = async (row: BookmarkSub) => {
    try {
      await call<BookmarkSub>(CMD.toggleBookmarkSubAuto, { pixivUserId: row.pixiv_user_id });
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const unsubscribe = async () => {
    if (!removeTarget) return;
    setBusy(true);
    try {
      await call<boolean>(CMD.removeBookmarkSub, { pixivUserId: removeTarget.pixiv_user_id });
      setRemoveTarget(null);
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  // 检查单个订阅：拉公开收藏 → 下载新作品 → 入库/缩略图/元数据（后端一条龙）。
  // full=true 是右键触发的全量检查：从第一页翻到最后一页，不因库里已齐全而提前停止。
  const checkOne = async (row: BookmarkSub, full: boolean) => {
    setChecking(row.pixiv_user_id);
    setCheckError(null);
    setFullTarget(null);
    // 先立起浮窗：拉收藏页之前网络请求可能有几秒，用户要能随时取消
    setScanTask({
      current: 0,
      total: 0,
      imported: 0,
      message: full ? "准备全量检查订阅…" : "准备检查订阅…",
      kind: "subs",
    });
    try {
      const result = await call<SubResult>(CMD.checkBookmarkSub, {
        pixivUserId: row.pixiv_user_id,
        full,
      });
      setCheckResult(result);
      bumpData();
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setCheckError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setChecking(null);
      // 后端进度事件会打开右下角浮窗；这里必须收掉，否则残留的 scanTask
      // 会让后续扫描/同步都被判成「已有任务在运行」。
      setScanTask(null);
    }
  };

  // 订阅列表按昵称或 Pixiv 用户 ID 过滤
  const keyword = q.trim().toLowerCase();
  const visibleRows = keyword
    ? rows.filter((row) => {
        const name = displayName(row.name, row.pixiv_user_id).toLowerCase();
        return name.includes(keyword) || String(row.pixiv_user_id).includes(keyword);
      })
    : rows;

  return (
    <div className="px-6 py-4">
      <div className="mb-4 flex h-10 items-center">
        <span className="text-body text-text">订阅列表</span>
        <button
          type="button"
          className="btn ml-auto inline-flex items-center gap-2"
          onClick={() => setAddOpen(true)}
        >
          <Plus size={14} />
          订阅收藏
        </button>
      </div>

      {error ? (
        <EmptyState title="加载失败" hint={error} />
      ) : loading ? (
        <p className="py-8 text-center text-micro text-text-2">加载中…</p>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Bookmark}
          title="还没有订阅"
          hint="订阅他人的公开收藏后，新作品会自动下载归档"
        />
      ) : visibleRows.length === 0 ? (
        <EmptyState icon={Search} title="没有匹配的订阅" hint="换个关键词试试" />
      ) : (
        <div className="flex flex-col">
          {visibleRows.map((row) => (
            <div
              key={row.pixiv_user_id}
              className="flex h-[88px] items-center gap-4 border-b border-line"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate-1 text-body text-text">
                  {displayName(row.name, row.pixiv_user_id)}
                  {row.restrict === "private" && (
                    <span
                      className="ml-2 inline-block rounded border border-line px-1 align-middle text-micro text-text-3"
                      title="本账号自己的收藏：检查时会同时翻私密与公开两份列表"
                    >
                      私密
                    </span>
                  )}
                </p>
                <p className="mt-1 text-micro text-text-2">
                  上次检查：{formatTime(row.last_checked)}
                  {row.last_result ? ` · ${row.last_result}` : ""}
                </p>
              </div>

              <Switch on={row.auto_download} onToggle={() => toggleAuto(row)} />

              <button
                type="button"
                className="btn"
                disabled={checking !== null}
                title="左键检查新作品；右键全量检查（翻完所有收藏页，需确认）"
                onClick={() => checkOne(row, false)}
                onContextMenu={(event) => {
                  event.preventDefault();
                  setFullTarget(row);
                }}
              >
                {checking === row.pixiv_user_id ? "检查中…" : "检查"}
              </button>
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => setRemoveTarget(row)}
              >
                退订
              </button>
            </div>
          ))}
        </div>
      )}

      <Dialog
        open={addOpen}
        title="订阅收藏"
        confirmLabel="订阅"
        busy={busy || !preview}
        onCancel={() => {
          setAddOpen(false);
          setUserId("");
          setPreview(null);
          setPreviewError(null);
        }}
        onConfirm={subscribe}
      >
        <div className="flex items-end gap-2">
          <input
            className="line-input"
            placeholder="对方 Pixiv 用户 ID"
            value={userId}
            onChange={(event) => {
              setUserId(event.currentTarget.value);
              setPreview(null);
              setPreviewError(null);
            }}
            autoFocus
          />
          <button type="button" className="btn shrink-0" onClick={doPreview}>
            查询
          </button>
        </div>

        {preview && (
          <p className="mt-3 text-micro text-text-2">
            {preview.name.trim() ? `@${preview.name.trim()}` : `用户 ${preview.pixiv_user_id}`} ·
            收藏 {preview.total_bookmarks ?? "未知"} 件
          </p>
        )}
        {previewError && <p className="mt-2 text-micro text-danger">{previewError}</p>}
        {!preview && !previewError && (
          <p className="mt-2 text-micro text-text-3">先查询确认用户，再订阅。</p>
        )}
      </Dialog>

      <Dialog
        open={removeTarget !== null}
        title="退订"
        danger
        busy={busy}
        confirmLabel="退订"
        onCancel={() => setRemoveTarget(null)}
        onConfirm={unsubscribe}
      >
        <p>
          将取消对「
          {removeTarget ? displayName(removeTarget.name, removeTarget.pixiv_user_id) : ""}
          」的订阅。已下载的作品会保留。
        </p>
      </Dialog>

      <Dialog
        open={fullTarget !== null}
        title="全量检查"
        confirmLabel="全量检查"
        onCancel={() => setFullTarget(null)}
        onConfirm={() => {
          if (fullTarget) void checkOne(fullTarget, true);
        }}
      >
        <p className="text-body text-text">
          {fullTarget ? displayName(fullTarget.name, fullTarget.pixiv_user_id) : ""}
        </p>
        <p className="mt-2 text-micro text-text-2">
          会从收藏第一页翻到最后一页（最多 100 页），不因库里已齐全而提前停止；已经齐全的作品不会重新下载。收藏量大时耗时较长，过程中可以取消。
        </p>
      </Dialog>

      <Dialog
        open={checkResult !== null}
        title={checkResult?.cancelled ? "检查已取消" : "检查完成"}
        confirmLabel="知道了"
        onCancel={() => setCheckResult(null)}
        onConfirm={() => setCheckResult(null)}
      >
        <p className="text-body text-text">
          {displayName(checkResult?.name, checkResult?.pixiv_user_id ?? 0)}
        </p>
        <p className="mt-2 text-micro text-text-2">
          新增 {checkResult?.new_found ?? 0} · 下载 {checkResult?.downloaded ?? 0} · 失败{" "}
          {checkResult?.failed ?? 0} · 翻页 {checkResult?.pages_scanned ?? 0}
          {checkResult?.caught_up ? " · 已追平" : ""}
          {checkResult?.cancelled ? " · 已取消（剩余作品未处理）" : ""}
        </p>
        {checkResult?.error && (
          <p className="mt-2 text-micro text-danger">{checkResult.error}</p>
        )}
      </Dialog>

      <Dialog
        open={checkError !== null}
        title="检查失败"
        confirmLabel="知道了"
        onCancel={() => setCheckError(null)}
        onConfirm={() => setCheckError(null)}
      >
        <p className="text-micro text-danger">{checkError}</p>
      </Dialog>
    </div>
  );
}