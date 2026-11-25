import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ViewKey = "gallery" | "authors" | "tags" | "favorites" | "bookmarkSubs" | "local";
/** 顶栏被页面自己的搜索框接管的视图 */
export type PageSearchKey = "authors" | "tags" | "favorites" | "bookmarkSubs";
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

/** 随机排序的种子：翻页期间保持不变，换种子就是重抽一轮。 */
function nextRandomSeed() {
  return Math.floor(Math.random() * 999_999) + 1;
}

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
  /** 订阅下载：该订阅还没下完的队列总量 */
  backlog?: number;
  message: string;
  /** 浮窗标题用：扫描 / 同步元数据 / 订阅下载 / 校验图片 */
  kind?: "scan" | "sync" | "subs" | "repair";
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
  /** 随机排序的种子：只用来让翻页顺序稳定，换排序或重抽时更新 */
  randomSeed: number;
  r18: TriState;
  ai: TriState;
  gif: TriState;
  deletedOnly: boolean;

  /** 各列表页自己的搜索词：作者 / 标签 / 本地收藏 / 订阅 */
  pageSearch: Record<PageSearchKey, string>;
  /** 这些视图里打开了子列表（作者作品列表等）时，顶栏交还给全局筛选 */
  nestedView: boolean;
  setPageSearch: (key: PageSearchKey, value: string) => void;
  setNestedView: (nested: boolean) => void;

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
      randomSeed: nextRandomSeed(),
      r18: "all",
      ai: "all",
      gif: "all",
      deletedOnly: false,
      pageSearch: { authors: "", tags: "", favorites: "", bookmarkSubs: "" },
      nestedView: false,

      setView: (view) => set({ view }),
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

      // 查看器里点标签/收藏夹：先切视图，再把目标交给对应视图消费
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
      setPageSearch: (key, value) =>
        set((state) => ({ pageSearch: { ...state.pageSearch, [key]: value } })),
      setNestedView: (nestedView) => set({ nestedView }),
      // 点当前排序项切换升降序，点其他项改字段并重置为降序；
      // 随机每次点都重抽种子，且不参与升降序
      setSort: (sort) =>
        set((s) => {
          if (sort === "random") {
            return { sort, order: "desc" as const, randomSeed: nextRandomSeed() };
          }
          if (s.sort === sort) {
            return { order: s.order === "desc" ? ("asc" as const) : ("desc" as const) };
          }
          return { sort, order: "desc" as const };
        }),
      toggleOrder: () => set((s) => ({ order: s.order === "desc" ? "asc" : "desc" })),
      cycleTri: (which) => set((s) => ({ [which]: nextTriState(s[which]) }) as Pick<UiState, typeof which>),
      toggleDeletedOnly: () => set((s) => ({ deletedOnly: !s.deletedOnly })),
      clearFilters: () => set({ q: "", r18: "all", ai: "all", gif: "all", deletedOnly: false }),
    }),
    {
      name: "pixiv-archive-ui",
      // 折叠状态、主题、安全模式、布局需要跨重启保持；筛选条件每次从默认值开始。
      // 排序也记住，但随机种子不记——重启后重新抽，顺序自然不同。
      partialize: (state) => ({
        sidebarCollapsed: state.sidebarCollapsed,
        theme: state.theme,
        safeMode: state.safeMode,
        layout: state.layout,
        sort: state.sort,
        order: state.order,
      }),
    },
  ),
);