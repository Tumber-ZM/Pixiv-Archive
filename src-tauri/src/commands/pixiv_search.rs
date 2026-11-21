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
pub async fn search_pixiv_illusts(
    word: String,
    sort: Option<String>,
) -> AppResult<Vec<RemoteIllust>> {
    let word = word.trim();
    if word.is_empty() {
        return Ok(Vec::new());
    }
    let client = PixivClient::from_settings()?;
    let sort = sort.as_deref().unwrap_or("date_desc");
    if sort == "popular_desc" {
        match client.search_illusts_sorted(word, "popular_desc").await {
            Ok(items) if !items.is_empty() => return Ok(items),
            Ok(_) => {}
            Err(_) => {}
        }
    }
    client.search_illusts_sorted(word, "date_desc").await
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

/// 作者页「展示线上全部」的单页接口：前端每拿到一页就立即展示。
#[tauri::command]
pub async fn list_pixiv_author_illusts_page(
    pixiv_user_id: i64,
    next_url: Option<String>,
) -> AppResult<(Vec<RemoteIllust>, Option<String>)> {
    let client = PixivClient::from_settings()?;
    client
        .user_illusts_remote(pixiv_user_id, next_url.as_deref())
        .await
}

/// 作者页「展示线上全部」：按 Pixiv 用户 ID 拉取线上作品（最多 1000 件）。
#[tauri::command]
pub async fn list_pixiv_author_illusts(pixiv_user_id: i64) -> AppResult<Vec<RemoteIllust>> {
    let client = PixivClient::from_settings()?;
    let mut items = Vec::new();
    let mut next: Option<String> = None;
    loop {
        let (page, next_url) = client
            .user_illusts_remote(pixiv_user_id, next.as_deref())
            .await?;
        if page.is_empty() {
            break;
        }
        items.extend(page);
        if items.len() >= 1000 {
            items.truncate(1000);
            break;
        }
        match next_url {
            Some(url) => next = Some(url),
            None => break,
        }
    }
    Ok(items)
}

/// 通过后端带 Referer 拉取 Pixiv 缩略图，返回 data URL；WebView 直接请求 pximg 会被拒。
#[tauri::command]
pub async fn fetch_remote_thumbnail(url: String) -> AppResult<String> {
    if !url.starts_with("https://i.pximg.net/") {
        return Err(AppError::new("INVALID", "不支持的缩略图地址"));
    }
    let response = reqwest::Client::new()
        .get(&url)
        .header("Referer", "https://www.pixiv.net/")
        .header("User-Agent", "PixivArchive/2.0")
        .send()
        .await
        .map_err(|error| AppError::new("NETWORK", format!("无法加载 Pixiv 缩略图：{error}")))?;
    if !response.status().is_success() {
        return Err(AppError::internal(format!(
            "Pixiv 缩略图返回 HTTP {}",
            response.status().as_u16()
        )));
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("image/jpeg")
        .to_string();
    let bytes = response
        .bytes()
        .await
        .map_err(|error| AppError::new("NETWORK", format!("读取 Pixiv 缩略图失败：{error}")))?;
    if bytes.is_empty() || bytes.len() > 4 * 1024 * 1024 {
        return Err(AppError::internal("Pixiv 缩略图大小异常"));
    }
    Ok(format!(
        "data:{content_type};base64,{}",
        base64_encode(&bytes)
    ))
}

fn base64_encode(input: &[u8]) -> String {
    const TABLE: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity((input.len() + 2) / 3 * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        output.push(TABLE[((n >> 18) & 63) as usize] as char);
        output.push(TABLE[((n >> 12) & 63) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}
