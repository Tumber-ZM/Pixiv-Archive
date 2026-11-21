//! 列表查询的 SQL 构建器。
//!
//! R18 / AI / GIF 三态筛选必须在 SQL 里做，因为它们要参与分页计数
//! （`03-数据模型与旧库兼容.md` §五）。

use rusqlite::types::Value;

use crate::models::ListQuery;

pub const DEFAULT_PAGE_SIZE: u32 = 60;
pub const MAX_PAGE_SIZE: u32 = 200;

pub const R18_EXISTS: &str = "EXISTS (SELECT 1 FROM artwork_tags at JOIN tags t ON t.id = at.tag_id \
     WHERE at.artwork_id = a.id AND t.name IN ('R-18','R-18G'))";
pub const GIF_EXISTS: &str = "EXISTS (SELECT 1 FROM images i \
     WHERE i.artwork_id = a.id AND lower(i.path) LIKE '%.gif')";

/// 把 LIKE 通配符转义掉，否则用户输入的 `%` 会退化成全表匹配。
pub fn escape_like(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        if matches!(ch, '%' | '_' | '\\') {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

pub struct Filter {
    pub where_sql: String,
    pub params: Vec<Value>,
}

pub fn build_filter(query: &ListQuery) -> Filter {
    let mut clauses: Vec<String> = Vec::new();
    let mut params: Vec<Value> = Vec::new();

    // 本地图片（文件名里没有 Pixiv ID，入库时派生了负数 ID）默认不进画廊，
    // 也不进作者/标签/收藏夹视图；只有「本地作品」页显式传 scope=local
    match query.scope.as_deref() {
        Some("local") => clauses.push("a.pixiv_id < 0".to_string()),
        _ => clauses.push("a.pixiv_id > 0".to_string()),
    }

    if let Some(keyword) = query.q.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        let pattern = format!("%{}%", escape_like(keyword));
        // 关键词命中：标题 / 描述 / 作者名 / Pixiv ID / 标签原名 / 标签译名
        clauses.push(
            "(a.title LIKE ? ESCAPE '\\' \
             OR a.description LIKE ? ESCAPE '\\' \
             OR a.author_name LIKE ? ESCAPE '\\' \
             OR CAST(a.pixiv_id AS TEXT) LIKE ? ESCAPE '\\' \
             OR EXISTS (SELECT 1 FROM artwork_tags at JOIN tags t ON t.id = at.tag_id \
                        WHERE at.artwork_id = a.id \
                          AND (t.name LIKE ? ESCAPE '\\' OR t.translated_name LIKE ? ESCAPE '\\')))"
                .to_string(),
        );
        for _ in 0..6 {
            params.push(Value::Text(pattern.clone()));
        }
    }

    // 三态：all 不过滤 / hide 反向 / only 正向
    match query.r18.as_deref().unwrap_or("all") {
        "hide" => clauses.push(format!("NOT {R18_EXISTS}")),
        "only" => clauses.push(R18_EXISTS.to_string()),
        _ => {}
    }
    match query.ai.as_deref().unwrap_or("all") {
        "hide" => clauses.push("a.ai_type <> 2".to_string()),
        "only" => clauses.push("a.ai_type = 2".to_string()),
        _ => {}
    }
    match query.gif.as_deref().unwrap_or("all") {
        "hide" => clauses.push(format!("NOT {GIF_EXISTS}")),
        "only" => clauses.push(GIF_EXISTS.to_string()),
        _ => {}
    }

    if query.deleted_only.unwrap_or(false) {
        clauses.push("a.pixiv_status = 'deleted'".to_string());
    }
    if let Some(author_id) = query.author_id {
        clauses.push("a.author_id = ?".to_string());
        params.push(Value::Integer(author_id));
    }
    if let Some(tag) = query.tag.as_deref().filter(|t| !t.is_empty()) {
        clauses.push(
            "EXISTS (SELECT 1 FROM artwork_tags at JOIN tags t ON t.id = at.tag_id \
             WHERE at.artwork_id = a.id AND t.name = ?)"
                .to_string(),
        );
        params.push(Value::Text(tag.to_string()));
    }
    if let Some(favorite_id) = query.favorite_id {
        clauses.push(
            "EXISTS (SELECT 1 FROM favorite_artworks fa \
             WHERE fa.artwork_id = a.id AND fa.favorite_id = ?)"
                .to_string(),
        );
        params.push(Value::Integer(favorite_id));
    }

    let where_sql = if clauses.is_empty() {
        "1 = 1".to_string()
    } else {
        clauses.join(" AND ")
    };

    Filter { where_sql, params }
}

/// 排序白名单：字段名映射写死，绝不把前端传的字符串直接拼进 SQL。
/// 随机排序用同一个 seed 保证翻页顺序稳定。
pub fn order_by(query: &ListQuery) -> String {
    let direction = if query.order.as_deref() == Some("asc") {
        "ASC"
    } else {
        "DESC"
    };

    match query.sort.as_deref() {
        Some("id") => format!("ORDER BY a.id {direction}"),
        Some("first_seen") => format!("ORDER BY a.first_seen {direction}"),
        Some("create_date") => format!("ORDER BY a.create_date {direction}"),
        Some("title") => format!("ORDER BY a.title {direction}"),
        Some("pixiv_id") => format!("ORDER BY a.pixiv_id {direction}"),
        Some("random") => {
            // seed 取模，避免 a.id * seed * 1103515245 在 SQLite 里溢出成浮点后排序不稳定
            let seed = query.seed.unwrap_or(1).rem_euclid(1_000_000) + 1;
            format!("ORDER BY (a.id * {seed} * 1103515245 + 12345) % 2147483647 {direction}")
        }
        _ => format!("ORDER BY a.first_seen {direction}"),
    }
}

/// page 从 1 开始；page_size 默认 60、上限 200。
pub fn page_of(query: &ListQuery) -> (u32, u32) {
    let page = query.page.unwrap_or(1).max(1);
    let page_size = query
        .page_size
        .unwrap_or(DEFAULT_PAGE_SIZE)
        .clamp(1, MAX_PAGE_SIZE);
    (page, page_size)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tri(r18: &str, ai: &str, gif: &str) -> ListQuery {
        ListQuery {
            r18: Some(r18.to_string()),
            ai: Some(ai.to_string()),
            gif: Some(gif.to_string()),
            ..Default::default()
        }
    }

    /// 三态的 27 种穷举组合，每种断言 R18 / AI / GIF 三个子句的形态。
    #[test]
    fn tri_state_combinations_are_exhaustive() {
        let states = ["all", "hide", "only"];
        let mut checked = 0;

        for r18 in states {
            for ai in states {
                for gif in states {
                    let filter = build_filter(&tri(r18, ai, gif));

                    match r18 {
                        "hide" => assert!(
                            filter.where_sql.contains(&format!("NOT {R18_EXISTS}")),
                            "R18=hide 缺少反向子句"
                        ),
                        "only" => assert!(
                            filter.where_sql.contains(R18_EXISTS),
                            "R18=only 缺少正向子句"
                        ),
                        _ => assert!(
                            !filter.where_sql.contains(R18_EXISTS),
                            "R18=all 不该出现子句"
                        ),
                    }

                    match ai {
                        "hide" => assert!(filter.where_sql.contains("a.ai_type <> 2")),
                        "only" => assert!(filter.where_sql.contains("a.ai_type = 2")),
                        _ => assert!(!filter.where_sql.contains("a.ai_type")),
                    }

                    match gif {
                        "hide" => assert!(
                            filter.where_sql.contains(&format!("NOT {GIF_EXISTS}")),
                            "GIF=hide 缺少反向子句"
                        ),
                        "only" => assert!(
                            filter.where_sql.contains(GIF_EXISTS),
                            "GIF=only 缺少正向子句"
                        ),
                        _ => assert!(
                            !filter.where_sql.contains(GIF_EXISTS),
                            "GIF=all 不该出现子句"
                        ),
                    }

                    checked += 1;
                }
            }
        }

        assert_eq!(checked, 27);
    }

    #[test]
    fn all_filters_off_still_excludes_local_files() {
        let filter = build_filter(&ListQuery::default());
        assert_eq!(filter.where_sql, "a.pixiv_id > 0");
        assert!(filter.params.is_empty());
    }

    #[test]
    fn local_scope_only_returns_negative_ids() {
        let filter = build_filter(&ListQuery {
            scope: Some("local".to_string()),
            ..Default::default()
        });
        assert_eq!(filter.where_sql, "a.pixiv_id < 0");
    }

    #[test]
    fn like_wildcards_are_escaped() {
        let filter = build_filter(&ListQuery {
            q: Some("100%_a\\b".to_string()),
            ..Default::default()
        });
        let pattern = format!("%{}%", escape_like("100%_a\\b"));
        assert!(pattern.contains("100\\%\\_a\\\\b"));
        let bound = filter
            .params
            .iter()
            .filter_map(|v| match v {
                Value::Text(t) => Some(t.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(bound.len(), 6, "六个 LIKE 参数都要绑定");
        assert!(bound.iter().all(|t| *t == pattern.as_str()));
    }

    #[test]
    fn sort_whitelist_rejects_unknown_fields() {
        let order = order_by(&ListQuery {
            sort: Some("title; DROP TABLE artworks".to_string()),
            order: Some("asc".to_string()),
            ..Default::default()
        });
        assert_eq!(order, "ORDER BY a.first_seen ASC");
    }

    #[test]
    fn random_order_is_stable_for_same_seed() {
        let query = ListQuery {
            sort: Some("random".to_string()),
            seed: Some(42),
            ..Default::default()
        };
        let first = order_by(&query);
        assert_eq!(
            first,
            order_by(&query.clone()),
            "同一 seed 必须生成同一 ORDER BY"
        );

        let other = order_by(&ListQuery {
            sort: Some("random".to_string()),
            seed: Some(43),
            ..Default::default()
        });
        assert_ne!(first, other, "不同 seed 应生成不同排序");
    }

    #[test]
    fn page_size_is_clamped() {
        assert_eq!(page_of(&ListQuery::default()), (1, 60));
        assert_eq!(
            page_of(&ListQuery {
                page: Some(0),
                page_size: Some(9999),
                ..Default::default()
            }),
            (1, 200)
        );
    }
}