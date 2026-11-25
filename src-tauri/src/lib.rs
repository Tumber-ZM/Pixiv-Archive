mod commands;
mod db;
mod error;
pub mod migration;
mod models;
mod net;
mod platform;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // WebView2 默认按应用标识共享用户数据目录，同一时间只允许一个实例使用；
    // 旧实例还在跑时，新 exe 会因 0x800700AA（资源在使用中）创建 WebView 失败而闪退。
    // 按数据目录隔离后，debug/release 以及多份拷贝可以各自独立。
    if let Ok(data) = platform::paths::data_dir() {
        let webview = data.join("webview2");
        let _ = std::fs::create_dir_all(&webview);
        // SAFETY: 在 Tauri 启动前调用，此时还没有其他线程读环境变量，不存在并发冲突。
        unsafe {
            std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &webview);
        }
    }

    tauri::Builder::default()
        // 单实例必须最先注册：第二次启动时聚焦已有窗口，然后自己退出
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_main(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .manage(commands::task::TaskCancel::default())
        .manage(commands::StartupNotice::default())
        .setup(|app| {
            // 便携数据目录：先把目录铺好，并把旧的 settings.json 复制过来一份，
            // 避免换到 exe 旁以后图片目录/代理/token 全部丢失（库不迁移）。
            let _ = platform::paths::ensure_dirs();
            seed_settings_from_appdata();

            // 老库升级：启动时补跑一次幂等迁移（只建表/补列/丢掉旧索引，
            // 不做全库去重）。修掉旧索引时留个提示，让用户跑一次「校验图片」。
            if let Ok(path) = platform::paths::db_path() {
                if path.is_file() {
                    if let Ok((_, legacy)) = db::ensure_ready(&path) {
                        if legacy {
                            if let Ok(mut guard) = app.state::<commands::StartupNotice>().0.lock() {
                                *guard = Some(
                                    "检测到旧版本数据库：已移除「按图片内容去重」的旧索引。\
                                     旧版本曾因此丢掉跨作品共用的图片，建议运行一次「校验图片」补齐。"
                                        .to_string(),
                                );
                            }
                        }
                    }
                }
            }

            // 启动时把数据目录和用户配置的图片目录加进 asset 协议白名单
            allow_image_dir(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::artworks::batch_delete_artworks,
            commands::artworks::delete_artwork,
            commands::artworks::get_artwork,
            commands::artworks::get_stats,
            commands::artworks::list_artworks,
            commands::artworks::search_artworks,
            commands::browse::add_bookmark_sub,
            commands::browse::get_author_avatar,
            commands::browse::list_authors,
            commands::browse::list_bookmark_subs,
            commands::browse::list_tags,
            commands::browse::preview_bookmark_sub,
            commands::browse::refresh_author_avatars,
            commands::browse::remove_bookmark_sub,
            commands::browse::toggle_bookmark_sub_auto,
            commands::download::regenerate_thumbnails,
            commands::favorites::add_to_favorite,
            commands::favorites::batch_remove_from_favorite,
            commands::favorites::create_favorite,
            commands::favorites::delete_favorite,
            commands::favorites::list_favorites,
            commands::favorites::remove_from_favorite,
            commands::migration::import_legacy,
            commands::migration::data_sizes,
            commands::migration::export_data_bundle,
            commands::migration::import_data_bundle,
            commands::migration::relink_images,
            commands::scan::cancel_scan,
            commands::scan::data_directory,
            commands::scan::default_scan_root,
            commands::scan::open_in_folder,
            commands::scan::pick_directory,
            commands::scan::pick_file,
            commands::scan::scan_directory,
            commands::scan::scan_status,
            commands::settings::get_settings,
            commands::settings::save_settings,
            commands::subs::check_all_bookmark_subs,
            commands::subs::check_bookmark_sub,
            commands::sync::sync_artworks,
            commands::sync::sync_metadata,
            commands::task::cancel_task,
            commands::task::task_running,
            commands::task::task_kinds,
            commands::update::app_info,
            commands::take_startup_notice,
            commands::subs::repair_incomplete_images,
            commands::update::check_update,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn focus_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// 把数据目录和设置里的图片目录写进 asset 协议白名单。
///
/// tauri.conf.json 只静态放行了 `%APPDATA%`；便携模式下数据目录在 exe 旁边，
/// 原图又在用户自己选的图片目录里，都需要运行时补授权，否则显示空白。
fn allow_image_dir(app: &tauri::AppHandle) {
    if let Ok(data) = platform::paths::data_dir() {
        let _ = app.asset_protocol_scope().allow_directory(&data, true);
    }
    if let Ok(Some(dir)) = commands::settings::image_source_dir() {
        let _ = app.asset_protocol_scope().allow_directory(dir, true);
    }
}

/// 便携模式首次启动时，把旧 `%APPDATA%\pixiv_archive_desktop\settings.json`
/// 复制到 exe 旁的 data 目录：只带配置（图片目录/代理/token），不搬库。
fn seed_settings_from_appdata() {
    let Ok(data) = platform::paths::data_dir() else {
        return;
    };
    let target = data.join("settings.json");
    if target.is_file() {
        return;
    }
    let Some(appdata) = std::env::var_os("APPDATA") else {
        return;
    };
    let source = std::path::PathBuf::from(appdata)
        .join(platform::paths::APP_DIR_NAME)
        .join("settings.json");
    if source.is_file() {
        let _ = std::fs::copy(&source, &target);
    }
}