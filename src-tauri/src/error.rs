use serde::Serialize;

/// 前后端统一的错误模型（`04-IPC契约.md`）。
/// 序列化成 `{ code, message }`，前端按 code 分流处理。
#[derive(Debug, Clone, Serialize)]
pub struct AppError {
    pub code: String,
    pub message: String,
}

impl AppError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new("NOT_FOUND", message)
    }

    /// 参数校验类错误，T04 的列表查询接入后使用。
#[allow(dead_code)]
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new("INVALID_INPUT", message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new("INTERNAL", message)
    }

    /// 用户主动取消：调用方不要把它当成下载失败。
    pub fn cancelled() -> Self {
        Self::new("CANCELLED", "任务已取消")
    }

    pub fn io(error: std::io::Error) -> Self {
        Self::new("IO", error.to_string())
    }

    pub fn db(error: rusqlite::Error) -> Self {
        Self::new("DB", error.to_string())
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

impl std::error::Error for AppError {}

impl From<std::io::Error> for AppError {
    fn from(error: std::io::Error) -> Self {
        Self::io(error)
    }
}

impl From<rusqlite::Error> for AppError {
    fn from(error: rusqlite::Error) -> Self {
        Self::db(error)
    }
}

impl From<zip::result::ZipError> for AppError {
    fn from(error: zip::result::ZipError) -> Self {
        Self::new("ZIP", error.to_string())
    }
}

pub type AppResult<T> = Result<T, AppError>;