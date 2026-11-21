use rusqlite::Connection;

use crate::db;
use crate::error::{AppError, AppResult};
use crate::models::{BatchResult, FavoriteRow};
use crate::platform::paths;

fn open_library() -> AppResult<Connection> {
    let path = paths::db_path()?;
    if !path.is_file() {
        return Err(AppError::not_found("数据库尚未初始化，请先导入旧数据或扫描目录"));
    }
    db::open(&path)
}

pub fn list_with(conn: &Connection) -> AppResult<Vec<FavoriteRow>> {
    let mut stmt = conn.prepare(
        "SELECT f.id, f.name, COALESCE(f.create_date, ''),
                (SELECT COUNT(*) FROM favorite_artworks fa WHERE fa.favorite_id = f.id)
         FROM favorites f
         ORDER BY f.name",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(FavoriteRow {
            id: row.get(0)?,
            name: row.get(1)?,
            create_date: row.get(2)?,
            artwork_count: row.get(3)?,
        })
    })?;

    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn row_of(conn: &Connection, favorite_id: i64) -> AppResult<FavoriteRow> {
    conn.query_row(
        "SELECT f.id, f.name, COALESCE(f.create_date, ''),
                (SELECT COUNT(*) FROM favorite_artworks fa WHERE fa.favorite_id = f.id)
         FROM favorites f WHERE f.id = ?1",
        [favorite_id],
        |row| {
            Ok(FavoriteRow {
                id: row.get(0)?,
                name: row.get(1)?,
                create_date: row.get(2)?,
                artwork_count: row.get(3)?,
            })
        },
    )
    .map_err(AppError::db)
}

pub fn create_with(conn: &Connection, name: &str) -> AppResult<FavoriteRow> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err(AppError::invalid("本地收藏名称不能为空"));
    }

    let exists: Option<i64> = conn
        .query_row("SELECT id FROM favorites WHERE name = ?1", [trimmed], |row| {
            row.get(0)
        })
        .ok();

    if let Some(id) = exists {
        return row_of(conn, id);
    }

    conn.execute("INSERT INTO favorites (name) VALUES (?1)", [trimmed])?;
    let id = conn.last_insert_rowid();
    row_of(conn, id)
}

pub fn add_with(conn: &Connection, favorite_id: i64, artwork_ids: &[i64]) -> AppResult<BatchResult> {
    if !favorite_id_exists(conn, favorite_id)? {
        return Err(AppError::not_found(format!("本地收藏 {favorite_id} 不存在")));
    }

    let tx = conn.unchecked_transaction()?;
    let mut ok = 0i64;
    let mut failed = 0i64;
    let mut errors = Vec::new();

    for artwork_id in artwork_ids {
        // 已在该本地收藏里不算错误，重复收藏是幂等操作
        match tx.execute(
            "INSERT OR IGNORE INTO favorite_artworks (favorite_id, artwork_id) VALUES (?1, ?2)",
            [favorite_id, *artwork_id],
        ) {
            Ok(_) => ok += 1,
            Err(error) => {
                failed += 1;
                errors.push(format!("#{artwork_id}: {error}"));
            }
        }
    }
    tx.commit()?;

    Ok(BatchResult { ok, failed, errors })
}

pub fn remove_with(
    conn: &Connection,
    favorite_id: i64,
    artwork_ids: &[i64],
) -> AppResult<BatchResult> {
    let tx = conn.unchecked_transaction()?;
    let mut ok = 0i64;
    let mut failed = 0i64;
    let mut errors = Vec::new();

    for artwork_id in artwork_ids {
        match tx.execute(
            "DELETE FROM favorite_artworks WHERE favorite_id = ?1 AND artwork_id = ?2",
            [favorite_id, *artwork_id],
        ) {
            Ok(_) => ok += 1,
            Err(error) => {
                failed += 1;
                errors.push(format!("#{artwork_id}: {error}"));
            }
        }
    }
    tx.commit()?;

    Ok(BatchResult { ok, failed, errors })
}

