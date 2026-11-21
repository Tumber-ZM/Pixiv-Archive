use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use md5::{Digest, Md5};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::Sha256;
use tokio::io::AsyncWriteExt;
use tokio::sync::Semaphore;

use crate::commands::settings;
use crate::error::{AppError, AppResult};

const OAUTH_URL: &str = "https://oauth.secure.pixiv.net/auth/token";
const API_BASE: &str = "https://app-api.pixiv.net";
/// 与 pixivpy 3.7.5 一致的 App OAuth client。
/// 少写 `DbtuzpyT` 后缀会直接 918「不正なOAuthクライアントです」。
const CLIENT_ID: &str = "MOBrBDS8blbauoSck0ZfDbtuzpyT";
const CLIENT_SECRET: &str = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj";
/// Pixiv 用它对客户端时间戳做签名校验
const HASH_SECRET: &str = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c";
const USER_AGENT: &str = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// 图片重试上限：指数退避 + jitter，4 次足够覆盖瞬时抖动
const MAX_IMAGE_ATTEMPTS: usize = 4;
/// 单张图片的读取超时（原图可达 20MB，比 JSON 接口宽松）
const IMAGE_TIMEOUT: Duration = Duration::from_secs(120);
/// access_token 名义有效期 1 小时，提前 5 分钟换新
const TOKEN_MARGIN: u64 = 300;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IllustTag {
    pub name: String,
    pub translated_name: Option<String>,
}

/// `/v1/illust/detail` 里同步需要的字段，字段语义照旧项目 `_parse_illust`。
#[derive(Debug, Clone, Serialize)]
pub struct IllustData {
    pub pixiv_id: i64,
    pub title: String,
    pub description: String,
    pub author_id: i64,
    pub author_name: String,
    pub author_avatar: Option<String>,
    pub create_date: Option<String>,
    pub page_count: i64,
    /// Pixiv 的 `visible`：false = 已删除或非公开，图片地址会返回 limit_unknown 占位图
    pub visible: bool,
    pub width: Option<i64>,
    pub height: Option<i64>,
    /// Pixiv：1 = 非 AI，2 = AI。缺失按 1 处理，避免误标
    pub ai_type: i64,
    pub tags: Vec<IllustTag>,
    pub image_urls: Vec<String>,
    /// Pixiv 动图（ugoira）：静态首帧不能直接用，要走 zip + 帧合成 GIF
    pub is_ugoira: bool,
}

#[derive(Debug, Clone)]
pub struct UgoiraFrame {
    pub file: String,
    pub delay: u64,
}

#[derive(Debug, Clone)]
pub struct UgoiraMeta {
    pub zip_url: String,
    pub frames: Vec<UgoiraFrame>,
}

/// 作品列表（收藏 / 作者作品）里的一项：只取下载/入库需要的字段，原图 URL 靠 detail 另取。
#[derive(Debug, Clone, Serialize)]
pub struct IllustRef {
    pub pixiv_id: i64,
    pub title: String,
    pub author_name: String,
    pub page_count: i64,
}

/// 远端作者搜索的一条结果。
#[derive(Debug, Clone, Serialize)]
pub struct RemoteAuthor {
    pub pixiv_user_id: i64,
    pub name: String,
    pub account: String,
    pub avatar: Option<String>,
}

/// 远端作品搜索的一条结果。
#[derive(Debug, Clone, Serialize)]
pub struct RemoteIllust {
    pub pixiv_id: i64,
    pub title: String,
    pub author_name: String,
    pub author_id: i64,
    pub page_count: i64,
}

struct CachedToken {
    access_token: String,
    expires_at: Instant,
    /// token 所属的 Pixiv 用户 ID（OAuth 响应里的 `response.user.id`）
    user_id: Option<i64>,
}

pub struct PixivClient {
    http: reqwest::Client,
    refresh_token: String,
    mirror: Option<String>,
    token: Mutex<Option<CachedToken>>,
    /// 图片下载并发上限（设置 `image_concurrency`，默认 2）
    image_permits: Arc<Semaphore>,
    /// API 节流：保存「下一次允许发起请求的时刻」，串行调用不会挤在一起
    api_gate: Mutex<Instant>,
    api_interval: Duration,
    /// 连续被限流的次数：0–4，用于自动放慢请求
    rate_penalty: Mutex<u32>,
}

impl PixivClient {
    /// 按设置里的连接模式与 token 建客户端。
    pub fn from_settings() -> AppResult<Self> {
        let settings = settings::view()?;
        let refresh_token = settings::refresh_token()?.unwrap_or_default();
        let http = build_http(&settings.mode, settings.proxy.as_deref())?;
        let concurrency = settings.image_concurrency.clamp(1, 8) as usize;
        Ok(Self {
            http,
            refresh_token,
            mirror: settings.image_mirror.clone(),
            token: Mutex::new(None),
            image_permits: Arc::new(Semaphore::new(concurrency)),
            api_gate: Mutex::new(Instant::now()),
            api_interval: Duration::from_millis(settings.sync_delay_ms as u64),
            rate_penalty: Mutex::new(0),
        })
    }

    /// 被限流一次就多等一个基础间隔，最多放大到 5 倍。
    fn note_rate_limited(&self) {
        if let Ok(mut guard) = self.rate_penalty.lock() {
            *guard = (*guard + 1).min(4);
        }
    }

    /// 请求成功后立刻恢复原速。
    fn note_success(&self) {
        if let Ok(mut guard) = self.rate_penalty.lock() {
            *guard = 0;
        }
    }

