//! 一次性工具：把本机旧项目数据导入新数据目录，用于界面验收。
//!
//! 用法（在 target 里编出来的 exe 是 Low 完整性，需复制到普通目录再运行）：
//! ```powershell
//! cargo build --manifest-path src-tauri/Cargo.toml --example import_legacy
//! Copy-Item src-tauri\target\debug\examples\import_legacy.exe "D:\Little Tool\pa-medium-test\" -Force
//! & "D:\Little Tool\pa-medium-test\import_legacy.exe"
//! ```

fn main() {
    let legacy = std::path::Path::new(r"D:\Little Tool\Pixiv-Archive");
    let appdata = std::env::var("APPDATA").expect("APPDATA 未设置");
    let data = std::path::PathBuf::from(appdata).join("pixiv_archive_desktop");

    if !legacy.join("archive.db").is_file() {
        eprintln!("旧库不存在：{}", legacy.join("archive.db").display());
        std::process::exit(1);
    }

    match pixiv_archive_lib::migration::import_legacy(legacy, &data, true, true) {
        Ok(report) => {
            println!("导入完成 → {}", data.display());
            println!("  作品 {} 件 / 图片 {} 张 / 缩略图 {} 个", report.artworks, report.images, report.thumbs_copied);
            if let Some(backup) = report.backed_up_to {
                println!("  旧库已备份到 {backup}");
            }
        }
        Err(error) => {
            eprintln!("导入失败：{error}");
            std::process::exit(1);
        }
    }
}