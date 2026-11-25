use rusqlite::{params, Connection};

use crate::commands::download;
use crate::error::{AppError, AppResult};
use crate::models::TagCombo;

fn auto_name(tags: &[String]) -> String {
    let mut name = tags
        .iter()
        .take(2)
        .map(|tag| format!("#{tag}"))
        .collect::<Vec<_>>()
        .join(" ");
    if tags.len() > 2 {
        name.push_str(&format!(" 等 {} 个", tags.len()));
    }
    clamp_name(&name)
}

/// 组合名只留 30 个字符，避免前端一行被撑开。
fn clamp_name(name: &str) -> String {
    let mut chars = name.chars();
    let head: String = chars.by_ref().take(30).collect();
    if chars.next().is_some() {
        format!("{head}…")
    } else {
        head
    }
}

fn clean_tags(tags: Vec<String>) -> Vec<String> {
    let mut out = Vec::new();
    for tag in tags {
        let tag = tag.trim().to_string();
        if tag.is_empty() || out.contains(&tag) {
            continue;
        }
        out.push(tag);
    }
    out
}

fn combo_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<TagCombo> {
    let tags_json: String = row.get(2)?;
    Ok(TagCombo {
        id: row.get(0)?,
        name: row.get(1)?,
        tags: serde_json::from_str(&tags_json).unwrap_or_default(),
        pinned: row.get::<_, i64>(3)? != 0,
    })
}

fn combo_by_id(conn: &Connection, id: i64) -> AppResult<TagCombo> {
    conn.query_row(
        "SELECT id, name, tags, pinned FROM tag_combos WHERE id = ?1",
        [id],
        combo_from_row,
    )
    .map_err(|_| AppError::not_found("标签组合不存在"))
}

#[tauri::command]
pub fn list_tag_combos() -> AppResult<Vec<TagCombo>> {
    let conn = download::open_library()?;
    let mut stmt = conn.prepare(
        "SELECT id, name, tags, pinned FROM tag_combos
          ORDER BY pinned DESC, updated_at DESC, id DESC",
    )?;
    let rows = stmt.query_map([], combo_from_row)?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

#[tauri::command]
pub fn save_tag_combo(name: Option<String>, tags: Vec<String>) -> AppResult<TagCombo> {
    let tags = clean_tags(tags);
    if tags.is_empty() {
        return Err(AppError::invalid("先勾选至少一个标签"));
    }
    let name = name
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| auto_name(&tags));
    let name = clamp_name(&name);
    let tags_json = serde_json::to_string(&tags)
        .map_err(|error| AppError::internal(format!("标签组合无法序列化：{error}")))?;

    let conn = download::open_library()?;
    conn.execute(
        "INSERT INTO tag_combos (name, tags, pinned) VALUES (?1, ?2, 0)",
        params![name, tags_json],
    )?;
    combo_by_id(&conn, conn.last_insert_rowid())
}

#[tauri::command]
pub fn rename_tag_combo(id: i64, name: String) -> AppResult<TagCombo> {
    let name = clamp_name(name.trim());
    if name.is_empty() {
        return Err(AppError::invalid("组合名不能为空"));
    }
    let conn = download::open_library()?;
    let changed = conn.execute(
        "UPDATE tag_combos SET name = ?1, updated_at = datetime('now') WHERE id = ?2",
        params![name, id],
    )?;
    if changed == 0 {
        return Err(AppError::not_found("标签组合不存在"));
    }
    combo_by_id(&conn, id)
}

#[tauri::command]
pub fn delete_tag_combo(id: i64) -> AppResult<bool> {
    let conn = download::open_library()?;
    let changed = conn.execute("DELETE FROM tag_combos WHERE id = ?1", [id])?;
    Ok(changed > 0)
}

#[tauri::command]
pub fn toggle_tag_combo_pin(id: i64) -> AppResult<TagCombo> {
    let conn = download::open_library()?;
    let changed = conn.execute(
        "UPDATE tag_combos SET pinned = 1 - pinned, updated_at = datetime('now') WHERE id = ?1",
        [id],
    )?;
    if changed == 0 {
        return Err(AppError::not_found("标签组合不存在"));
    }
    combo_by_id(&conn, id)
}
