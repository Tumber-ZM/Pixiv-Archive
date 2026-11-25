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
  /** 是否至少在一个收藏夹里 */
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
}

export interface TagRow {
  id: number;
  name: string;
  translated_name: string | null;
  count: number;
  /** 该标签下随机一张作品的缩略图 */
  cover_path: string | null;
}

export interface BookmarkSub {
  pixiv_user_id: number;
  name: string | null;
  last_pid: number | null;
  auto_download: boolean;
  last_checked: string | null;
  last_result: string | null;
  created_at: string;
}

export interface SubPreview {
  pixiv_user_id: number;
  name: string;
  total_bookmarks: number | null;
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

/** 单个收藏订阅的检查结果。 */
export interface SubResult {
  pixiv_user_id: number;
  name: string;
  new_found: number;
  /** 库里已有、但缺页需要补的作品数 */
  repaired: number;
  downloaded: number;
  failed: number;
  pages_scanned: number;
  caught_up: boolean;
  /** 用户点了取消，检查提前结束 */
  cancelled: boolean;
  error: string | null;
}

/** 图片校验（补缺页）的汇总。 */
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