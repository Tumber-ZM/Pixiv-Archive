import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ViewKey = "gallery" | "authors" | "tags" | "favorites" | "subs" | "local";
export type Theme = "light" | "dark" | "system";
export type TriState = "all" | "hide" | "only";
export type SortKey = "first_seen" | "create_date" | "title" | "pixiv_id" | "random";
/** 画廊布局：固定网格（1:1 裁切 + 图下信息）/ 瀑布流（原比例 + 悬停浮出标题） */
export type Layout = "grid" | "masonry";

export const SORT_LABELS: Record<SortKey, string> = {
  first_seen: "添加时间",
  create_date: "发布时间",
  title: "标题",
  pixiv_id: "Pixiv ID",
  random: "随机",
};

export const TRI_LABELS: Record<TriState, string> = {
  all: "全部",
  hide: "隐藏",
  only: "仅",
};

/** 三态循环：全部 → 隐藏 → 仅 → 全部（`06-界面规格.md` §2） */
export function nextTriState(value: TriState): TriState {
  if (value === "all") return "hide";
  if (value === "hide") return "only";
  return "all";
}

/** 扫描任务的实时进度 */
export interface ScanTask {
  current: number;
  total: number;
  imported: number;
  message: string;
  /** 浮窗标题用：扫描 / 同步元数据 / 订阅下载 / 同步收藏 / 校验图片 */
  kind?: "scan" | "sync" | "subs" | "favorites" | "repair";
}

/** 任务完成后的收尾结果 */
export interface ScanResult {
  scanned: number;
  imported: number;
  duplicates: number;
  unsupported: number;
  thumbnails: number;
  errors: number;
  cancelled: boolean;
}

interface UiState {
  view: ViewKey;
  layout: Layout;
  sidebarCollapsed: boolean;
  theme: Theme;
  /** 安全模式：全局模糊所有图片，防止内容外泄 */
  safeMode: boolean;
  settingsOpen: boolean;

  /** 从查看器跳转时携带的目标，由 Tags / Favorites / Authors 组件消费一次后清空 */
  pendingTag: string | null;
  pendingFavorite: { id: number; name: string } | null;
  pendingAuthor: { id: number; name: string } | null;
  openTag: (name: string) => void;
  openFavorite: (id: number, name: string) => void;
  openAuthor: (id: number, name: string) => void;
  consumePending: () => void;

  /** 扫描对话框与进行中的任务 */
  scanOpen: boolean;
  scanTask: ScanTask | null;
  /** 上一次扫描的汇总，非空时弹出结果面板 */
  scanResult: ScanResult | null;
  /** 作品增删 / 扫描 / 同步完成后递增，列表与侧栏统计据此重新取数 */
  dataEpoch: number;
  /** 只刷新侧栏统计（删除后作品数变化） */
  statsEpoch: number;
  setScanOpen: (open: boolean) => void;
  setScanTask: (task: ScanTask | null) => void;
  setScanResult: (result: ScanResult | null) => void;
  bumpStats: () => void;
  bumpData: () => void;

  q: string;
  sort: SortKey;
  order: "asc" | "desc";
  r18: TriState;
  ai: TriState;
  gif: TriState;
  deletedOnly: boolean;

  setView: (view: ViewKey) => void;
  toggleLayout: () => void;
  toggleSidebar: () => void;
  setTheme: (theme: Theme) => void;
  cycleTheme: () => void;
  toggleSafeMode: () => void;
  setSettingsOpen: (open: boolean) => void;

  setQ: (q: string) => void;
  setSort: (sort: SortKey) => void;
  toggleOrder: () => void;
  cycleTri: (which: "r18" | "ai" | "gif") => void;
  toggleDeletedOnly: () => void;
  clearFilters: () => void;
}

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      view: "gallery",
      layout: "grid",
      sidebarCollapsed: false,
      theme: "system",
      safeMode: false,
      settingsOpen: false,
      pendingTag: null,
      pendingFavorite: null,
      pendingAuthor: null,
      scanOpen: false,
      scanTask: null,
      scanResult: null,
      dataEpoch: 0,
      statsEpoch: 0,

      q: "",
      sort: "first_seen",
      order: "desc",
      r18: "all",
      ai: "all",
      gif: "all",
      deletedOnly: false,

      // 切换页面时清掉搜索词：顶栏搜索的搜索范围会跟着页面变
      setView: (view) =>
        set((state) => (state.view === view ? { view } : { view, q: "" })),
      toggleLayout: () =>
        set((state) => ({ layout: state.layout === "grid" ? "masonry" : "grid" })),
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setTheme: (theme) => set({ theme }),
      // 三态循环：浅色 → 深色 → 跟随系统
      cycleTheme: () =>
        set((s) => ({
          theme: s.theme === "light" ? "dark" : s.theme === "dark" ? "system" : "light",
        })),
      toggleSafeMode: () => set((s) => ({ safeMode: !s.safeMode })),
      setSettingsOpen: (settingsOpen) => set({ settingsOpen }),

      // 查看器里点标签/本地收藏：先切视图，再把目标交给对应视图消费
      openTag: (name) =>
        set({ view: "tags", pendingTag: name, pendingFavorite: null, pendingAuthor: null }),
      openFavorite: (id, name) =>
        set({
          view: "favorites",
          pendingFavorite: { id, name },
          pendingTag: null,
          pendingAuthor: null,
        }),
      openAuthor: (id, name) =>
        set({
          view: "authors",
          pendingAuthor: { id, name },
          pendingTag: null,
          pendingFavorite: null,
        }),
      consumePending: () =>
        set({ pendingTag: null, pendingFavorite: null, pendingAuthor: null }),

      setScanOpen: (scanOpen) => set({ scanOpen }),
      setScanTask: (scanTask) => set({ scanTask }),
      setScanResult: (scanResult) => set({ scanResult }),
      bumpStats: () => set((state) => ({ statsEpoch: state.statsEpoch + 1 })),
      // 列表和统计一起刷新：扫描/同步完成后当前视图立即重取
      bumpData: () =>
        set((state) => ({
          dataEpoch: state.dataEpoch + 1,
          statsEpoch: state.statsEpoch + 1,
        })),

      setQ: (q) => set({ q }),
      // 点当前排序项切换升降序，点其他项改字段并重置为降序
      setSort: (sort) =>
        set((s) => (s.sort === sort ? { order: s.order === "desc" ? "asc" : "desc" } : { sort, order: "desc" })),
      toggleOrder: () => set((s) => ({ order: s.order === "desc" ? "asc" : "desc" })),
      cycleTri: (which) => set((s) => ({ [which]: nextTriState(s[which]) }) as Pick<UiState, typeof which>),
      toggleDeletedOnly: () => set((s) => ({ deletedOnly: !s.deletedOnly })),
      clearFilters: () => set({ q: "", r18: "all", ai: "all", gif: "all", deletedOnly: false }),
    }),
    {
      name: "pixiv-archive-ui",
      // 折叠状态、主题、安全模式需要跨重启保持，筛选条件每次从默认值开始
      partialize: (state) => ({
        sidebarCollapsed: state.sidebarCollapsed,
        theme: state.theme,
        safeMode: state.safeMode,
        layout: state.layout,
      }),
    },
  ),
);