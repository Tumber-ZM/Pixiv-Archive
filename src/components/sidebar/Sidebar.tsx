import { useEffect, useState } from "react";

import {
  Bell,
  Bookmark,
  FolderOpen,
  Heart,
  Images,
  Lock,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  ScanLine,
  Settings,
  Sun,
  Tag,
  Users,
  type LucideIcon,
} from "lucide-react";

import { Dialog } from "../dialog/Dialog";
import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi, type Theme, type ViewKey } from "../../stores/ui";
import type { Stats, SubResult, SyncReport } from "../../types/ipc";

const NAV: ReadonlyArray<{ key: ViewKey; label: string; Icon: LucideIcon }> = [
  { key: "gallery", label: "画廊", Icon: Images },
  { key: "authors", label: "作者列表", Icon: Users },
  { key: "tags", label: "标签列表", Icon: Tag },
  { key: "favorites", label: "本地收藏", Icon: Heart },
  { key: "subs", label: "订阅列表", Icon: Bookmark },
  { key: "local", label: "本地作品", Icon: FolderOpen },
];

/** 操作项不是导航，不显示激活态（`06-界面规格.md` §1）。 */
const ACTIONS: ReadonlyArray<{
  label: string;
  Icon: LucideIcon;
  action?: "scan" | "sync" | "subs";
}> = [
  { label: "同步元数据", Icon: RefreshCw, action: "sync" },
  { label: "扫描图片", Icon: ScanLine, action: "scan" },
  { label: "订阅更新", Icon: Bell, action: "subs" },
];

const THEME_META: Record<Theme, { label: string; Icon: LucideIcon }> = {
  light: { label: "浅色", Icon: Sun },
  dark: { label: "深色", Icon: Moon },
  system: { label: "跟随系统", Icon: Monitor },
};