    fn current_interval(&self) -> Duration {
        let penalty = self
            .rate_penalty
            .lock()
            .map(|guard| (*guard).min(4))
            .unwrap_or(0);
        self.api_interval * (1 + penalty)
    }

    /// API 节流：先预约下一次允许时刻再睡眠，多任务共用同一个客户端时不会叠加等待。
    async fn throttle_api(&self) {
        let wait = {
            let Ok(mut guard) = self.api_gate.lock() else {
                return;
            };
            let now = Instant::now();
            let wait = guard.saturating_duration_since(now);
            *guard = now + wait + self.current_interval();
            wait
        };
        if wait > Duration::ZERO {
            tokio::time::sleep(wait).await;
        }
    }

    /// 拿 access_token，命中缓存就不发请求。
    async fn access_token(&self) -> AppResult<String> {
        if let Ok(guard) = self.token.lock() {
            if let Some(cached) = guard.as_ref() {
                if cached.expires_at > Instant::now() {
                    return Ok(cached.access_token.clone());
                }
            }
        }

        if self.refresh_token.trim().is_empty() {
            return Err(AppError::invalid(
                "还没有填写 Pixiv Refresh Token（设置 → 网络）",
            ));
        }

        // Pixiv 会校验 App 端签名：x-client-time + md5(time + hash_secret)
        let client_time = utc_now_iso();
        let client_hash = {
            let mut hasher = Md5::new();
            hasher.update(format!("{client_time}{HASH_SECRET}"));
            format!("{:x}", hasher.finalize())
        };

        let response = self
            .http
            .post(OAUTH_URL)
            .header("App-OS", "ios")
            .header("App-OS-Version", "14.6")
            .header("x-client-time", &client_time)
            .header("x-client-hash", &client_hash)
            .header("Accept-Language", "zh-CN")
            .form(&[
                ("get_secure_url", "1"),
                ("client_id", CLIENT_ID),
                ("client_secret", CLIENT_SECRET),
                ("grant_type", "refresh_token"),
                ("refresh_token", self.refresh_token.trim()),
            ])
            .send()
            .await
            .map_err(|error| AppError::internal(format!("连接 Pixiv 认证服务失败：{error}")))?;

        let status = response.status();
        let body: Value = response.json().await.unwrap_or(Value::Null);

        if !status.is_success() {
            let reason = body
                .get("message")
                .and_then(Value::as_str)
                .or_else(|| body.get("error").and_then(Value::as_str))
                .unwrap_or("未知原因");
            return Err(AppError::new(
                "AUTH",
                format!("Pixiv 认证失败（HTTP {}）：{reason}", status.as_u16()),
            ));
        }

        let access_token = body
            .pointer("/response/access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::new("AUTH", "认证响应里没有 access_token"))?
            .to_string();
        let expires_in = body
            .pointer("/response/expires_in")
            .and_then(Value::as_u64)
            .unwrap_or(3600);
        // user.id 在 Pixiv 的 JSON 里有时是字符串、有时是数字，两种都接
        let user_id = body
            .pointer("/response/user/id")
            .and_then(|value| {
                value
                    .as_i64()
                    .or_else(|| value.as_str().and_then(|raw| raw.parse().ok()))
            });

        if let Ok(mut guard) = self.token.lock() {
            *guard = Some(CachedToken {
                access_token: access_token.clone(),
                expires_at: Instant::now()
                    + Duration::from_secs(expires_in.saturating_sub(TOKEN_MARGIN).max(60)),
                user_id,
            });
        }
        Ok(access_token)
    }

    /// token 所属的 Pixiv 用户 ID（同步「我的收藏」要用）。
    ///
    /// OAuth 响应里带 `user.id`；拿不到时返回错误，由调用方提示用户重新填 token。
    pub async fn current_user_id(&self) -> AppResult<i64> {
        self.access_token().await?;
        let guard = self
            .token
            .lock()
            .map_err(|_| AppError::internal("token 缓存已损坏"))?;
        guard
            .as_ref()
            .and_then(|cached| cached.user_id)
            .ok_or_else(|| AppError::internal("Pixiv 认证响应里没有用户 ID，请重新填写 Refresh Token"))
    }

    /// 拉作品详情并转成同步用的结构。
    pub async fn illust_detail(&self, pixiv_id: i64) -> AppResult<IllustData> {
        self.throttle_api().await;
        let token = self.access_token().await?;

        let response = self
            .http
            .get(format!("{API_BASE}/v1/illust/detail"))
            .query(&[("illust_id", pixiv_id)])
            .bearer_auth(token)
            .header("Referer", "https://app-api.pixiv.net/")
            .send()
            .await
            .map_err(|error| AppError::internal(format!("请求作品详情失败：{error}")))?;

        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            return Err(AppError::new(
                "AUTH",
                "Pixiv 认证失效，请在设置里更新 Refresh Token",
            ));
        }
        if status == reqwest::StatusCode::NOT_FOUND {
            return Err(AppError::not_found("作品在 Pixiv 上不存在或已删除"));
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            self.note_rate_limited();
            return Err(AppError::internal("Pixiv 接口限流（HTTP 429），已自动降速"));
        }
        if !status.is_success() {
            return Err(AppError::internal(format!(
                "Pixiv 返回 HTTP {}",
                status.as_u16()
            )));
        }

        let body: Value = response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("作品详情响应无法解析：{error}")))?;

        if body.get("error").is_some() {
            return Err(AppError::internal(format!(
                "Pixiv 返回错误：{}",
                body.get("error").map(Value::to_string).unwrap_or_default()
            )));
        }

        let illust = body
            .get("illust")
            .ok_or_else(|| AppError::not_found("Pixiv 返回的作品数据为空"))?;
        Ok(parse_illust(illust))
    }

    /// 取用户收藏列表的一页。`restrict` = `public` / `private`。
    ///
    /// `private` 只有 token 属于该用户时才有内容，用来同步「我的收藏」。
    pub async fn user_bookmarks(
        &self,
        user_id: i64,
        restrict: &str,
        next_url: Option<&str>,
    ) -> AppResult<(Vec<IllustRef>, Option<String>)> {
        self.throttle_api().await;
        let token = self.access_token().await?;
        let url = next_url
            .map(str::to_string)
            .unwrap_or_else(|| format!("{API_BASE}/v1/user/bookmarks/illust"));
        let mut request = self
            .http
            .get(&url)
            .header("Referer", "https://app-api.pixiv.net/")
            .header("Accept-Language", "zh-CN")
            .bearer_auth(token);
        if next_url.is_none() {
            request = request.query(&[
                ("user_id", user_id.to_string()),
                ("restrict", restrict.to_string()),
            ]);
        }

        let response = request
            .send()
            .await
            .map_err(|error| AppError::internal(format!("请求收藏列表失败：{error}")))?;

        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            return Err(AppError::new(
                "AUTH",
                "Pixiv 认证失效，请在设置里更新 Refresh Token",
            ));
        }
        if status == reqwest::StatusCode::NOT_FOUND {
            return Err(AppError::not_found("该用户不存在，或收藏列表不可见"));
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            self.note_rate_limited();
            return Err(AppError::internal("Pixiv 接口限流（HTTP 429），已自动降速"));
        }
        if !status.is_success() {
            return Err(AppError::internal(format!(
                "Pixiv 返回 HTTP {}",
                status.as_u16()
            )));
        }

        let body: Value = response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("收藏列表响应无法解析：{error}")))?;

        Ok(parse_illust_list(&body))
    }

    /// 取用户公开收藏列表的一页（订阅检查用）。
    pub async fn user_bookmarks_public(
        &self,
        user_id: i64,
        next_url: Option<&str>,
    ) -> AppResult<(Vec<IllustRef>, Option<String>)> {
        self.user_bookmarks(user_id, "public", next_url).await
    }

    /// 取用户作品列表的一页（App API `/v1/user/illusts`）。
    ///
    /// 作者订阅用它替代收藏接口，返回结构与收藏列表完全一致；
    /// 收藏按收藏时间倒序、作品按发布时间倒序，两者都从新到旧。
    pub async fn user_illusts(
        &self,
        user_id: i64,
        next_url: Option<&str>,
    ) -> AppResult<(Vec<IllustRef>, Option<String>)> {
        self.throttle_api().await;
        let token = self.access_token().await?;
        let url = next_url
            .map(str::to_string)
            .unwrap_or_else(|| format!("{API_BASE}/v1/user/illusts"));
        let mut request = self
            .http
            .get(&url)
            .header("Referer", "https://app-api.pixiv.net/")
            .header("Accept-Language", "zh-CN")
            .bearer_auth(token);
        if next_url.is_none() {
            // 不带 filter=for_ios：那会把 R-18 作品过滤掉，与收藏订阅的口径不一致
            request = request.query(&[
                ("user_id", user_id.to_string()),
                ("type", "illust".to_string()),
            ]);
        }

        let response = request
            .send()
            .await
            .map_err(|error| AppError::internal(format!("请求作者作品失败：{error}")))?;

        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            return Err(AppError::new(
                "AUTH",
                "Pixiv 认证失效，请在设置里更新 Refresh Token",
            ));
        }
        if status == reqwest::StatusCode::NOT_FOUND {
            return Err(AppError::not_found("该用户不存在"));
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            self.note_rate_limited();
            return Err(AppError::internal("Pixiv 接口限流（HTTP 429），已自动降速"));
        }
        if !status.is_success() {
            return Err(AppError::internal(format!(
                "Pixiv 返回 HTTP {}",
                status.as_u16()
            )));
        }

        let body: Value = response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("作者作品列表无法解析：{error}")))?;

        Ok(parse_illust_list(&body))
    }

    /// 该作者最新一件作品的 ID（没有作品或请求失败时为 None）。
    ///
    /// 作者订阅默认只追新：订阅时用它记下起点，之后的检查只处理比它新的作品。
    pub async fn latest_illust_id(&self, user_id: i64) -> AppResult<Option<i64>> {
        let (items, _) = self.user_illusts(user_id, None).await?;
        Ok(items.iter().map(|item| item.pixiv_id).max())
    }

    /// 下载一张图片到内存（头像等小文件用）。走镜像替换 + 必须的 Referer。
    ///
    /// 429 按 `Retry-After` 冷却；5xx 与连接重置重试；其它 4xx 视为确定性失败。
    pub async fn fetch_image(&self, url: &str) -> AppResult<Vec<u8>> {
        let _permit = self
            .image_permits
            .acquire()
            .await
            .map_err(|_| AppError::internal("下载并发控制已关闭"))?;
        let target = apply_mirror(url, self.mirror.as_deref());
        let mut last_error = String::from("未知错误");

        for attempt in 0..MAX_IMAGE_ATTEMPTS {
            let result = self
                .http
                .get(&target)
                .header("Referer", "https://app-api.pixiv.net/")
                .timeout(IMAGE_TIMEOUT)
                .send()
                .await;

            match result {
                Ok(response) if response.status().is_success() => {
                    match response.bytes().await {
                        Ok(bytes) => {
                            self.note_success();
                            return Ok(bytes.to_vec());
                        }
                        Err(error) => last_error = format!("读取响应失败：{error}"),
                    }
                }
                Ok(response) if response.status() == reqwest::StatusCode::TOO_MANY_REQUESTS => {
                    self.note_rate_limited();
                    let wait = retry_after(&response).unwrap_or_else(|| Duration::from_secs(30));
                    last_error = format!("HTTP 429（冷却 {}s）", wait.as_secs());
                    tokio::time::sleep(wait).await;
                    continue;
                }
                Ok(response) => {
                    let status = response.status();
                    last_error = format!("HTTP {}", status.as_u16());
                    // 403 可能是 Referer/镜像的瞬时异常，留一次重试机会
                    if status.is_client_error() && status != reqwest::StatusCode::FORBIDDEN {
                        break;
                    }
                }
                Err(error) => last_error = format!("{error}"),
            }

            if attempt + 1 < MAX_IMAGE_ATTEMPTS {
                sleep_backoff(attempt).await;
            }
        }

        Err(AppError::internal(format!(
            "下载图片失败（{last_error}）：{target}"
        )))
    }

    /// 流式下载到文件，返回 sha256。
    ///
    /// - 先写 `<dest>.part`，成功后才改名，中断不会留下半张图被扫描入库；
    /// - `.part` 已存在时带 `Range` 续传，网络中断不必从头再下；
    /// - 哈希边下边算，入库时直接用，省掉一次整文件重读；
    /// - `Content-Length` 与实写字节数不符视为失败（防代理返回截断或 HTML 错误页）。
    pub async fn fetch_image_to(
        &self,
        url: &str,
        dest: &Path,
        cancel: Option<&AtomicBool>,
    ) -> AppResult<String> {
        let cancelled = || cancel.is_some_and(|flag| flag.load(Ordering::Relaxed));
        if cancelled() {
            return Err(AppError::cancelled());
        }
        let _permit = self
            .image_permits
            .acquire()
            .await
            .map_err(|_| AppError::internal("下载并发控制已关闭"))?;
        let target = apply_mirror(url, self.mirror.as_deref());
        let part = PathBuf::from(format!("{}.part", dest.display()));
        let mut last_error = String::from("未知错误");

        for attempt in 0..MAX_IMAGE_ATTEMPTS {
            if cancelled() {
                return Err(AppError::cancelled());
            }
            let (have, mut hasher) = match read_part_state(&part).await {
                Some(state) => state,
                None => (0u64, Sha256::new()),
            };

            let mut request = self
                .http
                .get(&target)
                .header("Referer", "https://app-api.pixiv.net/")
                .timeout(IMAGE_TIMEOUT);
            if have > 0 {
                request = request.header("Range", format!("bytes={have}-"));
            }

            let mut response = match request.send().await {
                Ok(response) => response,
                Err(error) => {
                    last_error = format!("{error}");
                    sleep_cancellable(backoff_duration(attempt), cancel).await?;
                    continue;
                }
            };

            let status = response.status();

            // 服务端认为 `.part` 已经完整
            if status == reqwest::StatusCode::RANGE_NOT_SATISFIABLE && have > 0 {
                self.note_success();
                return finish_part(&part, dest, hasher);
            }
            if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
                self.note_rate_limited();
                let wait = retry_after(&response).unwrap_or_else(|| Duration::from_secs(30));
                last_error = format!("HTTP 429（冷却 {}s）", wait.as_secs());
                sleep_cancellable(wait, cancel).await?;
                continue;
            }
            if !status.is_success() {
                last_error = format!("HTTP {}", status.as_u16());
                if status.is_client_error() && status != reqwest::StatusCode::FORBIDDEN {
                    break;
                }
                sleep_cancellable(backoff_duration(attempt), cancel).await?;
                continue;
            }

            // 206 才允许追加；200 说明服务端忽略了 Range，必须截断重写
            let append = status == reqwest::StatusCode::PARTIAL_CONTENT && have > 0;
            if !append {
                hasher = Sha256::new();
            }

            let mut file = if append {
                tokio::fs::OpenOptions::new().append(true).open(&part).await
            } else {
                tokio::fs::File::create(&part).await
            }
            .map_err(AppError::io)?;

            let mut failure: Option<String> = None;
            let mut written = 0u64;
            loop {
                if cancelled() {
                    let _ = file.sync_all().await;
                    return Err(AppError::cancelled());
                }
                match response.chunk().await {
                    Ok(Some(chunk)) => {
                        hasher.update(&chunk);
                        if let Err(error) = file.write_all(&chunk).await {
                            failure = Some(format!("写入失败：{error}"));
                            break;
                        }
                        written += chunk.len() as u64;
                    }
                    Ok(None) => break,
                    Err(error) => {
                        failure = Some(format!("读取响应失败：{error}"));
                        break;
                    }
                }
            }
            let _ = file.sync_all().await;
            drop(file);

            if let Some(error) = failure {
                last_error = error;
                sleep_cancellable(backoff_duration(attempt), cancel).await?;
                continue;
            }

            if let Some(expected) = response.content_length() {
                if written != expected {
                    last_error = format!("下载不完整：服务端声明 {expected} 字节，实际 {written}");
                    sleep_cancellable(backoff_duration(attempt), cancel).await?;
                    continue;
                }
            }

            self.note_success();
            return finish_part(&part, dest, hasher);
        }

        Err(AppError::internal(format!(
            "下载图片失败（{last_error}）：{target}"
        )))
    }
}