pub fn delete_with(conn: &Connection, favorite_id: i64) -> AppResult<BatchResult> {
    // 只删本地收藏本身，作品记录不受影响（favorite_artworks 由 CASCADE 清理）
    let affected = conn.execute("DELETE FROM favorites WHERE id = ?1", [favorite_id])?;
    if affected == 0 {
        return Err(AppError::not_found(format!("本地收藏 {favorite_id} 不存在")));
    }
    Ok(BatchResult {
        ok: 1,
        failed: 0,
        errors: Vec::new(),
    })
}

fn favorite_id_exists(conn: &Connection, favorite_id: i64) -> AppResult<bool> {
    let found: Option<i64> = conn
        .query_row("SELECT id FROM favorites WHERE id = ?1", [favorite_id], |row| {
            row.get(0)
        })
        .ok();
    Ok(found.is_some())
}

#[tauri::command]
pub fn list_favorites() -> AppResult<Vec<FavoriteRow>> {
    let conn = open_library()?;
    list_with(&conn)
}

#[tauri::command]
pub fn create_favorite(name: String) -> AppResult<FavoriteRow> {
    let conn = open_library()?;
    create_with(&conn, &name)
}

#[tauri::command]
pub fn delete_favorite(favorite_id: i64) -> AppResult<BatchResult> {
    let conn = open_library()?;
    delete_with(&conn, favorite_id)
}

#[tauri::command]
pub fn add_to_favorite(favorite_id: i64, artwork_ids: Vec<i64>) -> AppResult<BatchResult> {
    let conn = open_library()?;
    add_with(&conn, favorite_id, &artwork_ids)
}

#[tauri::command]
pub fn remove_from_favorite(favorite_id: i64, artwork_id: i64) -> AppResult<BatchResult> {
    let conn = open_library()?;
    remove_with(&conn, favorite_id, &[artwork_id])
}

#[tauri::command]
pub fn batch_remove_from_favorite(
    favorite_id: i64,
    artwork_ids: Vec<i64>,
) -> AppResult<BatchResult> {
    let conn = open_library()?;
    remove_with(&conn, favorite_id, &artwork_ids)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn memory_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(crate::db::schema::SCHEMA_SQL).unwrap();
        conn.execute("INSERT INTO artworks (pixiv_id, title) VALUES (1, 'a')", [])
            .unwrap();
        conn.execute("INSERT INTO artworks (pixiv_id, title) VALUES (2, 'b')", [])
            .unwrap();
        conn
    }

    #[test]
    fn create_is_idempotent_and_lists_counts() {
        let conn = memory_db();
        let first = create_with(&conn, "风景").unwrap();
        let second = create_with(&conn, "风景").unwrap();
        assert_eq!(first.id, second.id, "同名本地收藏应复用而不是重复创建");

        add_with(&conn, first.id, &[1, 2]).unwrap();
        let listed = list_with(&conn).unwrap();
        let row = listed.iter().find(|f| f.id == first.id).unwrap();
        assert_eq!(row.artwork_count, 2);
    }

    #[test]
    fn add_is_idempotent() {
        let conn = memory_db();
        let fav = create_with(&conn, "A").unwrap();
        add_with(&conn, fav.id, &[1]).unwrap();
        add_with(&conn, fav.id, &[1]).unwrap();

        let listed = list_with(&conn).unwrap();
        assert_eq!(listed[0].artwork_count, 1, "同一作品重复加入不应重复计数");
    }

    #[test]
    fn remove_and_delete_behave() {
        let conn = memory_db();
        let fav = create_with(&conn, "B").unwrap();
        add_with(&conn, fav.id, &[1, 2]).unwrap();

        remove_with(&conn, fav.id, &[1]).unwrap();
        assert_eq!(list_with(&conn).unwrap()[0].artwork_count, 1);

        delete_with(&conn, fav.id).unwrap();
        assert!(list_with(&conn).unwrap().is_empty());

        // 作品本身必须还在
        let artworks: i64 = conn
            .query_row("SELECT COUNT(*) FROM artworks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(artworks, 2);
    }

    #[test]
    fn rejects_blank_name_and_missing_favorite() {
        let conn = memory_db();
        assert_eq!(create_with(&conn, "  ").unwrap_err().code, "INVALID_INPUT");
        assert_eq!(add_with(&conn, 999, &[1]).unwrap_err().code, "NOT_FOUND");
    }
}