export function Sidebar() {
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const view = useUi((s) => s.view);
  const theme = useUi((s) => s.theme);
  const safeMode = useUi((s) => s.safeMode);
  const setView = useUi((s) => s.setView);
  const toggleSidebar = useUi((s) => s.toggleSidebar);
  const cycleTheme = useUi((s) => s.cycleTheme);
  const toggleSafeMode = useUi((s) => s.toggleSafeMode);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  const setScanOpen = useUi((s) => s.setScanOpen);
  const statsEpoch = useUi((s) => s.statsEpoch);
  const scanResult = useUi((s) => s.scanResult);

  // 统计只算 Pixiv 作品：本地文件（pixiv_id < 0）后端 SQL 里已经排除
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    let cancelled = false;
    call<Stats>(CMD.getStats)
      .then((value) => {
        if (!cancelled) setStats(value);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [view, statsEpoch, scanResult]);

  const setScanTask = useUi((s) => s.setScanTask);
  const scanTask = useUi((s) => s.scanTask);
  const bumpData = useUi((s) => s.bumpData);

  const [syncing, setSyncing] = useState(false);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);

  // 同步元数据：默认只补「没同步过 / 上次失败」的作品；Shift+点击走全量重同步
  const runSync = async (full = false) => {
    if (syncing) return;
    if (scanTask) {
      setSyncError("扫描任务进行中，请等它结束再同步");
      return;
    }
    setSyncing(true);
    setSyncError(null);
    setScanTask({ current: 0, total: 0, imported: 0, message: "准备同步元数据…", kind: "sync" });
    try {
      const report = await call<SyncReport>(CMD.syncMetadata, { full });
      setSyncReport(report);
      // 同步会补标题/作者/标签，画廊里的卡片要立刻更新
      bumpData();
    } catch (reason: unknown) {
      setSyncError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setSyncing(false);
      setScanTask(null);
    }
  };

  const [subRunning, setSubRunning] = useState(false);
  const [subResults, setSubResults] = useState<SubResult[] | null>(null);
  const [subError, setSubError] = useState<string | null>(null);

  // 「订阅更新」：检查所有启用的订阅，发现新作品就下载 → 入库 → 缩略图 → 元数据
  const runSubs = async () => {
    if (subRunning) return;
    if (scanTask) {
      setSubError("扫描任务进行中，请等它结束再更新订阅");
      return;
    }
    setSubRunning(true);
    setSubError(null);
    setScanTask({ current: 0, total: 0, imported: 0, message: "准备检查订阅…", kind: "subs" });
    try {
      const results = await call<SubResult[]>(CMD.checkAllSubs, { full: false });
      setSubResults(results);
      bumpData();
    } catch (reason: unknown) {
      setSubError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setSubRunning(false);
      setScanTask(null);
    }
  };

  const { label: themeLabel, Icon: ThemeIcon } = THEME_META[theme];
  // 折叠态要居中，但 .nav-item 自带 `padding: 0 14px`，utility 优先级盖不住，只能内联覆盖
  const itemStyle = collapsed ? { justifyContent: "center", padding: 0 } : undefined;

  return (
    <aside
      className="flex shrink-0 flex-col border-r border-line bg-surface transition-[width] duration-overlay ease-pa"
      style={{ width: collapsed ? "var(--sidebar-w-collapsed)" : "var(--sidebar-w)" }}
    >
      <div
        className={`flex h-12 shrink-0 items-center px-4 ${collapsed ? "justify-center" : "justify-between"}`}
      >
        {!collapsed && <span className="text-body text-text">Pixiv Archive</span>}
        <button
          type="button"
          className="icon-btn"
          onClick={toggleSidebar}
          title={collapsed ? "展开侧栏" : "折叠侧栏"}
          aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}
        >
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        </button>
      </div>

      <nav className="mt-6 flex flex-col gap-1 px-2">
        {NAV.map(({ key, label, Icon }) => (
          <button
            key={key}
            type="button"
            className="nav-item"
            data-active={view === key}
            style={itemStyle}
            onClick={() => setView(key)}
            title={label}
          >
            <Icon size={16} />
            {!collapsed && <span>{label}</span>}
          </button>
        ))}
      </nav>

      <div className="mt-6 flex flex-col gap-1 px-2">
        {!collapsed && <p className="group-label mb-2">操作</p>}
        {ACTIONS.map(({ label, Icon, action }) => (
          <button
            key={label}
            type="button"
            className="nav-item"
            style={itemStyle}
            disabled={
              action === undefined ||
              (action === "sync" && syncing) ||
              (action === "subs" && subRunning)
            }
            title={
              action === "scan"
                ? "扫描本地图片目录并入库"
                : action === "sync"
                  ? "同步作品元数据（标题 / 作者 / 标签 / R18 / AI）；Shift+点击 = 全量重同步"
                  : action === "subs"
                    ? "检查所有启用的订阅（收藏 / 作者），下载新作品并入库"
                    : label
            }
            onClick={
              action === "scan"
                ? () => setScanOpen(true)
                : action === "sync"
                  ? (event) => runSync(event.shiftKey)
                  : action === "subs"
                    ? runSubs
                    : undefined
            }
          >
            <Icon size={16} />
            {!collapsed && <span>{label}</span>}
          </button>
        ))}
      </div>

      <div className="mt-auto flex flex-col gap-1 px-2 pb-3">
        {!collapsed && (
          <p className="px-2 pb-2 text-micro text-text-2">
            {stats
              ? `${stats.artworks} 作品 · ${stats.images} 图 · ${stats.authors} 作者`
              : "— 作品 · — 图 · — 作者"}
          </p>
        )}

        <button
          type="button"
          className="nav-item"
          style={itemStyle}
          onClick={toggleSafeMode}
          data-active={safeMode}
          title={safeMode ? "关闭安全模式" : "开启安全模式（模糊所有图片）"}
        >
          <Lock size={16} />
          {!collapsed && <span>安全模式</span>}
        </button>

        <button
          type="button"
          className="nav-item"
          style={itemStyle}
          onClick={cycleTheme}
          title={`主题：${themeLabel}`}
        >
          <ThemeIcon size={16} />
          {!collapsed && <span>{themeLabel}</span>}
        </button>

        <button
          type="button"
          className="nav-item"
          style={itemStyle}
          onClick={() => setSettingsOpen(true)}
          title="设置"
        >
          <Settings size={16} />
          {!collapsed && <span>设置</span>}
        </button>
      </div>

      <Dialog
        open={syncReport !== null}
        title="同步完成"
        confirmLabel="知道了"
        onCancel={() => setSyncReport(null)}
        onConfirm={() => setSyncReport(null)}
      >
        <p className="text-body text-text">本次待同步 {syncReport?.total ?? 0} 件</p>
        <p className="mt-2 text-micro text-text-2">
          成功 {syncReport?.synced ?? 0} · 已删除 {syncReport?.deleted ?? 0} · 失败{" "}
          {syncReport?.failed ?? 0}
        </p>
        {syncReport?.auth_error && (
          <p className="mt-2 text-micro text-danger">{syncReport.auth_error}</p>
        )}
      </Dialog>

      <Dialog
        open={syncError !== null}
        title="同步失败"
        confirmLabel="知道了"
        onCancel={() => setSyncError(null)}
        onConfirm={() => setSyncError(null)}
      >
        <p className="text-micro text-danger">{syncError}</p>
      </Dialog>
    <Dialog
        open={subResults !== null}
        title="订阅更新完成"
        confirmLabel="知道了"
        onCancel={() => setSubResults(null)}
        onConfirm={() => setSubResults(null)}
      >
        <p className="text-body text-text">
          新增{" "}
          {subResults?.reduce((sum, row) => sum + row.new_found, 0) ?? 0} 件 · 下载{" "}
          {subResults?.reduce((sum, row) => sum + row.downloaded, 0) ?? 0} 件
        </p>
        <div className="mt-2 flex max-h-[240px] flex-col gap-1 overflow-y-auto">
          {subResults?.map((row) => (
            <p key={`${row.kind}:${row.pixiv_user_id}`} className="text-micro text-text-2">
              {row.kind === "author" ? "作者" : "收藏"} ·{" "}
              {row.name || `用户 ${row.pixiv_user_id}`}：新增 {row.new_found} · 下载{" "}
              {row.downloaded}
              {row.failed > 0 ? ` · 失败 ${row.failed}` : ""}
              {row.cancelled ? " · 已取消" : ""}
              {row.error ? `（${row.error}）` : ""}
            </p>
          ))}
        </div>
      </Dialog>

      <Dialog
        open={subError !== null}
        title="订阅更新失败"
        confirmLabel="知道了"
        onCancel={() => setSubError(null)}
        onConfirm={() => setSubError(null)}
      >
        <p className="text-micro text-danger">{subError}</p>
      </Dialog>
    </aside>
  );
}