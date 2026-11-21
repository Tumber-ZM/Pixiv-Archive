use std::sync::atomic::AtomicBool;

use crate::commands::download;
use crate::commands::settings;
use crate::error::{AppError, AppResult};
use crate::net::pixiv::{PixivClient, RemoteAuthor, RemoteIllust, RemoteTag};
use crate::platform::paths;

/// 本地作者搜不到时，去 Pixiv 上搜作者。
///
/// 纯查询：不写库、不改本地状态；离线时返回 `NETWORK` 错误，由前端显示成提示。
#[tauri::command]
pub async fn search_pixiv_authors(word: String) -> AppResult<Vec<RemoteAuthor>> {
    let word = word.trim();
    if word.is_empty() {
        return Ok(Vec::new());
    }
    let client = PixivClient::from_settings()?;
    client.search_users(word).await
}

/// 本地作品搜不到时，去 Pixiv 上搜作品。
#[tauri::command]
pub async fn search_pixiv_illusts(word: String) -> AppResult<Vec<RemoteIllust>> {
    let word = word.trim();
    if word.is_empty() {
        return Ok(Vec::new());
    }
    let client = PixivClient::from_settings()?;
    client.search_illusts(word).await
}

/// Pixiv 上的标签：有搜索词时聚合远端作品结果的标签，没有词时取热门标签。
#[tauri::command]
pub async fn search_pixiv_tags(word: String) -> AppResult<Vec<RemoteTag>> {
    let client = PixivClient::from_settings()?;
    client.search_tags(word.trim()).await
}

/// 把远端搜索结果里的一件作品下载并入库（补全 / 下载按钮）。
#[tauri::command]
pub async fn download_pixiv_illust(pixiv_id: i64) -> AppResult<usize> {
    let client = PixivClient::from_settings()?;
    let data = client.illust_detail(pixiv_id).await?;
    let source_root = settings::image_source_dir()?
        .map(std::path::PathBuf::from)
        .ok_or_else(|| AppError::invalid("还没有设置图片目录（设置 → 基础）"))?;
    let data_dir = paths::data_dir()?;
    let mut conn = download::open_library()?;
    let cancel = AtomicBool::new(false);
    download::fetch_and_import(
        &client,
        &mut conn,
        &data_dir,
        &source_root,
        &data,
        download::ORIGIN_LOCAL,
        &cancel,
    )
    .await
}
