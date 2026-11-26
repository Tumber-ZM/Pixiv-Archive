use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::commands::browse;
use crate::error::{AppError, AppResult};
use crate::models::{SettingsInput, SettingsView};
use crate::platform::paths;

const DEFAULT_SYNC_DELAY_MS: u32 = 200;
/// 图片同时下载数默认值：保守起步，设置面板可调（1–8）。
const DEFAULT_IMAGE_CONCURRENCY: u32 = 2;
const DEFAULT_THUMBNAIL_SIZE: u32 = 400;
pub const SETTINGS_FILE_NAME: &str = "settings.json";

/// `data_dir/settings.json` 的落盘结构。
///
/// 旧项目把这些塞在 `.env`（`IMAGE_SOURCE_DIR` 等）；新项目统一用 JSON，
/// 免得再绕一层环境变量解析。字段全是 Option，缺省即「没配过」。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct SettingsFile {
    image_source_dir: Option<String>,
    refresh_token: Option<String>,
    mode: Option<String>,
    proxy: Option<String>,
    sync_delay_ms: Option<u32>,
    image_concurrency: Option<u32>,
    image_mirror: Option<String>,
    theme: Option<String>,
    safe_mode: Option<bool>,
    thumbnail_size: Option<u32>,
    check_update_on_start: Option<bool>,
    skipped_version: Option<String>,
}

fn settings_path() -> AppResult<PathBuf> {
    Ok(paths::data_dir()?.join(SETTINGS_FILE_NAME))
}

fn read_file() -> AppResult<SettingsFile> {
    let path = settings_path()?;
    if !path.is_file() {
        return Ok(SettingsFile::default());
    }
    let raw = std::fs::read_to_string(&path)?;
    // 手工改坏时不要让整个应用起不来，退回默认值
    Ok(serde_json::from_str(&raw).unwrap_or_default())
}

fn write_file(file: &SettingsFile) -> AppResult<()> {
    let path = settings_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let raw = serde_json::to_string_pretty(file)
        .map_err(|error| AppError::internal(format!("设置序列化失败：{error}")))?;
    // 先写 .tmp 再改名，写到一半崩溃也不会留下坏 JSON
    let temp = path.with_extension("json.tmp");
    std::fs::write(&temp, raw)?;
    std::fs::rename(&temp, &path)?;
    Ok(())
}

fn non_empty(value: Option<String>) -> Option<String> {
    value
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty())
}

fn to_view(file: &SettingsFile) -> SettingsView {
    SettingsView {
        has_token: file
            .refresh_token
            .as_deref()
            .map_or(false, |token| !token.trim().is_empty()),
        image_source_dir: file.image_source_dir.clone(),
        mode: file.mode.clone().unwrap_or_else(|| "direct".to_string()),
        proxy: file.proxy.clone(),
        sync_delay_ms: file.sync_delay_ms.unwrap_or(DEFAULT_SYNC_DELAY_MS),
        image_concurrency: file
            .image_concurrency
            .unwrap_or(DEFAULT_IMAGE_CONCURRENCY)
            .clamp(1, 8),
        image_mirror: file.image_mirror.clone(),
        thumbnail_size: file.thumbnail_size.unwrap_or(DEFAULT_THUMBNAIL_SIZE),
        safe_mode: file.safe_mode.unwrap_or(false),
        theme: file.theme.clone().unwrap_or_else(|| "system".to_string()),
        check_update_on_start: file.check_update_on_start.unwrap_or(true),
        skipped_version: file.skipped_version.clone(),
    }
}

/// 扫描与「收藏订阅」下载共用的图片目录。没配置时返回 None。
pub fn image_source_dir() -> AppResult<Option<String>> {
    Ok(non_empty(read_file()?.image_source_dir))
}

/// 供其它模块读取当前配置（网络客户端等）。
pub fn view() -> AppResult<SettingsView> {
    Ok(to_view(&read_file()?))
}

/// 原始 refresh token，只给网络客户端用，不通过 IPC 暴露。
pub fn refresh_token() -> AppResult<Option<String>> {
    Ok(non_empty(read_file()?.refresh_token))
}

#[tauri::command]
pub fn get_settings() -> AppResult<SettingsView> {
    view()
}

#[tauri::command]
pub async fn save_settings(app: tauri::AppHandle, input: SettingsInput) -> AppResult<SettingsView> {
    use tauri::Manager;

    let mut file = read_file()?;
    // 只有这次真的提交了新 token 才触发「自动订阅自己」：
    // 改图片目录、主题等设置时不该反复请求 Pixiv。
    let token_saved = input.refresh_token.is_some();

    if let Some(value) = input.image_source_dir {
        file.image_source_dir = non_empty(Some(value));
    }
    if let Some(value) = input.refresh_token {
        file.refresh_token = non_empty(Some(value));
    }
    if let Some(value) = input.mode {
        file.mode = non_empty(Some(value));
    }
    if let Some(value) = input.proxy {
        file.proxy = non_empty(Some(value));
    }
    if let Some(value) = input.sync_delay_ms {
        file.sync_delay_ms = Some(value);
    }
    if let Some(value) = input.image_concurrency {
        file.image_concurrency = Some(value.clamp(1, 8));
    }
    if let Some(value) = input.image_mirror {
        file.image_mirror = non_empty(Some(value));
    }
    if let Some(value) = input.safe_mode {
        file.safe_mode = Some(value);
    }
    if let Some(value) = input.theme {
        file.theme = non_empty(Some(value));
    }
    if let Some(value) = input.check_update_on_start {
        file.check_update_on_start = Some(value);
    }
    if let Some(value) = input.skipped_version {
        file.skipped_version = non_empty(Some(value));
    }

    write_file(&file)?;

    // 图片目录刚改过时，立刻把它加进 asset 协议白名单，
    // 否则新目录里的原图会显示为空白（缩略图在 data_dir，本来就放行）
    if let Some(dir) = file.image_source_dir.as_deref() {
        let _ = app.asset_protocol_scope().allow_directory(dir, true);
    }

    // token 保存成功后，把当前账号加进「收藏订阅」（私密收藏）。
    // 识别失败（token 无效 / 网络不通）静默跳过，不能让设置保存失败。
    if token_saved {
        let _ = tokio::time::timeout(
            std::time::Duration::from_secs(8),
            browse::ensure_self_subscription(),
        )
        .await;
    }

    Ok(to_view(&file))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn view_falls_back_to_defaults() {
        let view = to_view(&SettingsFile::default());
        assert!(!view.has_token);
        assert!(view.image_source_dir.is_none());
        assert_eq!(view.mode, "direct");
        assert_eq!(view.sync_delay_ms, DEFAULT_SYNC_DELAY_MS);
        assert_eq!(view.image_concurrency, DEFAULT_IMAGE_CONCURRENCY);
        assert_eq!(view.thumbnail_size, DEFAULT_THUMBNAIL_SIZE);
        assert_eq!(view.theme, "system");
        assert!(view.check_update_on_start, "更新检查默认开启");
        assert!(view.skipped_version.is_none());
    }

    #[test]
    fn blank_paths_are_dropped() {
        assert_eq!(non_empty(Some("   ".to_string())), None);
        assert_eq!(
            non_empty(Some(" D:\\Pics ".to_string())),
            Some("D:\\Pics".to_string())
        );
    }

    #[test]
    fn token_only_counts_when_not_blank() {
        let mut file = SettingsFile::default();
        file.refresh_token = Some("   ".to_string());
        assert!(!to_view(&file).has_token);
        file.refresh_token = Some("abc".to_string());
        assert!(to_view(&file).has_token);
    }
}