import { useEffect, useState } from "react";
import { Bell, Plus } from "lucide-react";

import { Dialog } from "../components/dialog/Dialog";
import { EmptyState } from "../components/common/EmptyState";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { Sub, SubKind, SubPreview, SubResult } from "../types/ipc";

type KindFilter = "all" | SubKind;

const KIND_LABEL: Record<SubKind, string> = {
  bookmark: "Pixiv 收藏",
  author: "作者",
};

const KIND_HINT: Record<SubKind, string> = {
  bookmark: "检查时拉取对方的公开 Pixiv 收藏，发现新作品就自动下载。",
  author: "检查时拉取该作者的作品列表，新作品自动下载。",
};

function formatTime(raw: string | null): string {
  if (!raw) return "从未检查";
  return raw.replace("T", " ").slice(0, 16);
}

/** 订阅用户名：库里还没补上昵称时退回「未知用户 + id」，不显示空白。 */
function displayName(name: string | null | undefined, id: number): string {
  const trimmed = name?.trim();
  return trimmed ? trimmed : `未知用户 ${id}`;
}

/** 订阅类型角标：收藏 / 作者（复用全局 `.badge` 的尺寸与字重） */
function KindBadge({ kind }: { kind: SubKind }) {
  return (
    <span className="badge sub-kind shrink-0" data-kind={kind}>
      {KIND_LABEL[kind]}
    </span>
  );
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

export function Subs() {
  const [rows, setRows] = useState<Sub[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const setScanTask = useUi((state) => state.setScanTask);
  const bumpData = useUi((state) => state.bumpData);

  const [addOpen, setAddOpen] = useState(false);
  const [kind, setKind] = useState<SubKind>("bookmark");
  const [targetId, setTargetId] = useState("");
  const [backfill, setBackfill] = useState(false);
  const [preview, setPreview] = useState<SubPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Sub | null>(null);
  const [backfillTarget, setBackfillTarget] = useState<Sub | null>(null);
  const [busy, setBusy] = useState(false);
  // 正在检查的订阅（`kind:target`），同一时间只允许一个
  const [checking, setChecking] = useState<string | null>(null);
  const [checkResult, setCheckResult] = useState<SubResult | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    call<Sub[]>(CMD.listSubs)
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
  }, [refreshKey]);

  const visible = rows.filter((row) => kindFilter === "all" || row.kind === kindFilter);

  const closeAdd = () => {
    setAddOpen(false);
    setTargetId("");
    setBackfill(false);
    setPreview(null);
    setPreviewError(null);
  };

  const pickKind = (next: SubKind) => {
    setKind(next);
    // 类型变了，上一次的查询结果不再适用
    setPreview(null);
    setPreviewError(null);
  };

  const doPreview = async () => {
    const id = Number(targetId.trim());
    if (!Number.isInteger(id) || id <= 0) {
      setPreviewError("请输入正的 Pixiv 用户 ID");
      setPreview(null);
      return;
    }
    setPreviewError(null);
    try {
      // 后端只补昵称；查不到（无 token / 离线）也不阻塞订阅
      const data = await call<SubPreview>(CMD.previewSub, { kind, targetPixivId: id });
      setPreview(data);
    } catch (reason: unknown) {
      setPreview(null);
      setPreviewError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const subscribe = async () => {
    if (!preview) {
      setPreviewError("先查询确认用户，再订阅。");
      return;
    }
    const id = Number(targetId.trim());
    setBusy(true);
    try {
      await call<Sub>(CMD.addSub, {
        kind,
        targetPixivId: id,
        backfill: kind === "author" ? backfill : undefined,
      });
      closeAdd();
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setPreviewError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const toggleAuto = async (row: Sub) => {
    try {
      await call<Sub>(CMD.toggleSubAuto, {
        kind: row.kind,
        targetPixivId: row.target_pixiv_id,
      });
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const unsubscribe = async () => {
    if (!removeTarget) return;
    setBusy(true);
    try {
      await call<boolean>(CMD.removeSub, {
        kind: removeTarget.kind,
        targetPixivId: removeTarget.target_pixiv_id,
      });
      setRemoveTarget(null);
      setRefreshKey((key) => key + 1);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  // 检查单个订阅：拉列表 → 下载新作品 → 入库/缩略图/元数据（后端一条龙）
  const checkOne = async (row: Sub) => {
    const key = `${row.kind}:${row.target_pixiv_id}`;
    setChecking(key);
    setCheckError(null);
    // 先立起浮窗：拉列表之前网络请求可能有几秒，用户要能随时取消
    setScanTask({
      current: 0,
      total: 0,
      imported: 0,
      message: "准备检查订阅…",
      kind: "subs",
    });
    try {
      const result = await call<SubResult>(CMD.checkSub, {
        kind: row.kind,
        targetPixivId: row.target_pixiv_id,
        full: false,
      });
      setCheckResult(result);
      bumpData();
      setRefreshKey((k) => k + 1);
    } catch (reason: unknown) {
      setCheckError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setChecking(null);
      // 后端进度事件会打开右下角浮窗；这里必须收掉，否则残留的 scanTask
      // 会让后续扫描/同步都被判成「已有任务在运行」。
      setScanTask(null);
    }
  };

  // 补全历史：把起点重置为 0，再走一次普通检查（会翻到列表末尾）
  const startBackfill = async () => {
    if (!backfillTarget) return;
    const row = backfillTarget;
    setBusy(true);
    try {
      await call<Sub>(CMD.resetSubBaseline, {
        kind: row.kind,
        targetPixivId: row.target_pixiv_id,
      });
      setBackfillTarget(null);
      setRefreshKey((key) => key + 1);
      await checkOne(row);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="px-6 py-4">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="text-body text-text">订阅列表</span>
        <span className="ml-2 text-micro text-text-3">订阅类型</span>
        {(["all", "bookmark", "author"] as KindFilter[]).map((value) => (
          <button
            key={value}
            type="button"
            className="tri-btn"
            data-active={kindFilter === value}
            onClick={() => setKindFilter(value)}
          >
            {value === "all" ? "全部" : KIND_LABEL[value]}
          </button>
        ))}
        <span className="text-micro text-text-3">共 {visible.length} 条</span>
        <button
          type="button"
          className="btn ml-auto inline-flex items-center gap-2"
          onClick={() => setAddOpen(true)}
        >
          <Plus size={14} />
          添加订阅
        </button>
      </div>

      {error ? (
        <EmptyState title="加载失败" hint={error} />
      ) : loading ? (
        <p className="py-8 text-center text-micro text-text-2">加载中…</p>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Bell}
          title="还没有订阅"
          hint="在作者列表点「订阅」，或在这里订阅别人的公开 Pixiv 收藏"
        />
      ) : visible.length === 0 ? (
        <p className="py-8 text-center text-micro text-text-2">没有该类型的订阅</p>
      ) : (
        <div className="flex flex-col">
          {visible.map((row) => {
            const key = `${row.kind}:${row.target_pixiv_id}`;
            return (
              <div key={key} className="flex h-[88px] items-center gap-4 border-b border-line">
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 text-body text-text">
                    <KindBadge kind={row.kind} />
                    <span className="truncate-1">
                      {displayName(row.name, row.target_pixiv_id)}
                    </span>
                    {row.kind === "author" && row.baseline_pid === 0 && (
                      <span className="shrink-0 text-micro text-text-3">补全历史</span>
                    )}
                  </p>
                  <p className="mt-1 text-micro text-text-2">
                    上次检查：{formatTime(row.last_checked)}
                    {row.last_result ? ` · ${row.last_result}` : ""}
                  </p>
                </div>

                <Switch on={row.auto_download} onToggle={() => toggleAuto(row)} />

                {row.kind === "author" && (
                  <button
                    type="button"
                    className="btn"
                    disabled={checking !== null}
                    title="重新扫描该作者的全部历史作品（已入库的会跳过）"
                    onClick={() => setBackfillTarget(row)}
                  >
                    补全
                  </button>
                )}

                <button
                  type="button"
                  className="btn"
                  disabled={checking !== null}
                  title={
                    row.kind === "author"
                      ? "检查该作者的作品并下载新作品"
                      : "检查该用户的公开 Pixiv 收藏并下载新作品"
                  }
                  onClick={() => checkOne(row)}
                >
                  {checking === key ? "检查中…" : "检查"}
                </button>
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => setRemoveTarget(row)}
                >
                  退订
                </button>
              </div>
            );
          })}
        </div>
      )}

      <Dialog
        open={addOpen}
        title="添加订阅"
        confirmLabel="订阅"
        busy={busy}
        confirmDisabled={!preview}
        onCancel={closeAdd}
        onConfirm={subscribe}
      >
        <p className="text-micro text-text-3">订阅类型</p>
        <div className="mt-2 flex gap-2">
          {(["bookmark", "author"] as SubKind[]).map((value) => (
            <button
              key={value}
              type="button"
              className="tri-btn"
              data-active={kind === value}
              onClick={() => pickKind(value)}
            >
              {KIND_LABEL[value]}
            </button>
          ))}
        </div>
        <p className="mt-2 text-micro text-text-3">{KIND_HINT[kind]}</p>

        <div className="mt-4 flex items-end gap-2">
          <input
            className="line-input"
            placeholder={kind === "author" ? "作者 Pixiv 用户 ID" : "对方 Pixiv 用户 ID"}
            value={targetId}
            onChange={(event) => {
              setTargetId(event.currentTarget.value);
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
            {preview.name.trim() ? `@${preview.name.trim()}` : `用户 ${preview.target_pixiv_id}`}
            {preview.kind === "bookmark" ? ` · Pixiv 收藏 ${preview.total_bookmarks ?? "未知"} 件` : ""}
          </p>
        )}
        {previewError && <p className="mt-2 text-micro text-danger">{previewError}</p>}
        {!preview && !previewError && (
          <p className="mt-2 text-micro text-text-3">先查询确认用户，再订阅。</p>
        )}

        {kind === "author" && (
          <label className="mt-3 flex items-start gap-2 text-micro text-text-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={backfill}
              onChange={(event) => setBackfill(event.currentTarget.checked)}
            />
            <span>
              补全该作者已有作品
              <span className="mt-0.5 block text-micro text-text-3">
                首次检查会下载该作者的全部作品，可能很多；不勾选则只收录订阅之后发布的新作品。
              </span>
            </span>
          </label>
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
          {removeTarget ? displayName(removeTarget.name, removeTarget.target_pixiv_id) : ""}
          」的{removeTarget?.kind === "author" ? "作者" : "Pixiv 收藏"}订阅。已下载的作品会保留。
        </p>
      </Dialog>

      <Dialog
        open={backfillTarget !== null}
        title="补全历史作品"
        confirmLabel="补全"
        busy={busy}
        onCancel={() => setBackfillTarget(null)}
        onConfirm={startBackfill}
      >
        <p>
          将重新扫描「
          {backfillTarget
            ? displayName(backfillTarget.name, backfillTarget.target_pixiv_id)
            : ""}
          」的全部历史作品，已入库的会自动跳过。作品多时可能比较久，可以在右下角取消。
        </p>
      </Dialog>

      <Dialog
        open={checkResult !== null}
        title={checkResult?.cancelled ? "检查已取消" : "检查完成"}
        confirmLabel="知道了"
        onCancel={() => setCheckResult(null)}
        onConfirm={() => setCheckResult(null)}
      >
        <p className="flex items-center gap-2 text-body text-text">
          <KindBadge kind={checkResult?.kind ?? "bookmark"} />
          {displayName(checkResult?.name, checkResult?.pixiv_user_id ?? 0)}
        </p>
        <p className="mt-2 text-micro text-text-2">
          新增 {checkResult?.new_found ?? 0}
          {checkResult?.repaired ? ` · 补缺 ${checkResult.repaired}` : ""} · 下载{" "}
          {checkResult?.downloaded ?? 0} · 失败 {checkResult?.failed ?? 0} · 翻页{" "}
          {checkResult?.pages_scanned ?? 0}
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