/// 读 `.part` 已有内容的长度与哈希；不存在或为空返回 None。
async fn read_part_state(part: &Path) -> Option<(u64, Sha256)> {
    let meta = tokio::fs::metadata(part).await.ok()?;
    if !meta.is_file() || meta.len() == 0 {
        return None;
    }
    let path = part.to_path_buf();
    tokio::task::spawn_blocking(move || {
        use std::io::Read as _;
        let mut file = std::fs::File::open(&path).ok()?;
        let mut hasher = Sha256::new();
        let mut buffer = [0u8; 64 * 1024];
        let mut total = 0u64;
        loop {
            let read = file.read(&mut buffer).ok()?;
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
            total += read as u64;
        }
        Some((total, hasher))
    })
    .await
    .ok()
    .flatten()
}

/// `.part` 改名成正式文件并返回哈希。
fn finish_part(part: &Path, dest: &Path, hasher: Sha256) -> AppResult<String> {
    // Windows 的 rename 不覆盖已存在文件；调用方保证 dest 不存在，这里兜底清掉半成品
    if dest.exists() {
        let _ = std::fs::remove_file(dest);
    }
    std::fs::rename(part, dest).map_err(AppError::io)?;
    Ok(format!("{:x}", hasher.finalize()))
}

/// 指数退避 + 抖动。
fn backoff_duration(attempt: usize) -> Duration {
    let base = 500u64.saturating_mul(1u64 << attempt.min(4));
    Duration::from_millis(base + jitter_ms())
}

