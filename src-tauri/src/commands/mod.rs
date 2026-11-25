pub mod artworks;
pub mod browse;
pub mod download;
pub mod favorites;
pub mod migration;
pub mod scan;
pub mod scanner;
pub mod settings;
pub mod subs;
pub mod sync;
pub mod task;
pub mod thumbnails;
pub mod update;

use std::sync::Mutex;

use tauri::State;

/// 启动迁移留下的提示：旧库被修掉 `uidx_images_sha256` 时非空，前端取一次即清空。
#[derive(Default)]
pub struct StartupNotice(pub Mutex<Option<String>>);

#[tauri::command]
pub fn take_startup_notice(state: State<'_, StartupNotice>) -> Option<String> {
    state.0.lock().ok().and_then(|mut guard| guard.take())
}