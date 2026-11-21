use std::path::PathBuf;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter, State};

use super::scanner::{self, ScanOptions, ScanReport};
use super::task::TaskCancel;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::platform::paths;

/// 进度事件名，前端监听同名事件
pub const PROGRESS_EVENT: &str = "scan-progress";
/// 进度节流间隔：扫描很快时没必要每张都发一次 IPC
const PROGRESS_THROTTLE: Duration = Duration::from_millis(100);

fn open_library() -> AppResult<rusqlite::Connection> {
    let path = paths::db_path()?;
    let data_dir = paths::data_dir()?;
    std::fs::create_dir_all(&data_dir)?;

    if !path.is_file() {
        // 首次扫描：建库并写入 schema
        db::init(&path)?;
        return db::open(&path);
    }

    let conn = db::open(&path)?;
    // 老库可能没有 images.path 索引：扫描按路径判重前先补上（幂等）
    let _ = conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_path ON images(path)",
        [],
    );
    Ok(conn)
}

/// 扫描目录入库，边扫边发 `scan-progress`。
///
/// 阻塞工作放在 `spawn_blocking` 里，避免 SQLite 与解码把 UI 线程占住；
/// 取消只需把共享标志置位，已入库的数据保留。
#[tauri::command]
pub async fn scan_directory(
    app: AppHandle,
    state: State<'_, TaskCancel>,
    path: String,
    recursive: bool,
    generate_thumbnails: bool,
) -> AppResult<ScanReport> {
    let Some(cancel) = state.begin("scan") else {
        return Err(AppError::invalid("已有任务在运行"));
    };

    let root = PathBuf::from(path);
    let handle = app.clone();

    let joined = tauri::async_runtime::spawn_blocking(move || -> AppResult<ScanReport> {
        let conn = open_library()?;
        let data_dir = paths::data_dir()?;

        let options = ScanOptions {
            root,
            recursive,
            generate_thumbnails,
        };

        let mut last_emit = Instant::now() - PROGRESS_THROTTLE;

        let result = scanner::scan(&conn, &data_dir, &options, &cancel, |progress| {
            if last_emit.elapsed() < PROGRESS_THROTTLE {
                return;
            }
            let _ = handle.emit(PROGRESS_EVENT, &progress);
            last_emit = Instant::now();
        });

        result
    })
    .await
    .map_err(|error| AppError::internal(format!("扫描任务异常退出: {error}")))?;

    // 无论成功失败都要释放槽位，否则后续任务永远被拒
    state.end("scan");

    joined
}

/// 在资源管理器里定位文件。
///
/// 用 `explorer /select,"path"`：它会打开父目录并选中目标文件。
/// 路径里如果带引号会破坏参数解析，先剔掉；文件不存在就直接报错，
/// 否则 explorer 会静默打开「此电脑」，用户以为按钮没生效。
#[tauri::command]
pub fn open_in_folder(path: String) -> AppResult<()> {
    let target = PathBuf::from(&path);
    if !target.is_file() {
        return Err(AppError::not_found(format!("文件不存在：{path}")));
    }

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;

        // 必须用 raw_arg 手动拼：`Command::arg` 会给含空格的参数自动加引号，
        // 变成 `"/select,C:\My Files\a.jpg"`，explorer 认不出这个开关，
        // 于是退回到默认目录（文档）——正是"只定位到文档文件夹"的症状。
        // raw_arg 不转义，生成的命令行是 `explorer /select,"C:\My Files\a.jpg"`。
        let cleaned = path.replace('"', "");
        std::process::Command::new("explorer")
            .raw_arg(format!("/select,\"{cleaned}\""))
            .spawn()
            .map_err(|error| AppError::internal(format!("调用资源管理器失败：{error}")))?;
        return Ok(());
    }

    #[cfg(not(target_os = "windows"))]
    {
        Err(AppError::internal("当前平台暂不支持在文件夹中定位"))
    }
}

/// 取消当前长任务（扫描 / 同步元数据 / 订阅下载通用）。
#[tauri::command]
pub fn cancel_scan(state: State<'_, TaskCancel>) -> AppResult<bool> {
    Ok(state.request_cancel())
}

/// 当前是否有长任务在跑，前端据此决定按钮状态
#[tauri::command]
pub fn scan_status(state: State<'_, TaskCancel>) -> AppResult<bool> {
    Ok(state.is_running())
}

/// 供前端确认应用运行时会落到哪个数据目录
#[tauri::command]
pub fn data_directory() -> AppResult<String> {
    Ok(paths::data_dir()?.to_string_lossy().to_string())
}

/// 扫描对话框打开时预填的目录：用户在设置里配置的那一个。
/// 没配置就返回 None，前端据此提示「先去设置图片目录」。
#[tauri::command]
pub fn default_scan_root() -> AppResult<Option<String>> {
    super::settings::image_source_dir()
}

/// 打开系统文件选择器（数据包导入用）。
#[tauri::command]
pub async fn pick_file(app: AppHandle) -> AppResult<Option<String>> {
    use tauri_plugin_dialog::DialogExt;

    let picked = app
        .dialog()
        .file()
        .add_filter("数据包", &["zip"])
        .blocking_pick_file();
    Ok(picked
        .and_then(|path| path.into_path().ok())
        .map(|path| path.to_string_lossy().to_string()))
}

/// 打开系统目录选择器。用 async 命令 + blocking 调用：blocking 版不能在
/// 主线程跑，async command 会落到 tokio 线程，正好满足。
#[tauri::command]
pub async fn pick_directory(app: AppHandle) -> AppResult<Option<String>> {
    use tauri_plugin_dialog::DialogExt;

    let picked = app.dialog().file().blocking_pick_folder();
    Ok(picked
        .and_then(|path| path.into_path().ok())
        .map(|path| path.to_string_lossy().to_string()))
}