use crate::error::AppResult;
use crate::models::{AppInfo, UpdateInfo};
use crate::net::github;
use crate::platform::paths;

/// 「关于」页需要的信息：版本、数据目录、仓库地址。
#[tauri::command]
pub fn app_info() -> AppResult<AppInfo> {
    Ok(AppInfo {
        version: github::current_version(),
        data_dir: paths::data_dir()?.to_string_lossy().to_string(),
        legacy_dir: Some(paths::legacy_dir().to_string_lossy().to_string()),
        log_path: paths::logs_dir()?.to_string_lossy().to_string(),
        repo_url: github::REPO_URL.to_string(),
    })
}

/// 查询 GitHub 最新版本：启动自动检查与设置里的手动检查都走这里。
///
/// 失败由前端决定是否静默（启动检查静默，手动检查会显示错误）。
#[tauri::command]
pub async fn check_update() -> AppResult<UpdateInfo> {
    github::check_latest().await
}