async fn sleep_backoff(attempt: usize) {
    tokio::time::sleep(backoff_duration(attempt)).await;
}

/// 可取消的等待：取消后 100ms 内返回，避免退避/429 冷却把取消拖住。
async fn sleep_cancellable(duration: Duration, cancel: Option<&AtomicBool>) -> AppResult<()> {
    let step = Duration::from_millis(100);
    let mut left = duration;
    while left > Duration::ZERO {
        if cancel.is_some_and(|flag| flag.load(Ordering::Relaxed)) {
            return Err(AppError::cancelled());
        }
        let chunk = left.min(step);
        tokio::time::sleep(chunk).await;
        left -= chunk;
    }
    Ok(())
}

/// 避开 `rand` 依赖：用纳秒低位做 0–250ms 抖动。
fn jitter_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.subsec_nanos() as u64 % 251)
        .unwrap_or(0)
}

/// 429 的 `Retry-After`（只处理秒数形式，HTTP-date 形式罕见）。
fn retry_after(response: &reqwest::Response) -> Option<Duration> {
    response
        .headers()
        .get(reqwest::header::RETRY_AFTER)?
        .to_str()
        .ok()?
        .trim()
        .parse::<u64>()
        .ok()
        .map(Duration::from_secs)
}

impl PixivClient {
    /// 动图的 zip 地址与逐帧延时（App API `/v1/ugoira/metadata`）。
    pub async fn ugoira_metadata(&self, pixiv_id: i64) -> AppResult<UgoiraMeta> {
        self.throttle_api().await;
        let token = self.access_token().await?;
        let response = self
            .http
            .get(format!("{API_BASE}/v1/ugoira/metadata"))
            .query(&[("illust_id", pixiv_id.to_string())])
            .bearer_auth(token)
            .header("Referer", "https://app-api.pixiv.net/")
            .send()
            .await
            .map_err(|error| AppError::internal(format!("请求动图信息失败：{error}")))?;

        if !response.status().is_success() {
            return Err(AppError::internal(format!(
                "动图信息返回 HTTP {}",
                response.status().as_u16()
            )));
        }

        let body: Value = response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("动图信息无法解析：{error}")))?;
        let meta = body
            .get("ugoira_metadata")
            .ok_or_else(|| AppError::internal("动图信息为空"))?;

        let zip_url = meta
            .pointer("/zip_urls/medium")
            .and_then(Value::as_str)
            .or_else(|| meta.pointer("/zip_urls/original").and_then(Value::as_str))
            .ok_or_else(|| AppError::internal("动图信息里没有 zip 地址"))?;

        let frames = meta
            .get("frames")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| {
                        Some(UgoiraFrame {
                            file: item.get("file")?.as_str()?.to_string(),
                            delay: item.get("delay").and_then(Value::as_u64).unwrap_or(30),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();

        Ok(UgoiraMeta {
            zip_url: zip_url.to_string(),
            frames,
        })
    }

    /// `/v1/search/user`：本地作者搜不到时，去 Pixiv 上找作者。
    pub async fn search_users(&self, word: &str) -> AppResult<Vec<RemoteAuthor>> {
        let word = word.trim();
        if word.is_empty() {
            return Ok(Vec::new());
        }
        self.throttle_api().await;
        let token = self.access_token().await?;
        let response = self
            .http
            .get(format!("{API_BASE}/v1/search/user"))
            .query(&[
                ("word", word),
                ("sort", "date_desc"),
                ("filter", "for_android"),
            ])
            .bearer_auth(token)
            .header("Referer", "https://app-api.pixiv.net/")
            .send()
            .await
            .map_err(|error| AppError::new("NETWORK", format!("无法连接 Pixiv：{error}")))?;

        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            return Err(AppError::new(
                "AUTH",
                "Pixiv 认证失效，请在设置里更新 Refresh Token",
            ));
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            self.note_rate_limited();
            return Err(AppError::internal("Pixiv 接口限流（HTTP 429），已自动降速"));
        }
        if !status.is_success() {
            return Err(AppError::internal(format!(
                "Pixiv 作者搜索返回 HTTP {}",
                status.as_u16()
            )));
        }

        let body: Value = response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("作者搜索响应无法解析：{error}")))?;
        let previews = body
            .get("user_previews")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        Ok(previews
            .iter()
            .filter_map(|preview| {
                let user = preview.get("user")?;
                Some(RemoteAuthor {
                    pixiv_user_id: user.get("id").and_then(Value::as_i64)?,
                    name: user
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    account: user
                        .get("account")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    avatar: user
                        .pointer("/profile_image_urls/medium")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                })
            })
            .collect())
    }

    /// `/v1/search/illust`：本地作品搜不到时，去 Pixiv 上找工作。
    pub async fn search_illusts(&self, word: &str) -> AppResult<Vec<RemoteIllust>> {
        let word = word.trim();
        if word.is_empty() {
            return Ok(Vec::new());
        }
        self.throttle_api().await;
        let token = self.access_token().await?;
        let response = self
            .http
            .get(format!("{API_BASE}/v1/search/illust"))
            .query(&[
                ("word", word),
                ("search_target", "partial_match_for_tags"),
                ("sort", "date_desc"),
                ("filter", "for_android"),
            ])
            .bearer_auth(token)
            .header("Referer", "https://app-api.pixiv.net/")
            .send()
            .await
            .map_err(|error| AppError::new("NETWORK", format!("无法连接 Pixiv：{error}")))?;

        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            return Err(AppError::new(
                "AUTH",
                "Pixiv 认证失效，请在设置里更新 Refresh Token",
            ));
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            self.note_rate_limited();
            return Err(AppError::internal("Pixiv 接口限流（HTTP 429），已自动降速"));
        }
        if !status.is_success() {
            return Err(AppError::internal(format!(
                "Pixiv 作品搜索返回 HTTP {}",
                status.as_u16()
            )));
        }

        let body: Value = response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("作品搜索响应无法解析：{error}")))?;
        let illusts = body
            .get("illusts")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        Ok(illusts
            .iter()
            .filter_map(|illust| {
                Some(RemoteIllust {
                    pixiv_id: illust.get("id").and_then(Value::as_i64)?,
                    title: illust
                        .get("title")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    author_name: illust
                        .pointer("/user/name")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    author_id: illust.pointer("/user/id").and_then(Value::as_i64).unwrap_or(0),
                    page_count: illust.get("page_count").and_then(Value::as_i64).unwrap_or(1),
                })
            })
            .collect())
    }

    /// `/v1/user/detail` 的响应体。头像、昵称都从这里取。
    async fn user_detail_body(&self, user_id: i64) -> AppResult<Value> {
        self.throttle_api().await;
        let token = self.access_token().await?;
        let response = self
            .http
            .get(format!("{API_BASE}/v1/user/detail"))
            .query(&[("user_id", user_id.to_string())])
            .bearer_auth(token)
            .header("Referer", "https://app-api.pixiv.net/")
            .send()
            .await
            .map_err(|error| AppError::internal(format!("请求作者信息失败：{error}")))?;

        if !response.status().is_success() {
            return Err(AppError::internal(format!(
                "作者信息返回 HTTP {}",
                response.status().as_u16()
            )));
        }

        response
            .json()
            .await
            .map_err(|error| AppError::internal(format!("作者信息无法解析：{error}")))
    }

    /// 取用户头像地址（`/v1/user/detail`）。
    pub async fn user_avatar_url(&self, user_id: i64) -> AppResult<Option<String>> {
        let body = self.user_detail_body(user_id).await?;
        Ok(body
            .pointer("/user/profile_image_urls/medium")
            .and_then(Value::as_str)
            .map(str::to_string))
    }

    /// 取用户昵称（订阅列表显示用）。
    pub async fn user_name(&self, user_id: i64) -> AppResult<Option<String>> {
        let body = self.user_detail_body(user_id).await?;
        Ok(body
            .pointer("/user/name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .map(str::to_string))
    }
}

/// 把列表接口（公开收藏 / 用户作品）的响应体拆成「条目 + 下一页地址」。
///
/// 两个接口的 `illusts` 结构与 `next_url` 字段完全一致，解析只写一份。
fn parse_illust_list(body: &Value) -> (Vec<IllustRef>, Option<String>) {
    let illusts = body
        .get("illusts")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    let pixiv_id = item.get("id").and_then(Value::as_i64)?;
                    Some(IllustRef {
                        pixiv_id,
                        title: item
                            .get("title")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_string(),
                        author_name: item
                            .pointer("/user/name")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_string(),
                        page_count: item.get("page_count").and_then(Value::as_i64).unwrap_or(1),
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    let next = body
        .get("next_url")
        .and_then(Value::as_str)
        .map(str::to_string);

    (illusts, next)
}

/// 图片镜像：只替换 Pixiv 官方图片主机，保留路径与查询串。
fn apply_mirror(url: &str, mirror: Option<&str>) -> String {
    let Some(mirror) = mirror.map(str::trim).filter(|value| !value.is_empty()) else {
        return url.to_string();
    };

    for host in ["i.pximg.net", "i-f.pximg.net", "s.pximg.net"] {
        let marker = format!("://{host}");
        if let Some((_, rest)) = url.split_once(&marker) {
            let mirror = mirror.trim_end_matches('/');
            return if mirror.contains("://") {
                format!("{mirror}{rest}")
            } else {
                format!("https://{mirror}{rest}")
            };
        }
    }
    url.to_string()
}

/// UTC 时间戳，格式与 pixivpy 一致：`%Y-%m-%dT%H:%M:%S+00:00`
fn utc_now_iso() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let (year, month, day, hour, minute, second) = civil_from_unix(secs);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}+00:00")
}

/// Unix 秒 → UTC 年月日时分秒（Howard Hinnant 的 civil_from_days 算法）。
fn civil_from_unix(secs: u64) -> (i64, u64, u64, u64, u64, u64) {
    let days = secs / 86_400;
    let rem = secs % 86_400;
    let hour = rem / 3_600;
    let minute = (rem % 3_600) / 60;
    let second = rem % 60;

    let z = days as i64 + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year + 1 } else { year };

    (year, month, day, hour, minute, second)
}

