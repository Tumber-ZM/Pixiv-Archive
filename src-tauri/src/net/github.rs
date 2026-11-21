use std::time::Duration;

use serde::Deserialize;

use crate::commands::settings;
use crate::error::{AppError, AppResult};
use crate::models::UpdateInfo;

/// 项目主页（「关于」页展示、没有 Release 时的跳转目标）。
pub const REPO_URL: &str = "https://github.com/Tumber-ZM/Pixiv-Archive";
/// 最新 Release 接口。匿名访问限流 60 次/小时/IP，启动检查 + 手动检查足够。
const LATEST_RELEASE_API: &str =
    "https://api.github.com/repos/Tumber-ZM/Pixiv-Archive/releases/latest";
const CHECK_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Deserialize)]
struct Release {
    tag_name: String,
    html_url: String,
    #[serde(default)]
    body: String,
}

/// 当前程序版本，编译时来自 Cargo.toml。
pub fn current_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// 把 `v2.1.0` / `2.1.0` / `2.1` 解析成数字段，供逐段比较。
fn parse_version(text: &str) -> Vec<u64> {
    text.trim()
        .trim_start_matches(['v', 'V'])
        .split('.')
        .map(|part| {
            let digits: String = part.chars().take_while(|value| value.is_ascii_digit()).collect();
            digits.parse::<u64>().unwrap_or(0)
        })
        .collect()
}

fn is_newer(latest: &str, current: &str) -> bool {
    let latest = parse_version(latest);
    let current = parse_version(current);
    let len = latest.len().max(current.len());
    for index in 0..len {
        let left = latest.get(index).copied().unwrap_or(0);
        let right = current.get(index).copied().unwrap_or(0);
        if left != right {
            return left > right;
        }
    }
    false
}

/// 查询 GitHub 最新 Release 并与本机版本比较。
///
/// - 仓库还没有发布任何 Release（404）→ 返回「暂无发布版本」，不算错误
/// - 网络失败/限流 → 返回错误，由调用方决定是否静默
pub async fn check_latest() -> AppResult<UpdateInfo> {
    let settings = settings::view()?;
    let client = crate::net::pixiv::build_http_with_ua(
        &settings.mode,
        settings.proxy.as_deref(),
        &format!("Pixiv-Archive/{}", current_version()),
    )?;

    let current = current_version();
    let response = client
        .get(LATEST_RELEASE_API)
        .header("Accept", "application/vnd.github+json")
        .timeout(CHECK_TIMEOUT)
        .send()
        .await
        .map_err(|error| AppError::internal(format!("连接 GitHub 失败：{error}")))?;

    let status = response.status();
    if status == reqwest::StatusCode::NOT_FOUND {
        return Ok(UpdateInfo {
            current: current.clone(),
            latest: current,
            has_update: false,
            url: REPO_URL.to_string(),
            notes: "暂无发布版本".to_string(),
        });
    }
    if status == reqwest::StatusCode::FORBIDDEN
        || status == reqwest::StatusCode::TOO_MANY_REQUESTS
    {
        return Err(AppError::new(
            "RATE_LIMITED",
            "GitHub 接口访问受限（可能触发了频率限制），请稍后再试",
        ));
    }
    if !status.is_success() {
        return Err(AppError::internal(format!(
            "GitHub 返回 HTTP {}",
            status.as_u16()
        )));
    }

    let release: Release = response
        .json()
        .await
        .map_err(|error| AppError::internal(format!("更新信息无法解析：{error}")))?;

    let latest = release.tag_name.trim().to_string();
    Ok(UpdateInfo {
        has_update: is_newer(&latest, &current),
        current,
        latest,
        url: release.html_url,
        notes: release.body,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_common_version_shapes() {
        assert_eq!(parse_version("v2.1.0"), vec![2, 1, 0]);
        assert_eq!(parse_version("2.0.0"), vec![2, 0, 0]);
        assert_eq!(parse_version("v2.1"), vec![2, 1]);
        assert_eq!(parse_version("2.1.0-beta"), vec![2, 1, 0]);
    }

    #[test]
    fn compares_versions_by_segment() {
        assert!(is_newer("v2.1.0", "2.0.0"));
        assert!(is_newer("v2.0.1", "2.0.0"));
        assert!(!is_newer("v2.0.0", "2.0.0"));
        assert!(!is_newer("v1.9.9", "2.0.0"));
        // 段数不同按缺省 0 处理
        assert!(is_newer("v2.0", "1.9.9"));
        assert!(!is_newer("v2", "2.0.0"));
    }
}