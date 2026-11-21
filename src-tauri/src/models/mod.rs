// IPC 数据类型（`04-IPC契约.md`）。字段名与 TS 侧 `src/types/ipc.ts` 必须逐字一致。
//
// T02 只用到 ImportReport 之外的一小部分；其余在 T04–T09 的命令里逐步启用，
// 因此整个模块暂时允许 dead_code，到 T10 验收时移除这个 allow。
#![allow(dead_code)]

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize)]
pub struct Page<T> {
    pub items: Vec<T>,
    pub total: i64,
    pub page: u32,
    pub page_size: u32,
    pub total_pages: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtworkCard {
    pub id: i64,
    pub pixiv_id: i64,
    pub title: Option<String>,
    pub author_id: Option<i64>,
    pub author_name: Option<String>,
    pub page_count: i64,
    pub create_date: Option<String>,
    pub width: Option<i64>,
    pub height: Option<i64>,
    pub pixiv_status: String,
    pub ai_type: i64,
    /// 是否至少在一个本地收藏里
    pub is_favorited: bool,
    pub cover_path: Option<String>,
    pub cover_image_id: Option<i64>,
    /// 封面原始宽高，用于瀑布流按比例预估卡片高度
    pub cover_width: Option<i64>,
    pub cover_height: Option<i64>,
    /// 封面原图的磁盘路径（`cover_path` 可能已被替换成缩略图，定位文件夹要用这个）
    pub source_path: Option<String>,
    pub is_r18: bool,
    pub is_gif: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImageItem {
    pub id: i64,
    pub page: i64,
    pub path: String,
    pub width: Option<i64>,
    pub height: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Tag {
    pub id: i64,
    pub name: String,
    pub translated_name: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FavoriteRef {
    pub id: i64,
    pub name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtworkDetail {
    pub id: i64,
    pub pixiv_id: i64,
    pub title: Option<String>,
    pub description: Option<String>,
    pub author_id: Option<i64>,
    pub author_name: Option<String>,
    pub page_count: i64,
    pub width: Option<i64>,
    pub height: Option<i64>,
    pub create_date: Option<String>,
    pub pixiv_status: String,
    pub first_seen: Option<String>,
    pub last_synced: Option<String>,
    pub sync_error: Option<String>,
    pub local_path: Option<String>,
    pub ai_type: i64,
    pub tags: Vec<Tag>,
    pub images: Vec<ImageItem>,
    pub favorites: Vec<FavoriteRef>,
    pub is_r18: bool,
    pub is_gif: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub artworks: i64,
    pub images: i64,
    pub authors: i64,
    pub tags: i64,
    pub favorites: i64,
    pub deleted: i64,
    pub r18: i64,
    pub ai: i64,
    pub gif: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct DeletedResult {
    pub deleted: bool,
    pub files_failed: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BatchResult {
    pub ok: i64,
    pub failed: i64,
    pub errors: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuthorRow {
    pub id: i64,
    pub pixiv_user_id: i64,
    pub name: String,
    pub artwork_count: i64,
    pub avatar_path: Option<String>,
    /// 该作者最近几件作品的缩略图，用于列表卡片的封面条
    pub covers: Vec<String>,
    /// 是否已订阅该作者（列表里直接显示铃铛状态，不用再查一次）
    pub subscribed: bool,
    /// 是否至少有一件「收藏」来源的作品（作者订阅下载的不算，用于「仅看收藏」筛选）
    pub collected: bool,
    /// 是否至少有一件本地作品在 Pixiv 收藏里（同步收藏后才有意义）
    pub favorited: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct TagRow {
    pub id: i64,
    pub name: String,
    pub translated_name: Option<String>,
    pub count: i64,
    /// 该标签下随机一张作品的缩略图，用于标签画廊的封面
    pub cover_path: Option<String>,
}

/// 标签组合。`tags` 存标签原名；后续标签组合订阅引用 `id`。
#[derive(Debug, Clone, Serialize)]
pub struct TagCombo {
    pub id: i64,
    pub name: String,
    pub tags: Vec<String>,
    pub pinned: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct FavoriteRow {
    pub id: i64,
    pub name: String,
    pub create_date: String,
    pub artwork_count: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct JobHandle {
    pub job_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct JobSnapshot {
    pub job_id: String,
    pub kind: String,
    pub status: String,
    pub phase: String,
    pub current: i64,
    pub total: Option<i64>,
    pub message: String,
    pub error: Option<String>,
}

/// 订阅类型。`bookmark` = 对方的公开收藏，`author` = 该作者发布的作品。
///
/// 以后加 tag 组合订阅时在这里加一个值即可，表结构不用动。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubKind {
    Bookmark,
    Author,
}

impl SubKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bookmark => "bookmark",
            Self::Author => "author",
        }
    }

    /// 前端传字符串；未知值返回 None，由命令层转成 INVALID_INPUT。
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "bookmark" | "bookmarks" | "favorite" => Some(Self::Bookmark),
            "author" | "artist" | "user" => Some(Self::Author),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Sub {
    pub kind: String,
    /// 被订阅的 Pixiv 用户 ID（收藏订阅的目标也是用户）
    pub target_pixiv_id: i64,
    pub name: Option<String>,
    /// 作者订阅的起点：NULL = 待建立，0 = 补全全部历史
    pub baseline_pid: Option<i64>,
    pub auto_download: bool,
    pub last_checked: Option<String>,
    pub last_result: Option<String>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SubPreview {
    pub kind: String,
    pub target_pixiv_id: i64,
    pub name: String,
    pub total_bookmarks: Option<i64>,
}

/// 本地缓存的 Pixiv 收藏概况（作者列表筛选与「同步收藏」按钮用）。
#[derive(Debug, Clone, Default, Serialize)]
pub struct FavoriteSyncStatus {
    pub count: i64,
    pub last_synced: Option<String>,
}

/// 一次「同步我的收藏」的结果。
#[derive(Debug, Clone, Default, Serialize)]
pub struct FavoriteSyncReport {
    pub total: usize,
    pub public: usize,
    pub private: usize,
    pub pages: usize,
    pub cancelled: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct SettingsView {
    pub has_token: bool,
    pub image_source_dir: Option<String>,
    pub mode: String,
    pub proxy: Option<String>,
    pub sync_delay_ms: u32,
    /// 图片同时下载数（1–8）。默认 2，保守起步
    pub image_concurrency: u32,
    pub image_mirror: Option<String>,
    pub thumbnail_size: u32,
    pub safe_mode: bool,
    pub theme: String,
    /// 启动时自动检查更新
    pub check_update_on_start: bool,
    /// 用户点过「忽略此版本」时记下的版本号
    pub skipped_version: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SettingsInput {
    pub refresh_token: Option<String>,
    pub image_source_dir: Option<String>,
    pub mode: Option<String>,
    pub proxy: Option<String>,
    pub sync_delay_ms: Option<u32>,
    pub image_concurrency: Option<u32>,
    pub image_mirror: Option<String>,
    pub safe_mode: Option<bool>,
    pub theme: Option<String>,
    pub check_update_on_start: Option<bool>,
    pub skipped_version: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct IpRefreshResult {
    pub updated: usize,
    pub detail: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MigrationSummary {
    pub db_size: i64,
    pub thumbs_size: i64,
    pub metadata_size: i64,
    pub artworks: i64,
    pub images: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppInfo {
    pub version: String,
    pub data_dir: String,
    pub legacy_dir: Option<String>,
    pub log_path: String,
    pub repo_url: String,
}

/// 检查更新的结果。`has_update` 为 false 时 `latest` 可能等于当前版本。
#[derive(Debug, Clone, Serialize)]
pub struct UpdateInfo {
    pub current: String,
    pub latest: String,
    pub has_update: bool,
    /// GitHub Release 页面地址，有新版本时给用户跳转
    pub url: String,
    /// Release 正文（更新说明），可能为空
    pub notes: String,
}

/// 列表查询统一参数，所有列表接口共用（`04-IPC契约.md` §三）。
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ListQuery {
    pub page: Option<u32>,
    pub page_size: Option<u32>,
    pub q: Option<String>,
    pub sort: Option<String>,
    pub order: Option<String>,
    pub r18: Option<String>,
    pub ai: Option<String>,
    pub gif: Option<String>,
    pub deleted_only: Option<bool>,
    pub seed: Option<i64>,
    pub author_id: Option<i64>,
    pub tag: Option<String>,
    /// 同时命中这些标签（标签组合用，AND）
    pub tags_all: Option<Vec<String>>,
    pub favorite_id: Option<i64>,
    /// 只看 Pixiv 收藏里的作品（作者页的「仅看收藏」）
    pub favorited_only: Option<bool>,
    /// 没同步收藏时的兜底：只看订阅前就有的作品
    pub collected_only: Option<bool>,
    /// 只看已订阅作者的作品
    pub subscribed_only: Option<bool>,
    /// `local` = 只看本地图片（pixiv_id < 0）；缺省 = 只看 Pixiv 作品
    pub scope: Option<String>,
}