fn build_http(mode: &str, proxy: Option<&str>) -> AppResult<reqwest::Client> {
    build_http_with_ua(mode, proxy, USER_AGENT, true)
}

/// 供更新检查等模块复用：代理规则一致，只换 User-Agent。
///
/// `follow_redirects` 为 false 时不自动跟随 3xx：更新检查要读 `Location`
/// 里的 Release tag，跟随之后就只剩页面 HTML 了。
pub(crate) fn build_http_with_ua(
    mode: &str,
    proxy: Option<&str>,
    user_agent: &str,
    follow_redirects: bool,
) -> AppResult<reqwest::Client> {
    let redirect = if follow_redirects {
        reqwest::redirect::Policy::default()
    } else {
        reqwest::redirect::Policy::none()
    };
    let mut builder = reqwest::Client::builder()
        .user_agent(user_agent)
        .timeout(REQUEST_TIMEOUT)
        .referer(true)
        .redirect(redirect);

    match mode {
        "proxy" => {
            let address = proxy.unwrap_or("").trim();
            if address.is_empty() {
                return Err(AppError::invalid("代理模式需要在设置里填写代理地址"));
            }
            let proxy = reqwest::Proxy::all(address)
                .map_err(|error| AppError::invalid(format!("代理地址无效：{error}")))?;
            builder = builder.proxy(proxy);
        }
        "system" => {
            // 环境变量优先，其次读 Windows「Internet 选项」里的系统代理
            if let Some(address) = system_proxy() {
                let proxy = reqwest::Proxy::all(&address)
                    .map_err(|error| AppError::invalid(format!("系统代理地址无效：{error}")))?;
                builder = builder.proxy(proxy);
            }
        }
        _ => {}
    }

    builder
        .build()
        .map_err(|error| AppError::internal(format!("初始化网络客户端失败：{error}")))
}

