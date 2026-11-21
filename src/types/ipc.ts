/** IPC 类型，字段名与 Rust 侧逐字一致（`04-IPC契约.md`）。 */

export interface AppError {
  code: string;
  message: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ArtworkCard {
  id: number;
  pixiv_id: number;
  title: string | null;
  author_id: number | null;
  author_name: string | null;
  page_count: number;
  create_date: string | null;
  width: number | null;
  height: number | null;
  pixiv_status: string;
  ai_type: number;
  /** 是否至少在一个本地收藏里 */
  is_favorited: boolean;
  cover_path: string | null;
  cover_image_id: number | null;
  /** 封面原始宽高，瀑布流据此预估卡片高度 */
  cover_width: number | null;
  cover_height: number | null;
  /** 封面原图的磁盘路径，用于「在文件夹中定位」 */
  source_path: string | null;
  is_r18: boolean;
  is_gif: boolean;
}

export interface ImageItem {
  id: number;
  page: number;
  path: string;
  width: number | null;
  height: number | null;
}

export interface Tag {
  id: number;
  name: string;
  translated_name: string | null;
}

export interface FavoriteRef {
  id: number;
  name: string;
}

export interface ArtworkDetail {
  id: number;
  pixiv_id: number;
  title: string | null;
  description: string | null;
  author_id: number | null;
  author_name: string | null;
  page_count: number;
  width: number | null;
  height: number | null;
  create_date: string | null;
  pixiv_status: string;
  first_seen: string | null;
  last_synced: string | null;
  sync_error: string | null;
  local_path: string | null;
  ai_type: number;
  tags: Tag[];
  images: ImageItem[];
  favorites: FavoriteRef[];
  is_r18: boolean;
  is_gif: boolean;
}

export interface DeletedResult {
  deleted: boolean;
  files_failed: string[];
}

export interface BatchResult {
  ok: number;
  failed: number;
  errors: string[];
}

export interface FavoriteRow {
  id: number;
  name: string;
  create_date: string;
  artwork_count: number;
}

export interface AuthorRow {
  id: number;
  pixiv_user_id: number;
  name: string;
  artwork_count: number;
  avatar_path: string | null;
  /** 该作者最近几件作品的缩略图，用于列表卡片的封面条 */
  covers: string[];
  /** 是否已订阅该作者（卡片上的铃铛状态） */
  subscribed: boolean;
  /** 是否至少有一件「收藏」来源的作品（作者订阅下载的不算） */
  collected: boolean;
  /** 是否至少有一件本地作品在 Pixiv 收藏里 */
  favorited: boolean;
}

export interface TagRow {
  id: number;
  name: string;
  translated_name: string | null;
  count: number;
  /** 该标签下随机一张作品的缩略图 */
  cover_path: string | null;
}

/** 订阅类型：对方的公开收藏 / 该作者发布的作品。 */
export type SubKind = "bookmark" | "author";

export interface Sub {
  kind: SubKind;
  /** 被订阅的 Pixiv 用户 ID（收藏订阅的目标也是用户） */
  target_pixiv_id: number;
  name: string | null;
  /** 作者订阅的起点：null = 待建立，0 = 补全全部历史 */
  baseline_pid: number | null;
  auto_download: boolean;
  last_checked: string | null;
  last_result: string | null;
  created_at: string;
}

export interface SubPreview {
  kind: SubKind;
  target_pixiv_id: number;
  name: string;
  total_bookmarks: number | null;
}

/** 本地缓存的 Pixiv 收藏概况。 */
export interface FavoriteSyncStatus {
  count: number;
  last_synced: string | null;
}

/** 一次「同步我的收藏」的结果。 */
export interface FavoriteSyncReport {
  total: number;
  public: number;
  private: number;
  pages: number;
  cancelled: boolean;
}

export interface Stats {
  artworks: number;
  images: number;
  authors: number;
  tags: number;
  favorites: number;
  deleted: number;
  r18: number;
  ai: number;
  gif: number;
}

export const TRI = ["all", "hide", "only"] as const;
export type TriState = (typeof TRI)[number];
export type SortKey = "id" | "first_seen" | "create_date" | "title" | "pixiv_id" | "random";

/** 当前数据目录各部分的字节数（设置 → 数据迁移）。 */
export interface DataSizes {
  db: number;
  thumbnails: number;
  metadata: number;
}

export interface RelinkReport {
  relinked: number;
  missing: number;
  ambiguous: number;
}

export interface BundleImportReport {
  artworks: number;
  images: number;
  thumbs_restored: number;
  backed_up_to: string | null;
  relinked: RelinkReport | null;
}

export interface LegacyImportReport {
  artworks: number;
  images: number;
  thumbs_copied: number;
  backed_up_to: string | null;
}

/** 单个订阅的检查结果。 */
export interface SubResult {
  kind: SubKind;
  pixiv_user_id: number;
  name: string;
  new_found: number;
  /** 库里已有但缺页、这次补下载的作品数 */
  repaired: number;
  downloaded: number;
  failed: number;
  pages_scanned: number;
  caught_up: boolean;
  /** 用户点了取消，检查提前结束 */
  cancelled: boolean;
  error: string | null;
}

/** Pixiv 远端作者搜索的一条结果。 */
export interface RemoteAuthor {
  pixiv_user_id: number;
  name: string;
  account: string;
  avatar: string | null;
}

/** Pixiv 远端作品搜索的一条结果。 */
export interface RemoteIllust {
  pixiv_id: number;
  title: string;
  author_name: string;
  author_id: number;
  page_count: number;
}

/** 校验/补页的结果。 */
export interface RepairReport {
  checked: number;
  repaired: number;
  failed: number;
}

/** 同步元数据的汇总。 */
export interface SyncReport {
  total: number;
  synced: number;
  deleted: number;
  failed: number;
  auth_error: string | null;
}

/** 设置面板读回的完整配置。 */
export interface SettingsView {
  has_token: boolean;
  image_source_dir: string | null;
  mode: string;
  proxy: string | null;
  sync_delay_ms: number;
  image_concurrency: number;
  image_mirror: string | null;
  thumbnail_size: number;
  safe_mode: boolean;
  theme: string;
  check_update_on_start: boolean;
  skipped_version: string | null;
}

/** 保存设置时只传改动的字段。 */
export interface SettingsInput {
  refresh_token?: string;
  image_source_dir?: string;
  mode?: string;
  proxy?: string;
  sync_delay_ms?: number;
  image_concurrency?: number;
  image_mirror?: string;
  safe_mode?: boolean;
  theme?: string;
  check_update_on_start?: boolean;
  skipped_version?: string;
}

/** 「关于」页展示的应用信息。 */
export interface AppInfo {
  version: string;
  data_dir: string;
  legacy_dir: string | null;
  log_path: string;
  repo_url: string;
}

/** 检查更新的结果。 */
export interface UpdateInfo {
  current: string;
  latest: string;
  has_update: boolean;
  /** GitHub Release 页面地址 */
  url: string;
  /** Release 正文，可能为空 */
  notes: string;
}

/** 所有列表接口共用的查询参数（`04-IPC契约.md` §三）。 */
export interface ListQuery {
  page?: number;
  page_size?: number;
  q?: string;
  sort?: SortKey;
  order?: "asc" | "desc";
  r18?: TriState;
  ai?: TriState;
  gif?: TriState;
  deleted_only?: boolean;
  seed?: number;
  author_id?: number;
  tag?: string;
  favorite_id?: number;
  /** `local` = 只看本地文件（pixiv_id < 0）；缺省 = 只看 Pixiv 作品 */
  scope?: "local" | "pixiv";
}