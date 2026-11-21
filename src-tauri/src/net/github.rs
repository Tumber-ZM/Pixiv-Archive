use std::time::Duration;

use crate::commands::settings;
use crate::error::{AppError, AppResult};
use crate::models::UpdateInfo;

/// 项目主页（「关于」页展示、没有 Release 时的跳转目标）。
pub const REPO_URL: &str = "https://github.com/Tumber-ZM/Pixiv-Archive";
/// 最新 Release 的网页入口。GitHub 会 302 到 `/releases/tag/<tag>`；
/// 仓库还没发布 Release 时跳到 `/releases`。
///
/// 走网页跳转而不是 REST API：匿名 API 限额 60 次/小时/IP，共享出口很容易
/// 被刷到 403；网页跳转不消耗接口配额，也不需要解析 Release JSON。
const LATEST_RELEASE_PAGE: &str = "https://github.com/Tumber-ZM/Pixiv-Archive/releases/latest";
const CHECK_TIMEOUT: Duration = Duration::from_secs(15);

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

/// 从 302 的 `Location` 解析 Release 标签。
///
/// `.../releases/tag/v2.0.0` → `v2.0.0`；没有 Release 时 GitHub 跳到
/// `.../releases`，返回 `None`。
fn tag_from_location(location: &str) -> Option<String> {
    let path = location.split(['?', '#']).next().unwrap_or(location);
    let (_, tag) = path.split_once("/releases/tag/")?;
    let tag = tag.trim().trim_matches('/');
    if tag.is_empty() {
        None
    } else {
        Some(tag.to_string())
    }
}

/// 兜底：有些代理会把 302 改写成 200 并直接返回页面，这时从 HTML 里找
/// `/releases/tag/<tag>` 形式的链接。
fn tag_from_html(html: &str) -> Option<String> {
    const MARKER: &str = "/releases/tag/";
    let position = html.find(MARKER)?;
    let rest = &html[position + MARKER.len()..];
    let tag: String = rest
        .chars()
        .take_while(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '-' | '_'))
        .collect();
    if tag.is_empty() {
        None
    } else {
        Some(tag)
    }
}

/// 仓库还没有发布任何 Release：不算错误，返回「暂无发布版本」。
fn no_release(version: &str) -> UpdateInfo {
    UpdateInfo {
        current: version.to_string(),
        latest: version.to_string(),
        has_update: false,
        url: REPO_URL.to_string(),
        notes: "暂无发布版本".to_string(),
    }
}

/// 查询 GitHub 最新 Release 并与本机版本比较。
///
/// - 仓库还没有发布任何 Release → 返回「暂无发布版本」，不算错误
/// - 网络失败 → 返回错误，由调用方决定是否静默
pub async fn check_latest() -> AppResult<UpdateInfo> {
    let settings = settings::view()?;
    // 不跟随重定向：需要读 `Location` 里的 tag，跟随之后只剩页面 HTML；
    // 代理设置与其它网络请求保持一致。
    let client = crate::net::pixiv::build_http_with_ua(
        &settings.mode,
        settings.proxy.as_deref(),
        &format!("Pixiv-Archive/{}", current_version()),
        false,
    )?;

    let current = current_version();
    let response = client
        .get(LATEST_RELEASE_PAGE)
        .header("Accept", "text/html")
        .timeout(CHECK_TIMEOUT)
        .send()
        .await
        .map_err(|error| AppError::internal(format!("连接 GitHub 失败：{error}")))?;

    let status = response.status();
    let location = response
        .headers()
        .get(reqwest::header::LOCATION)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);

    if status == reqwest::StatusCode::NOT_FOUND {
        return Ok(no_release(&current));
    }
    if status == reqwest::StatusCode::FORBIDDEN
        || status == reqwest::StatusCode::TOO_MANY_REQUESTS
    {
        return Err(AppError::new(
            "RATE_LIMITED",
            "GitHub 访问受限（可能触发了频率限制），请稍后再试",
        ));
    }

    // 正常是 302；301/303/307/308 也按跳转处理
    if status.is_redirection() {
        let Some(tag) = location.as_deref().and_then(tag_from_location) else {
            // 跳到 `/releases` = 还没发布过 Release
            return Ok(no_release(&current));
        };
        return Ok(UpdateInfo {
            has_update: is_newer(&tag, &current),
            current,
            latest: tag.clone(),
            url: format!("{REPO_URL}/releases/tag/{tag}"),
            notes: String::new(),
        });
    }

    if !status.is_success() {
        return Err(AppError::internal(format!(
            "GitHub 返回 HTTP {}",
            status.as_u16()
        )));
    }

    // 200：跳转被中间层改写，直接从页面里找 tag
    let body = response.text().await.unwrap_or_default();
    match tag_from_html(&body) {
        Some(tag) => Ok(UpdateInfo {
            has_update: is_newer(&tag, &current),
            current,
            latest: tag.clone(),
            url: format!("{REPO_URL}/releases/tag/{tag}"),
            notes: String::new(),
        }),
        None => Err(AppError::internal("GitHub 返回了无法解析的响应")),
    }
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

    #[test]
    fn extracts_tag_from_redirect_location() {
        assert_eq!(
            tag_from_location("https://github.com/Tumber-ZM/Pixiv-Archive/releases/tag/v2.0.0")
                .as_deref(),
            Some("v2.0.0")
        );
        assert_eq!(
            tag_from_location("/releases/tag/v2.1.0?foo=bar").as_deref(),
            Some("v2.1.0")
        );
        assert_eq!(
            tag_from_location("/Tumber-ZM/Pixiv-Archive/releases/tag/v2.0.0/").as_deref(),
            Some("v2.0.0")
        );
        // 没有 Release：跳到 /releases
        assert_eq!(tag_from_location("/Tumber-ZM/Pixiv-Archive/releases"), None);
        assert_eq!(tag_from_location("https://github.com/"), None);
    }

    #[test]
    fn extracts_tag_from_html_fallback() {
        let html = r#"<a href="/Tumber-ZM/Pixiv-Archive/releases/tag/v2.0.0">v2.0.0</a>"#;
        assert_eq!(tag_from_html(html).as_deref(), Some("v2.0.0"));
        assert_eq!(tag_from_html("<html></html>"), None);
    }
}