/// 系统代理：先看环境变量，再看 Windows 注册表里的「Internet 选项」。
fn system_proxy() -> Option<String> {
    env_proxy().or_else(registry_proxy)
}

fn env_proxy() -> Option<String> {
    for key in [
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ] {
        if let Ok(value) = std::env::var(key) {
            let value = value.trim().to_string();
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

/// 启动子进程时隐藏控制台窗口。
///
/// GUI 程序（自身没有控制台）调用 console 程序（如 reg.exe）时，系统会新开一个黑框；
/// CREATE_NO_WINDOW 可以避免这个闪烁。
#[cfg(target_os = "windows")]
fn quiet_command(program: &str) -> std::process::Command {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let mut command = std::process::Command::new(program);
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

#[cfg(target_os = "windows")]
fn registry_proxy() -> Option<String> {
    fn read(name: &str) -> Option<String> {
        let output = quiet_command("reg")
            .args([
                "query",
                r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                "/v",
                name,
            ])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        let line = text.lines().find(|line| line.contains(name))?;
        line.split_whitespace().last().map(str::to_string)
    }

    let enabled = read("ProxyEnable")?;
    let enabled = enabled.trim();
    if enabled != "0x1" && enabled != "1" {
        return None;
    }

    let server = read("ProxyServer")?.trim().to_string();
    if server.is_empty() {
        return None;
    }

    // ProxyServer 可能是 "http=127.0.0.1:7890;https=..." 的复合形式
    let address = server
        .split(';')
        .find_map(|part| {
            let (scheme, value) = part.trim().split_once('=')?;
            matches!(
                scheme.trim().to_ascii_lowercase().as_str(),
                "http" | "https"
            )
            .then(|| value.trim().to_string())
        })
        .unwrap_or_else(|| server.clone());

    if address.is_empty() {
        return None;
    }
    if address.contains("://") {
        Some(address)
    } else {
        Some(format!("http://{address}"))
    }
}

#[cfg(not(target_os = "windows"))]
fn registry_proxy() -> Option<String> {
    None
}

fn parse_illust(illust: &Value) -> IllustData {
    let tags = illust
        .get("tags")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    let name = item.get("name").and_then(Value::as_str)?.to_string();
                    let translated = item
                        .get("translated_name")
                        .and_then(Value::as_str)
                        .filter(|value| !value.is_empty())
                        .map(str::to_string);
                    Some(IllustTag {
                        name,
                        translated_name: translated,
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    // 多页取 meta_pages，单页取 meta_single_page —— 与旧项目一致
    let image_urls = {
        let pages = illust.get("meta_pages").and_then(Value::as_array);
        match pages {
            Some(items) if !items.is_empty() => items
                .iter()
                .filter_map(|item| {
                    item.pointer("/image_urls/original")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .collect(),
            _ => illust
                .pointer("/meta_single_page/original_image_url")
                .and_then(Value::as_str)
                .map(|url| vec![url.to_string()])
                .unwrap_or_default(),
        }
    };

    IllustData {
        pixiv_id: illust.get("id").and_then(Value::as_i64).unwrap_or(0),
        title: illust
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        description: illust
            .get("caption")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        author_id: illust.pointer("/user/id").and_then(Value::as_i64).unwrap_or(0),
        author_name: illust
            .pointer("/user/name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        author_avatar: illust
            .pointer("/user/profile_image_urls/medium")
            .and_then(Value::as_str)
            .map(str::to_string),
        create_date: illust
            .get("create_date")
            .and_then(Value::as_str)
            .map(str::to_string),
        page_count: illust
            .get("page_count")
            .and_then(Value::as_i64)
            .unwrap_or(1),
        visible: illust.get("visible").and_then(Value::as_bool).unwrap_or(true),
        width: illust.get("width").and_then(Value::as_i64),
        height: illust.get("height").and_then(Value::as_i64),
        ai_type: illust.get("illust_ai_type").and_then(Value::as_i64).unwrap_or(1),
        tags,
        image_urls,
        is_ugoira: illust
            .get("illust_type")
            .and_then(Value::as_i64)
            .map(|value| value == 2)
            .unwrap_or(false)
            || illust.get("type").and_then(Value::as_str) == Some("ugoira"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_illust_list_with_next_url() {
        // /v1/user/illusts 与 /v1/user/bookmarks/illust 共用同一套解析
        let body: Value = serde_json::from_str(
            r#"{
                "illusts": [
                    {
                        "id": 112290978,
                        "title": "第一张",
                        "page_count": 2,
                        "user": { "name": "作者甲" }
                    },
                    { "id": 112290979, "title": "第二张" }
                ],
                "next_url": "https://app-api.pixiv.net/v1/user/illusts?user_id=1&offset=30"
            }"#,
        )
        .unwrap();

        let (items, next) = parse_illust_list(&body);
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].pixiv_id, 112290978);
        assert_eq!(items[0].author_name, "作者甲");
        assert_eq!(items[0].page_count, 2);
        assert_eq!(items[1].page_count, 1, "缺失 page_count 按单页处理");
        assert!(next.unwrap().contains("offset=30"));
    }

    #[test]
    fn parses_empty_illust_list() {
        let body: Value = serde_json::from_str(r#"{ "illusts": [] }"#).unwrap();
        let (items, next) = parse_illust_list(&body);
        assert!(items.is_empty());
        assert!(next.is_none());
    }

    #[test]
    fn formats_utc_timestamp_like_pixivpy() {
        assert_eq!(civil_from_unix(0), (1970, 1, 1, 0, 0, 0));
        assert_eq!(civil_from_unix(1_700_000_000), (2023, 11, 14, 22, 13, 20));
    }

    #[test]
    fn parses_multi_page_illust() {
        let value: Value = serde_json::from_str(
            r#"{
                "id": 112290978,
                "title": "测试作品",
                "caption": "<p>描述</p>",
                "create_date": "2024-01-02T03:04:05+09:00",
                "page_count": 2,
                "width": 1200,
                "height": 800,
                "illust_ai_type": 2,
                "user": {
                    "id": 12345,
                    "name": "老鼠公爵",
                    "profile_image_urls": { "medium": "https://i.pximg.net/avatar.jpg" }
                },
                "tags": [
                    { "name": "R-18", "translated_name": "" },
                    { "name": "オリジナル", "translated_name": "原创" }
                ],
                "meta_pages": [
                    { "image_urls": { "original": "https://i.pximg.net/a_p0.jpg" } },
                    { "image_urls": { "original": "https://i.pximg.net/a_p1.jpg" } }
                ]
            }"#,
        )
        .unwrap();

        let data = parse_illust(&value);
        assert_eq!(data.pixiv_id, 112290978);
        assert_eq!(data.author_id, 12345);
        assert_eq!(data.author_name, "老鼠公爵");
        assert_eq!(data.ai_type, 2);
        assert_eq!(data.page_count, 2);
        assert_eq!(data.image_urls.len(), 2);
        assert_eq!(data.tags.len(), 2);
        // 空 translated_name 会被丢掉，避免把空串写进库
        assert_eq!(data.tags[0].name, "R-18");
        assert!(data.tags[0].translated_name.is_none());
        assert_eq!(data.tags[1].translated_name.as_deref(), Some("原创"));
    }

    #[test]
    fn parses_single_page_illust_without_optional_fields() {
        let value: Value = serde_json::from_str(
            r#"{
                "id": 500,
                "title": "单页",
                "meta_single_page": { "original_image_url": "https://i.pximg.net/b.jpg" }
            }"#,
        )
        .unwrap();

        let data = parse_illust(&value);
        assert_eq!(data.image_urls, vec!["https://i.pximg.net/b.jpg"]);
        assert_eq!(data.page_count, 1);
        assert_eq!(data.ai_type, 1, "缺失 ai_type 按非 AI 处理");
        assert!(data.tags.is_empty());
        assert!(data.width.is_none());
    }
}