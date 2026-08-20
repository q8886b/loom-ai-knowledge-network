"""Loom storage layer.

SQLite (WAL) + FTS5 + sqlite-vec.
Schema per 005 §2.8 and §5.1.

Tables:
  cards         - main card table (id, title, type, content, source, layer, origin, tags, use_count, search_count)
  links         - bidirectional graph edges (source_id, target_id)
  card_tags     - derived tag index maintained from cards.tags
  cards_fts     - FTS5 virtual table on title + content
  cards_vec     - sqlite-vec virtual table (embedding dim fixed per DB)
  loom_meta     - small key/value metadata such as embedding_dim
  task_trace    - task execution log
  reject_log    - write-draft rejection log (for density gate statistics)
  card_annotations - human reading notes per card (TUI/阅读批注；一卡一条)
  reading_progress - reading position per scope ('ns' or 'ns:book')

废弃表（init_db 检测到自动 drop）：
  l1_files      - 旧 L1 活跃度旁路表（已合并到 cards 统一活跃度）
  l4_proposals  - 与 staging JSON 重复（JSON 作 SSOT）
  l4_meta       - L4 maturity 单独表（maturity 从 content 第一段即时提取）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import sqlite_vec

from . import embed

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Loom home defaults to ~/.loom; override with LOOM_HOME.
PROJECT_ROOT = Path(os.environ.get("LOOM_HOME", Path.home() / ".loom"))
DB_PATH = PROJECT_ROOT / "data" / "brain.db"
CARDS_DIR = PROJECT_ROOT / "cards"
SOURCES_DIR = PROJECT_ROOT / "sources"
L4_INDEX_PATH = PROJECT_ROOT / "data" / "l4_index.md"
ORIENT_PATH = PROJECT_ROOT / "data" / "orient.md"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _private_file(path: Path) -> None:
    if path.exists():
        path.chmod(0o600)


def _secure_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        _private_file(Path(f"{db_path}{suffix}"))


def _write_private_text(path: Path, content: str) -> None:
    _private_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)

VALID_COGNITIVE_TYPES = {
    "概念", "结构", "机制", "案例", "判断", "反思", "模式", "主题",
}
VALID_CARD_TYPES = VALID_COGNITIVE_TYPES | {"source"}
VALID_TYPES = VALID_CARD_TYPES

# Card layer matrix (card layers only; task targets like L2_light live in plan.json)
# Rows: layer; Cols: type
# Note: L2 模式 标"少见"非"禁止"（004 §type×layer），允许在 DIGEST-Deep 时
# 材料本身触发明显模式涌现的情况下建立（005 §9.1 末尾）
LAYER_TYPE_MATRIX: dict[str, set[str]] = {
    "L1":       {"source"},
    "L2":       VALID_COGNITIVE_TYPES,
    "L3":       VALID_COGNITIVE_TYPES,
    "L4":       {"模式", "判断", "反思"},
}

VALID_LAYERS = set(LAYER_TYPE_MATRIX.keys())
TASK_TARGET_LAYERS = {"L1", "L2_light", "L2", "L3", "L4"}

EMBED_DIM = embed.embedding_dim()
MIN_CONTENT_LEN = 30

MATURITY_PATTERN = re.compile(r"^\[(探索期|熟练期)\]")

# Namespace patterns (005 §2.1)
# 领域（按 sources/XX-领域/ 目录约定）
DOMAINS = {"llm", "fin", "med", "law", "sw", "phil", "prod", "psy", "fit", "hist", "soc", "sci"}
_DOMAINS_RE = "|".join(sorted(DOMAINS))

# 卢曼 ID：数字段与小写字母段交替，可停在任一段。
# 例：3 / 3a / 3a1 / 3ab / 3ab12c
LUHMANN_ID = r"[0-9]+(?:[a-z]+[0-9]+)*(?:[a-z]+)?"
LUHMANN_ID_PATTERN = re.compile(rf"^{LUHMANN_ID}$")
# 书名 ID：英文关键词 / 中文拼音，可用单个连字符或下划线分段。
BOOK_ID = r"[a-z0-9]+(?:[-_][a-z0-9]+)*"
# L1 单元 ID：材料内部天然编号（章节号/段落号/"full"等），可分段。
L1_UNIT_ID = BOOK_ID

NS_PATTERN_L1 = re.compile(rf"^({_DOMAINS_RE}):({BOOK_ID}):src:({L1_UNIT_ID})$")
NS_PATTERN_L2 = re.compile(rf"^({_DOMAINS_RE}):({BOOK_ID}):({LUHMANN_ID})$")
NS_PATTERN_L3 = re.compile(rf"^({_DOMAINS_RE}):({LUHMANN_ID})$")
NS_PATTERN_L4 = re.compile(rf"^gen:({LUHMANN_ID})$")

CARD_ID_PATTERNS = {
    "L1": NS_PATTERN_L1,
    "L2": NS_PATTERN_L2,
    "L3": NS_PATTERN_L3,
    "L4": NS_PATTERN_L4,
}


def card_id_matches_layer(card_id: str, layer: str) -> bool:
    """Return whether the complete card ID matches the layer grammar."""
    pattern = CARD_ID_PATTERNS.get(layer)
    return pattern is not None and pattern.fullmatch(card_id) is not None


def _require_card_id_format(card_id: str, layer: str) -> None:
    """Defence-in-depth format gate for every store write path."""
    if not card_id_matches_layer(card_id, layer):
        raise ValueError(f"card_id '{card_id}' does not match layer={layer} format")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def connect(db_path: Path = DB_PATH):
    """Open a connection with sqlite-vec loaded, WAL mode, FK enforced.

    事务语义：with 块正常退出 → commit；异常退出 → 显式 rollback 再 raise。
    """
    _private_dir(PROJECT_ROOT)
    _private_dir(db_path.parent)
    conn = sqlite3.connect(str(db_path))
    _secure_db_files(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    # sqlite_vec may live in package; load by symbol
    try:
        sqlite_vec.load(conn)
    except Exception:
        # try loading from the package's compiled extension path
        import sqlite_vec as _sv
        conn.load_extension(os.path.dirname(_sv.__file__) or ".")
    conn.execute("PRAGMA journal_mode=WAL;")
    _secure_db_files(db_path)
    conn.execute("PRAGMA foreign_keys=ON;")
    # 并发写：冲突时排队等待而非立即报错（默认 busy_timeout=0 会直接抛 locked）
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        _secure_db_files(db_path)


# Schema 版本——每次 schema 变更 +1。
# init_db 启动时检查 PRAGMA user_version：匹配则跳过所有 CREATE/迁移；不匹配才跑。
# 这样首次跑会建 schema + 跑迁移；后续启动 <1ms 跳过。
# 将来 schema 变更：1) 改 schema 代码 2) bump SCHEMA_VERSION 3)（可选）加迁移函数。
SCHEMA_VERSION = 6


def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables / run migrations if schema version outdated. Idempotent.

    版本门用 SQLite 内置 PRAGMA user_version——单行整数，永久持久化在 db 文件里。
    """
    _private_dir(CARDS_DIR)
    _private_dir(SOURCES_DIR)
    with connect(db_path) as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return  # schema 已最新，跳过（启动开销 <1ms）

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                type         TEXT NOT NULL,
                content      TEXT NOT NULL,
                source       TEXT,
                layer        TEXT NOT NULL,
                origin       TEXT NOT NULL DEFAULT 'ai',
                tags         TEXT NOT NULL DEFAULT '[]',
                use_count    INTEGER NOT NULL DEFAULT 0,
                search_count INTEGER NOT NULL DEFAULT 0,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cards_layer ON cards(layer);
            CREATE INDEX IF NOT EXISTS idx_cards_type  ON cards(type);

            CREATE TABLE IF NOT EXISTS links (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_id);
            CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_id);

            CREATE TABLE IF NOT EXISTS card_tags (
                card_id TEXT NOT NULL,
                tag     TEXT NOT NULL,
                PRIMARY KEY (card_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_card_tags_tag ON card_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_card_tags_card ON card_tags(card_id);

            CREATE TABLE IF NOT EXISTS task_trace (
                task_id       TEXT PRIMARY KEY,
                plan_json     TEXT,
                started_at    REAL,
                ended_at      REAL,
                status        TEXT,    -- running/computed_passed/done/failed/timeout/salvaged
                drafts_count  INTEGER DEFAULT 0,
                committed_count INTEGER DEFAULT 0,
                retries       INTEGER DEFAULT 0,
                committed_ids TEXT,
                session_id    TEXT     -- 创建 task 的 agent runtime session（防并行 race / batch 信号丢失）
            );

            CREATE TABLE IF NOT EXISTS reject_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,
                task_id    TEXT,
                card_id    TEXT,
                check_id   TEXT NOT NULL,
                reason     TEXT NOT NULL,
                stage      TEXT NOT NULL   -- write_draft / stop_hook
            );

            CREATE TABLE IF NOT EXISTS loom_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # FTS5 — contentless-less external content via cards table
        # trigram tokenizer：对中文友好（按 3 字符滑窗），要求 SQLite 3.34+
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
                id UNINDEXED, title, content,
                content='cards', content_rowid='rowid',
                tokenize='trigram'
            );
            -- triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS cards_ai AFTER INSERT ON cards BEGIN
                INSERT INTO cards_fts(rowid, id, title, content)
                VALUES (new.rowid, new.id, new.title, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS cards_ad AFTER DELETE ON cards BEGIN
                INSERT INTO cards_fts(cards_fts, rowid, id, title, content)
                VALUES('delete', old.rowid, old.id, old.title, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS cards_au AFTER UPDATE ON cards BEGIN
                INSERT INTO cards_fts(cards_fts, rowid, id, title, content)
                VALUES('delete', old.rowid, old.id, old.title, old.content);
                INSERT INTO cards_fts(rowid, id, title, content)
                VALUES (new.rowid, new.id, new.title, new.content);
            END;
        """)
        _migrate_embedding_meta(conn)
        _ensure_cards_vec(conn)

        # 一次性迁移：删除 l1_files 旁路表，把活跃度合并到 cards 表
        # l1_files 是 008 §10 决议之前的历史包袱，005 §2.1 已明确 L1 走 cards 统一活跃度
        _migrate_drop_l1_files(conn)
        # 一次性迁移：删除 l4_proposals 表（与 staging JSON 重复，JSON 作 SSOT）
        _migrate_drop_table_if_exists(conn, "l4_proposals")
        # 一次性迁移：删除 l4_meta 表（maturity 从 content 第一段即时提取，pull 模式）
        _migrate_drop_table_if_exists(conn, "l4_meta")
        # v2: task_trace 加 session_id 列（防 hook 在 batch agent 模式下信号丢失）
        _migrate_add_column_if_missing(conn, "task_trace", "session_id", "TEXT")
        # v3: task_trace 记录本任务最终入库的 card ids，便于批量任务复盘和精准打捞
        _migrate_add_column_if_missing(conn, "task_trace", "committed_ids", "TEXT")
        # v4: card origin + human-maintained tags. card_tags is a derived index.
        _migrate_add_column_if_missing(conn, "cards", "origin", "TEXT NOT NULL DEFAULT 'ai'")
        _migrate_add_column_if_missing(conn, "cards", "tags", "TEXT NOT NULL DEFAULT '[]'")
        # v6: 人类阅读批注（TUI）：note 一卡一条；阅读进度按 scope 记位置
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS card_annotations (
                card_id    TEXT PRIMARY KEY,
                note       TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reading_progress (
                scope        TEXT PRIMARY KEY,
                last_card_id TEXT NOT NULL DEFAULT '',
                read_ids     TEXT NOT NULL DEFAULT '[]',
                updated_at   REAL NOT NULL
            );
        """)
        _migrate_embedding_meta(conn)
        _ensure_cards_vec(conn)
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_cards_origin ON cards(origin);
            CREATE TABLE IF NOT EXISTS card_tags (
                card_id TEXT NOT NULL,
                tag     TEXT NOT NULL,
                PRIMARY KEY (card_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_card_tags_tag ON card_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_card_tags_card ON card_tags(card_id);
        """)
        rebuild_tag_index(conn)

        # 跑完所有 CREATE + 迁移，标记版本——下次启动直接跳过
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table', 'virtual table')",
        (name,),
    ).fetchone()
    return row is not None


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM loom_meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO loom_meta(key, value) VALUES (?, ?)",
        (key, value),
    )


def _migrate_embedding_meta(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loom_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    if _get_meta(conn, "embedding_dim") is not None:
        return
    # Pre-v5 Loom only had one vector shape: Zhipu embedding-3, 2048 dim.
    # If cards_vec already exists, preserve that dimension until an explicit
    # rebuild. Fresh DBs use the configured provider dimension.
    dim = 2048 if _table_exists(conn, "cards_vec") else EMBED_DIM
    _set_meta(conn, "embedding_dim", str(dim))


def _db_embedding_dim(conn: sqlite3.Connection) -> int:
    _migrate_embedding_meta(conn)
    value = _get_meta(conn, "embedding_dim")
    return int(value) if value else EMBED_DIM


def get_embedding_dim() -> int:
    with connect() as conn:
        return _db_embedding_dim(conn)


def _create_cards_vec(conn: sqlite3.Connection, dim: int) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS cards_vec USING vec0("
        f"card_id TEXT PRIMARY KEY, embedding float[{dim}])"
    )


def _ensure_cards_vec(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "cards_vec"):
        _create_cards_vec(conn, _db_embedding_dim(conn))


def _ensure_embedding_dim(conn: sqlite3.Connection, dim: int) -> None:
    expected = _db_embedding_dim(conn)
    if dim != expected:
        raise ValueError(
            f"embedding dimension mismatch for this DB: got {dim}, expected {expected}. "
            "Use the same embedding model or run `loom-admin rebuild-embeddings` after "
            "changing LOOM_EMBED_PROVIDER / LOOM_EMBED_MODEL / LOOM_EMBED_DIM."
        )


def reset_vector_index(dim: int) -> None:
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS cards_vec")
        _set_meta(conn, "embedding_dim", str(dim))
        _create_cards_vec(conn, dim)


def list_cards_for_embedding() -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, content FROM cards ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_embeddings_batch(card_ids: list[str], embeddings: list[list[float]]) -> None:
    if len(card_ids) != len(embeddings):
        raise ValueError("card_ids and embeddings length mismatch")
    with connect() as conn:
        for cid, emb in zip(card_ids, embeddings):
            _upsert_embedding(conn, cid, emb)


def _migrate_drop_l1_files(conn: sqlite3.Connection) -> None:
    """若检测到旧 l1_files 表存在，把 use_count/search_count 取 max 合并到
    对应 L1 source card，然后 drop 表。idempotent——表不存在则 no-op。
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='l1_files'"
    )
    if not cur.fetchone():
        return
    rows = conn.execute(
        "SELECT path, use_count, search_count FROM l1_files"
    ).fetchall()
    for r in rows:
        # 取 max 防覆盖：cards.use_count 可能已经 ≥ l1_files.use_count（旧 bump 双写）
        conn.execute(
            """UPDATE cards
               SET use_count = MAX(use_count, ?),
                   search_count = MAX(search_count, ?),
                   updated_at = ?
               WHERE layer='L1' AND type='source' AND source=?""",
            (r["use_count"], r["search_count"], now(), r["path"]),
        )
    conn.execute("DROP TABLE l1_files")


def _migrate_drop_table_if_exists(conn: sqlite3.Connection, table_name: str) -> None:
    """通用一次性 drop——用于清理已废弃的旧表。idempotent。"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    if cur.fetchone():
        conn.execute(f"DROP TABLE {table_name}")


def _migrate_add_column_if_missing(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str
) -> None:
    """idempotent 加列：若列已存在则 no-op。"""
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    cols = {row[1] for row in cur.fetchall()}
    if column_name not in cols:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        )


# ---------------------------------------------------------------------------
# Origin / tags
# ---------------------------------------------------------------------------

VALID_ORIGINS = {"ai", "human"}


def normalize_origin(origin: str | None) -> str:
    value = (origin or "ai").strip().lower()
    if value not in VALID_ORIGINS:
        raise ValueError(f"origin must be one of {sorted(VALID_ORIGINS)}, got {origin!r}")
    return value


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
    """Normalize human-maintained tag values while preserving display text."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = str(raw).strip()
        if not tag:
            raise ValueError("tag cannot be empty")
        if "," in tag:
            raise ValueError(f"tag cannot contain comma: {tag!r}")
        if tag not in seen:
            out.append(tag)
            seen.add(tag)
    return out


def tags_to_json(tags: Iterable[str] | None) -> str:
    return json.dumps(normalize_tags(tags), ensure_ascii=False)


def parse_tags_json(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"tags must be a JSON array: {e}") from e
    if not isinstance(parsed, list):
        raise ValueError("tags must be a JSON array")
    if not all(isinstance(item, str) for item in parsed):
        raise ValueError("tags must be a JSON array of strings")
    return normalize_tags(parsed)


def _sync_card_tag_index(conn: sqlite3.Connection, card_id: str, tags: Iterable[str]) -> None:
    conn.execute("DELETE FROM card_tags WHERE card_id=?", (card_id,))
    for tag in normalize_tags(tags):
        conn.execute(
            "INSERT OR IGNORE INTO card_tags(card_id, tag) VALUES (?, ?)",
            (card_id, tag),
        )


def rebuild_tag_index(conn: sqlite3.Connection | None = None) -> int:
    """Rebuild derived card_tags from cards.tags. Returns indexed edge count."""
    def _run(c: sqlite3.Connection) -> int:
        c.execute("DELETE FROM card_tags")
        count = 0
        rows = c.execute("SELECT id, tags FROM cards").fetchall()
        for row in rows:
            tags = parse_tags_json(row["tags"] if row["tags"] else "[]")
            _sync_card_tag_index(c, row["id"], tags)
            count += len(tags)
        return count

    if conn is not None:
        return _run(conn)
    with connect() as c:
        return _run(c)


def list_tags() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) AS count FROM card_tags GROUP BY tag ORDER BY count DESC, tag"
        ).fetchall()
    return [dict(r) for r in rows]


def update_card_tags(card_id: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> dict[str, Any]:
    add_tags = normalize_tags(add)
    remove_tags = normalize_tags(remove)
    overlap = sorted(set(add_tags) & set(remove_tags))
    if overlap:
        raise ValueError(f"same tag cannot be both added and removed: {overlap}")

    ts = now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if row is None:
            raise ValueError(f"card not found: {card_id}")
        tags = parse_tags_json(row["tags"] if row["tags"] else "[]")
        current = [t for t in tags if t not in set(remove_tags)]
        for tag in add_tags:
            if tag not in current:
                current.append(tag)
        tags_json = tags_to_json(current)
        conn.execute(
            "UPDATE cards SET tags=?, updated_at=? WHERE id=?",
            (tags_json, ts, card_id),
        )
        _sync_card_tag_index(conn, card_id, current)
        updated = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    card = dict(updated)
    write_card_file(card)
    return card


def _tag_filter_sql(tags: list[str] | None, alias: str = "c") -> tuple[str, list[Any]]:
    normalized = normalize_tags(tags)
    clauses = []
    params: list[Any] = []
    for idx, tag in enumerate(normalized):
        ct = f"ct_filter_{idx}"
        clauses.append(
            f"EXISTS (SELECT 1 FROM card_tags {ct} WHERE {ct}.card_id = {alias}.id AND {ct}.tag = ?)"
        )
        params.append(tag)
    return (" AND " + " AND ".join(clauses), params) if clauses else ("", [])


# ---------------------------------------------------------------------------
# Reading annotations / progress (human notes; written by TUI, read-only for agents)
# ---------------------------------------------------------------------------

READ_IDS_CAP = 20000


def set_annotation(card_id: str, note: str) -> dict[str, Any]:
    """Upsert the human note on a card. Empty note deletes the annotation row
    (标签不打这里——标签走 cards.tags / update_card_tags)。"""
    note = (note or "").strip()
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone()
        if row is None:
            raise ValueError(f"card not found: {card_id}")
        ts = now()
        if not note:
            conn.execute("DELETE FROM card_annotations WHERE card_id=?", (card_id,))
            return {"card_id": card_id, "note": "", "updated_at": None}
        conn.execute(
            """INSERT INTO card_annotations(card_id, note, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(card_id) DO UPDATE SET
                 note=excluded.note, updated_at=excluded.updated_at""",
            (card_id, note, ts),
        )
        row = conn.execute(
            "SELECT * FROM card_annotations WHERE card_id=?", (card_id,)
        ).fetchone()
    return dict(row)


def get_annotation(card_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM card_annotations WHERE card_id=?", (card_id,)
        ).fetchone()
        return dict(row) if row else None


def list_annotations() -> list[dict[str, Any]]:
    """标注卡聚合列表：有标签（cards.tags 非空）或有笔记（card_annotations）的卡。

    排序按最近变化（标签 updated_at 与笔记 updated_at 取大）倒序，事后集中处理
    时"最近标注"在最上面。
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.type, c.layer, c.tags,
                      a.note AS note, a.updated_at AS note_ts,
                      MAX(c.updated_at, COALESCE(a.updated_at, 0)) AS ts
               FROM cards c
               LEFT JOIN card_annotations a ON a.card_id = c.id
               WHERE c.tags != '[]' OR a.card_id IS NOT NULL
               ORDER BY ts DESC"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = parse_tags_json(d["tags"])
            out.append(d)
        return out


def set_reading_progress(
    scope: str, last_card_id: str = "", read_ids: list[str] | None = None
) -> dict[str, Any]:
    """Upsert reading progress for a scope ('ns' or 'ns:book').

    read_ids=None 表示保持已有列表不变，只更新 last_card_id。
    """
    with connect() as conn:
        existing = conn.execute(
            "SELECT read_ids FROM reading_progress WHERE scope=?", (scope,)
        ).fetchone()
        if read_ids is None:
            current = (
                json.loads(existing["read_ids"]) if existing and existing["read_ids"] else []
            )
        else:
            current = list(read_ids)
        current = current[-READ_IDS_CAP:]
        conn.execute(
            """INSERT INTO reading_progress(scope, last_card_id, read_ids, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(scope) DO UPDATE SET
                 last_card_id=excluded.last_card_id,
                 read_ids=excluded.read_ids,
                 updated_at=excluded.updated_at""",
            (scope, last_card_id, json.dumps(current, ensure_ascii=False), now()),
        )
        row = conn.execute(
            "SELECT * FROM reading_progress WHERE scope=?", (scope,)
        ).fetchone()
    return dict(row)


def mark_card_read(scope: str, card_id: str) -> dict[str, Any]:
    """把卡加入 scope 的已读列表（去重、追加到尾部）并更新 last_card_id。"""
    with connect() as conn:
        existing = conn.execute(
            "SELECT read_ids FROM reading_progress WHERE scope=?", (scope,)
        ).fetchone()
        ids = (
            json.loads(existing["read_ids"]) if existing and existing["read_ids"] else []
        )
        ids = [i for i in ids if i != card_id]
        ids.append(card_id)
        ids = ids[-READ_IDS_CAP:]
        conn.execute(
            """INSERT INTO reading_progress(scope, last_card_id, read_ids, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(scope) DO UPDATE SET
                 last_card_id=excluded.last_card_id,
                 read_ids=excluded.read_ids,
                 updated_at=excluded.updated_at""",
            (scope, card_id, json.dumps(ids, ensure_ascii=False), now()),
        )
        row = conn.execute(
            "SELECT * FROM reading_progress WHERE scope=?", (scope,)
        ).fetchone()
    return dict(row)


def get_reading_progress(scope: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reading_progress WHERE scope=?", (scope,)
        ).fetchone()
        return dict(row) if row else None


def list_reading_progress(ns: str | None = None) -> list[dict[str, Any]]:
    """全部进度；ns 给定时只取该域（scope LIKE 'ns:%'）。"""
    with connect() as conn:
        if ns:
            lower, upper = f"{ns}:", f"{ns};"
            rows = conn.execute(
                "SELECT * FROM reading_progress WHERE scope >= ? AND scope < ?",
                (lower, upper),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reading_progress").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Card CRUD
# ---------------------------------------------------------------------------

def now() -> float:
    return time.time()


def card_exists(conn: sqlite3.Connection, card_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone()
    return row is not None


def _l1_source_card_id_from_path(path: str) -> str | None:
    """根据 markdown 路径推断 L1 source card id 的"建议值"，仅用于诊断/提示。
    真实 id 由 `import-source` 显式传入，存为 card.source 的反查走 get_source_card_by_path。
    """
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        rel = p.relative_to(PROJECT_ROOT)
    except Exception:
        return None
    parts = rel.parts
    if len(parts) < 3 or parts[0] != "sources":
        return None
    domain_dir = parts[1]
    book_dir = parts[2]
    unit_name = p.stem
    # 领域映射：sources/XX-金融 → fin；其他领域按目录前缀拼音/英文（见 CLAUDE.md）
    domain_map = {
        "金融": "fin", "医学": "med", "法律": "law",
        "软件": "sw", "哲学": "phil", "产品": "prod", "LLM": "llm",
    }
    domain_key = domain_dir.split("-", 1)[-1]
    domain = domain_map.get(domain_key, domain_key.lower())
    book = book_dir.split("-", 1)[-1].lower().replace(" ", "")
    # 单元号：去掉非数字前缀（ch01 → 01），纯数字/字母保留
    import re as _re
    m = _re.search(r"(\d+)", unit_name)
    unit = m.group(1) if m else unit_name
    return f"{domain}:{book}:src:{unit}"


def get_card(card_id: str, increment_use: bool = False) -> dict[str, Any] | None:
    """Read a card. If increment_use, bump use_count (for read-card calls)."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if row is None:
            return None
        if increment_use:
            conn.execute(
                "UPDATE cards SET use_count=use_count+1, updated_at=? WHERE id=?",
                (now(), card_id),
            )
        return dict(row)


def list_cards_by_layer(layer: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cards WHERE layer=? ORDER BY id", (layer,)
        ).fetchall()
        return [dict(r) for r in rows]


def _id_namespace(card_id: str) -> str:
    """Extract namespace prefix from 'ns:rest' style id."""
    if ":" in card_id:
        return card_id.split(":", 1)[0]
    return ""


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def add_link(conn: sqlite3.Connection, src: str, tgt: str) -> None:
    """Add bidirectional edge. Skip self-loops and duplicates.

    新增 link 时 bump 被引用方（tgt）的 use_count——被 link 等于被引用（005 §八）。
    重复 link 不重复 bump（INSERT OR IGNORE 是 no-op 时跳过）。
    """
    if src == tgt:
        return
    cur = conn.execute(
        "INSERT OR IGNORE INTO links(source_id, target_id) VALUES (?, ?)",
        (src, tgt),
    )
    if cur.rowcount > 0:
        # 新增 link（非重复）→ bump 被引用方 use_count
        conn.execute(
            "UPDATE cards SET use_count=use_count+1, updated_at=? WHERE id=?",
            (now(), tgt),
        )
    conn.execute(
        "INSERT OR IGNORE INTO links(source_id, target_id) VALUES (?, ?)",
        (tgt, src),
    )


def remove_link(conn: sqlite3.Connection, src: str, tgt: str) -> None:
    conn.execute("DELETE FROM links WHERE source_id=? AND target_id=?", (src, tgt))
    conn.execute("DELETE FROM links WHERE source_id=? AND target_id=?", (tgt, src))


def get_links(card_id: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT target_id FROM links WHERE source_id=?", (card_id,)
        ).fetchall()
        return [r["target_id"] for r in rows]


def get_neighbors(card_id: str, depth: int = 1) -> list[str]:
    """BFS up to depth."""
    seen: set[str] = {card_id}
    frontier = {card_id}
    result: list[str] = []
    with connect() as conn:
        for _ in range(depth):
            next_frontier: set[str] = set()
            for node in frontier:
                rows = conn.execute(
                    "SELECT target_id FROM links WHERE source_id=?", (node,)
                ).fetchall()
                for r in rows:
                    t = r["target_id"]
                    if t not in seen:
                        seen.add(t)
                        result.append(t)
                        next_frontier.add(t)
            frontier = next_frontier
            if not frontier:
                break
    return result


def get_children(card_id: str) -> list[str]:
    """Return direct Luhmann children of `card_id`.

    A direct child is `card_id` plus exactly one new segment:
    - if the parent ends with digits, the child appends one letter (e.g. `fin:3` -> `fin:3a`);
    - if the parent ends with a letter, the child appends one digit run (e.g. `fin:3a` -> `fin:3a1`).

    This avoids misclassifying `fin:30a` as a child of `fin:3`.
    """
    split = _luhmann_ns_and_id(card_id)
    if split is None:
        return []
    segments = _luhmann_segments(split[1])
    if not segments:
        return []
    last = segments[-1]
    if last[0].isdigit():
        tail_re = re.compile(r"^[a-z]+$")
    else:
        tail_re = re.compile(r"^\d+$")

    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM cards WHERE id LIKE ? AND id != ?",
            (card_id + "%", card_id),
        ).fetchall()
        children = [
            r["id"] for r in rows
            if tail_re.match(r["id"][len(card_id):])
        ]
    return sorted(children)


def get_siblings(card_id: str) -> list[str]:
    """Return Luhmann siblings of `card_id` (cards sharing the same parent)."""
    parent = _parent_id(card_id)
    if parent is None:
        # Top-level root cards share the namespace.
        split = _luhmann_ns_and_id(card_id)
        if split is None:
            return []
        ns, luhmann = split
        if not re.fullmatch(r"\d+", luhmann):
            return []
        with connect() as conn:
            rows = conn.execute(
                "SELECT id FROM cards WHERE id LIKE ?",
                (ns + ":%",),
            ).fetchall()
            sibs: list[str] = []
            for r in rows:
                cid = r["id"]
                c_split = _luhmann_ns_and_id(cid)
                if (
                    c_split is not None
                    and c_split[0] == ns
                    and c_split[1] != luhmann
                    and re.fullmatch(r"\d+", c_split[1])
                ):
                    sibs.append(cid)
        return sorted(sibs)
    return [c for c in get_children(parent) if c != card_id]


def _luhmann_segments(luhmann: str) -> list[str]:
    """Split a Luhmann ID into alternating digit/letter segments.

    Examples: ``"12a1" -> ["12", "a", "1"]``; ``"3" -> ["3"]``.
    Invalid or partially matching input returns an empty list.
    """
    if LUHMANN_ID_PATTERN.fullmatch(luhmann) is None:
        return []
    return re.findall(r"\d+|[a-z]+", luhmann)


def _luhmann_parent(luhmann: str) -> str | None:
    """Return the Luhmann ID with the last segment stripped.

    Examples: ``"12a1" -> "12a"``; ``"3a" -> "3"``; ``"3" -> None``.
    """
    segments = _luhmann_segments(luhmann)
    if len(segments) <= 1:
        return None
    return "".join(segments[:-1])


def _luhmann_ns_and_id(card_id: str) -> tuple[str, str] | None:
    """Return (namespace prefix, luhmann segment) for L2/L3/L4 ids.

    - L2: ``llm:harness:03a`` -> ("llm:harness", "03a")
    - L3: ``fin:3a`` -> ("fin", "3a")
    - L4: ``gen:1a`` -> ("gen", "1a")
    - L1 / malformed: None
    """
    parts = card_id.split(":")
    if (
        len(parts) == 2
        and (parts[0] in DOMAINS or parts[0] == "gen")
        and LUHMANN_ID_PATTERN.fullmatch(parts[1]) is not None
    ):
        return parts[0], parts[1]
    if (
        len(parts) == 3
        and parts[0] in DOMAINS
        and re.fullmatch(BOOK_ID, parts[1]) is not None
        and LUHMANN_ID_PATTERN.fullmatch(parts[2]) is not None
    ):
        return f"{parts[0]}:{parts[1]}", parts[2]
    return None


def _parent_id(card_id: str) -> str | None:
    """Strip one trailing Luhmann segment.

    Examples:
      - ``llm:harness:01a`` -> ``llm:harness:01``
      - ``fin:3a`` -> ``fin:3``
      - ``gen:1a1`` -> ``gen:1a``
      - ``fin:3`` / ``llm:harness:01`` -> None
    """
    split = _luhmann_ns_and_id(card_id)
    if split is None:
        return None
    ns, luhmann = split
    parent_luhmann = _luhmann_parent(luhmann)
    if parent_luhmann is None:
        return None
    return f"{ns}:{parent_luhmann}"


def audit_luhmann_tree(db_path: Path = DB_PATH) -> dict[str, Any]:
    """Audit all card ID formats and L2/L3/L4 parent completeness."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, layer FROM cards ORDER BY id"
        ).fetchall()

    all_ids = {row["id"] for row in rows}
    invalid_ids: list[dict[str, str]] = []
    missing_parents: list[dict[str, str]] = []
    for row in rows:
        card_id = row["id"]
        layer = row["layer"]
        if not card_id_matches_layer(card_id, layer):
            invalid_ids.append({"id": card_id, "layer": layer})
            continue
        if layer == "L1":
            continue
        parent_id = _parent_id(card_id)
        if parent_id is not None and parent_id not in all_ids:
            missing_parents.append(
                {"id": card_id, "layer": layer, "parent_id": parent_id}
            )

    missing_parent_ids = sorted({item["parent_id"] for item in missing_parents})
    invalid_by_layer = {
        layer: sum(item["layer"] == layer for item in invalid_ids)
        for layer in ("L1", "L2", "L3", "L4")
    }
    missing_cards_by_layer = {
        layer: sum(item["layer"] == layer for item in missing_parents)
        for layer in ("L1", "L2", "L3", "L4")
    }
    missing_parent_ids_by_layer = {
        layer: len(
            {item["parent_id"] for item in missing_parents if item["layer"] == layer}
        )
        for layer in ("L1", "L2", "L3", "L4")
    }
    return {
        "ok": not invalid_ids and not missing_parents,
        "summary": {
            "cards_checked": len(rows),
            "invalid_ids": len(invalid_ids),
            "invalid_ids_by_layer": invalid_by_layer,
            "cards_with_missing_parent": len(missing_parents),
            "cards_with_missing_parent_by_layer": missing_cards_by_layer,
            "missing_parent_ids": len(missing_parent_ids),
            "missing_parent_ids_by_layer": missing_parent_ids_by_layer,
        },
        "invalid_ids": invalid_ids,
        "missing_parents": missing_parents,
        "missing_parent_ids": missing_parent_ids,
    }


def get_card_links_types(conn: sqlite3.Connection, card_id: str) -> list[dict[str, Any]]:
    """Return list of {id, type, layer} for cards this one links to."""
    rows = conn.execute(
        """SELECT c.id, c.type, c.layer FROM cards c
           JOIN links l ON c.id = l.target_id
           WHERE l.source_id=?""",
        (card_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Card file mirror (cards/<ns>/<id>.md)
# ---------------------------------------------------------------------------

def _card_file_path(card_id: str) -> Path:
    safe = card_id.replace(":", "/")
    return CARDS_DIR / f"{safe}.md"


def write_card_file(card: dict[str, Any]) -> None:
    """Mirror card content to cards/<ns>/<id>.md for git versioning."""
    p = _card_file_path(card["id"])
    origin = normalize_origin(card.get("origin"))
    tags = parse_tags_json(card.get("tags") or "[]")
    optional_meta: list[str] = []
    if origin == "human":
        optional_meta.append("origin: human")
    if tags:
        optional_meta.append(f"tags: {tags_to_json(tags)}")
    optional_block = ("\n".join(optional_meta) + "\n") if optional_meta else ""
    body = f"""---
id: {card['id']}
title: {card['title']}
type: {card['type']}
layer: {card['layer']}
source: {card.get('source') or ''}
{optional_block}\
use_count: {card.get('use_count', 0)}
search_count: {card.get('search_count', 0)}
---

# {card['title']}

{card['content']}
"""
    _write_private_text(p, body)


def remove_card_file(card_id: str) -> None:
    p = _card_file_path(card_id)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Insert / update (called only by privileged commit path)
# ---------------------------------------------------------------------------

def _require_luhmann_parent(
    conn: sqlite3.Connection,
    card_id: str,
    layer: str,
    batch_ids: set[str] | None = None,
) -> None:
    """Require a non-root card's parent in the DB or current atomic batch."""
    if layer not in {"L2", "L3", "L4"}:
        return
    parent_id = _parent_id(card_id)
    if parent_id is None or parent_id in (batch_ids or set()):
        return
    row = conn.execute("SELECT 1 FROM cards WHERE id=?", (parent_id,)).fetchone()
    if row is None:
        raise ValueError(f"card_id '{card_id}' requires missing parent '{parent_id}'")


def insert_card(
    card_id: str,
    title: str,
    type_: str,
    content: str,
    layer: str,
    source: str | None = None,
    origin: str | None = None,
    tags: Iterable[str] = (),
    links: Iterable[str] = (),
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    """Insert a card after enforcing ID format and parent completeness."""
    _require_card_id_format(card_id, layer)
    ts = now()
    card_origin = normalize_origin(origin)
    card_tags = normalize_tags(tags)
    tags_json = tags_to_json(card_tags)
    card = {
        "id": card_id,
        "title": title,
        "type": type_,
        "content": content,
        "source": source,
        "layer": layer,
        "origin": card_origin,
        "tags": tags_json,
        "use_count": 0,
        "search_count": 0,
    }
    with connect() as conn:
        _require_luhmann_parent(conn, card_id, layer)
        conn.execute(
            """INSERT INTO cards
               (id, title, type, content, source, layer, origin, tags, use_count, search_count,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
            (card_id, title, type_, content, source, layer, card_origin, tags_json, ts, ts),
        )
        _sync_card_tag_index(conn, card_id, card_tags)
        for tgt in links:
            add_link(conn, card_id, tgt)
        if embedding is not None:
            _upsert_embedding(conn, card_id, embedding)
    write_card_file(card)
    return card


class _BatchConflict(Exception):
    """内部信号：批量插入检测到 UNIQUE 冲突，触发事务 rollback。"""
    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts


def insert_cards_batch(
    cards: list[dict[str, Any]],
    embeddings: list[list[float] | None],
) -> dict[str, Any]:
    """单事务批量插卡。

    Args:
        cards: 每个 dict 需含 id/title/type/content/layer，可选 source/origin/tags/links
        embeddings: 与 cards 等长的向量列表，None 表示不嵌入

    Returns:
        {"status": "committed", "committed": [id...]}  全部成功
        {"status": "rejected", "conflicts": [id...]}   有冲突，整批 rollback

    冲突语义：任一卡 INSERT 撞 UNIQUE → 整批 rollback（一张都不入库），
    让子 agent 看到"全部被拒"，回去改完再来。文件镜像在 commit 成功后
    才写，避免事务 rollback 后留下孤儿镜像。
    """
    for card in cards:
        _require_card_id_format(card["id"], card["layer"])
    batch_ids = {card["id"] for card in cards}
    ts = now()
    conflicts: list[str] = []
    try:
        with connect() as conn:
            for card in cards:
                _require_luhmann_parent(
                    conn, card["id"], card["layer"], batch_ids=batch_ids
                )
            for card, emb in zip(cards, embeddings):
                cid = card["id"]
                card_origin = normalize_origin(card.get("origin"))
                card_tags = normalize_tags(card.get("tags", []))
                tags_json = tags_to_json(card_tags)
                try:
                    conn.execute(
                        """INSERT INTO cards
                           (id, title, type, content, source, layer, origin, tags,
                            use_count, search_count, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
                        (cid, card["title"], card["type"], card["content"],
                         card.get("source"), card["layer"], card_origin, tags_json, ts, ts),
                    )
                except sqlite3.IntegrityError:
                    conflicts.append(cid)
                    continue
                card["origin"] = card_origin
                card["tags"] = tags_json
                _sync_card_tag_index(conn, cid, card_tags)
                for tgt in card.get("links", []):
                    add_link(conn, cid, tgt)
                if emb is not None:
                    _upsert_embedding(conn, cid, emb)
            if conflicts:
                raise _BatchConflict(conflicts)
    except _BatchConflict:
        return {"status": "rejected", "conflicts": conflicts}

    for card in cards:
        write_card_file(card)
    return {"status": "committed", "committed": [c["id"] for c in cards]}


def update_card_content(
    card_id: str,
    content: str | None = None,
    title: str | None = None,
    embedding: list[float] | None = None,
) -> None:
    """Update an existing card. FTS sync via triggers."""
    ts = now()
    with connect() as conn:
        if content is not None:
            conn.execute(
                "UPDATE cards SET content=?, updated_at=? WHERE id=?",
                (content, ts, card_id),
            )
        if title is not None:
            conn.execute(
                "UPDATE cards SET title=?, updated_at=? WHERE id=?",
                (title, ts, card_id),
            )
        if embedding is not None:
            _upsert_embedding(conn, card_id, embedding)
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if row:
        write_card_file(dict(row))


def delete_card(card_id: str) -> None:
    with connect() as conn:
        descendants = conn.execute(
            "SELECT id FROM cards WHERE id LIKE ? AND id != ? ORDER BY id",
            (card_id + "%", card_id),
        ).fetchall()
        children = [row["id"] for row in descendants if _parent_id(row["id"]) == card_id]
        if children:
            raise ValueError(
                f"card '{card_id}' has children and cannot be deleted: {children}"
            )
        conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
        conn.execute("DELETE FROM links WHERE source_id=? OR target_id=?", (card_id, card_id))
        conn.execute("DELETE FROM cards_vec WHERE card_id=?", (card_id,))
        conn.execute("DELETE FROM card_tags WHERE card_id=?", (card_id,))
    remove_card_file(card_id)


def update_card(
    card_id: str,
    title: str | None = None,
    type_: str | None = None,
    content: str | None = None,
    source: str | None = None,
    origin: str | None = None,
    links: list[str] | None = None,
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    """Update an existing card. Caller responsible for validation.

    For links: None = unchanged, [] = clear all, [...] = replace all.
    Embedding: if None and content changed, caller should compute new embedding.

    layer 不可改——卡一旦入库，layer 是其认知身份（005 §4.3）。
    真要换 layer 应走 delete + 新建，让校验链路完整重跑。
    """
    existing = get_card(card_id)
    if not existing:
        raise ValueError(f"card not found: {card_id}")

    new_title = title if title is not None else existing["title"]
    new_type = type_ if type_ is not None else existing["type"]
    new_content = content if content is not None else existing["content"]
    new_source = source if source is not None else existing["source"]
    new_origin = normalize_origin(origin if origin is not None else existing.get("origin"))

    ts = now()
    with connect() as conn:
        conn.execute(
            """UPDATE cards SET title=?, type=?, content=?, source=?, origin=?, updated_at=?
               WHERE id=?""",
            (new_title, new_type, new_content, new_source, new_origin, ts, card_id),
        )

        if links is not None:
            old_links = [r["target_id"] for r in conn.execute(
                "SELECT target_id FROM links WHERE source_id=?", (card_id,)).fetchall()]
            for old in old_links:
                remove_link(conn, card_id, old)
            for new in links:
                add_link(conn, card_id, new)

        if embedding is not None:
            _upsert_embedding(conn, card_id, embedding)

        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if row:
        write_card_file(dict(row))
    return dict(row)


# ---------------------------------------------------------------------------
# L1 source cards (008 §10/§16/§22/§24)
# ---------------------------------------------------------------------------

def insert_source_card(
    source_id: str,
    title: str,
    path: str,
    content: str | None = None,
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    """注册 L1 source card。

    - content 默认从 path 读取全文（方案 C：全文入 cards.content）
    - source 字段保留原始 markdown 路径
    - FTS / embedding / 卡片镜像走正常 insert_card 路径
    """
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / path
    if content is None:
        content = p.read_text(encoding="utf-8")
    rel = str(p.relative_to(PROJECT_ROOT)) if str(p).startswith(str(PROJECT_ROOT)) else str(p)

    existing = get_card(source_id)
    if existing:
        return existing

    card = insert_card(
        card_id=source_id,
        title=title,
        type_="source",
        content=content,
        layer="L1",
        source=rel,
        links=[],
        embedding=embedding,
    )
    return card


def get_source_card_by_path(path: str) -> dict[str, Any] | None:
    """通过原始 markdown 路径反查 L1 source card。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM cards WHERE layer='L1' AND type='source' AND source=?",
            (path,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# L1 file activity (004 §卡片活跃度自动维护；走 cards 表统一活跃度)
# ---------------------------------------------------------------------------

def bump_l1_card_use_by_path(path: str) -> None:
    """Increment use_count for the L1 source card whose source=path.

    若 path 没对应到任何已入库的 L1 source card（如 read-source 兜底读未导入文件），
    则静默 no-op——这是合法场景（read-source 不负责创建卡）。
    """
    with connect() as conn:
        conn.execute(
            """UPDATE cards SET use_count=use_count+1, updated_at=?
               WHERE layer='L1' AND type='source' AND source=?""",
            (now(), path),
        )

def _upsert_embedding(conn: sqlite3.Connection, card_id: str, embedding: list[float]) -> None:
    import struct
    _ensure_cards_vec(conn)
    _ensure_embedding_dim(conn, len(embedding))
    blob = struct.pack(f"{len(embedding)}f", *embedding)
    conn.execute("DELETE FROM cards_vec WHERE card_id=?", (card_id,))
    conn.execute(
        "INSERT INTO cards_vec(card_id, embedding) VALUES (?, ?)",
        (card_id, blob),
    )


def _extract_maturity(content: str) -> str | None:
    first_line = content.lstrip().split("\n", 1)[0]
    m = MATURITY_PATTERN.match(first_line)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_fts(query: str, top: int = 10, ns: str | None = None,
               type_: str | None = None,
               tags: list[str] | None = None,
               bump_search: bool = True) -> list[dict[str, Any]]:
    with connect() as conn:
        sql = """SELECT c.id, c.title, c.type, c.layer, c.source, c.origin, c.tags,
                        CASE WHEN c.layer='L1' THEN substr(c.content, 1, 200) ELSE NULL END AS snippet,
                        CASE WHEN c.layer='L1' THEN length(c.content) ELSE NULL END AS content_size,
                        bm25(cards_fts) AS score
                 FROM cards_fts
                 JOIN cards c ON c.id = cards_fts.id
                 WHERE cards_fts MATCH ?"""
        params: list[Any] = [query]
        if ns:
            sql += " AND c.id LIKE ?"
            params.append(ns + ":%")
        if type_:
            sql += " AND c.type = ?"
            params.append(type_)
        tag_sql, tag_params = _tag_filter_sql(tags, alias="c")
        sql += tag_sql
        params.extend(tag_params)
        sql += " ORDER BY score LIMIT ?"
        params.append(top)
        rows = conn.execute(sql, params).fetchall()
        if not rows and len(query.strip()) < 3:
            like = f"%{query.strip()}%"
            like_sql = """SELECT id, title, type, layer, source, origin, tags,
                                 CASE WHEN layer='L1' THEN substr(content, 1, 200) ELSE NULL END AS snippet,
                                 CASE WHEN layer='L1' THEN length(content) ELSE NULL END AS content_size,
                                 0.0 AS score
                          FROM cards c
                          WHERE (title LIKE ? OR content LIKE ?)"""
            like_params: list[Any] = [like, like]
            if ns:
                like_sql += " AND c.id LIKE ?"
                like_params.append(ns + ":%")
            if type_:
                like_sql += " AND c.type = ?"
                like_params.append(type_)
            tag_sql, tag_params = _tag_filter_sql(tags, alias="c")
            like_sql += tag_sql
            like_params.extend(tag_params)
            like_sql += " ORDER BY updated_at DESC LIMIT ?"
            like_params.append(top)
            rows = conn.execute(like_sql, like_params).fetchall()
        if bump_search:
            _bump_search_counts(conn, [r["id"] for r in rows])
        return [dict(r) for r in rows]


def search_vector(query_vec: list[float], top: int = 10, ns: str | None = None,
                  type_: str | None = None,
                  tags: list[str] | None = None,
                  bump_search: bool = True) -> list[dict[str, Any]]:
    import struct
    blob = struct.pack(f"{len(query_vec)}f", *query_vec)
    with connect() as conn:
        _ensure_cards_vec(conn)
        _ensure_embedding_dim(conn, len(query_vec))
        sql = """SELECT v.card_id AS id, v.distance AS score
                 FROM cards_vec v
                 JOIN cards c ON c.id = v.card_id
                 WHERE v.embedding MATCH ?
                   AND k = ?"""
        params: list[Any] = [blob, top]
        if ns or type_ or tags:
            sub: list[str] = []
            if ns:
                sub.append("c.id LIKE ?")
                params.append(ns + ":%")
            if type_:
                sub.append("c.type = ?")
                params.append(type_)
            tag_sql, tag_params = _tag_filter_sql(tags, alias="c")
            if tag_sql:
                sub.append(tag_sql.removeprefix(" AND "))
                params.extend(tag_params)
            sql += " AND " + " AND ".join(sub)
        rows = conn.execute(sql, params).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        card_rows = conn.execute(
            f"""SELECT id, title, type, layer, source, origin, tags,
                       CASE WHEN layer='L1' THEN substr(content, 1, 200) ELSE NULL END AS snippet,
                       CASE WHEN layer='L1' THEN length(content) ELSE NULL END AS content_size
                FROM cards WHERE id IN ({placeholders})""",
            ids,
        ).fetchall()
        card_map = {r["id"]: dict(r) for r in card_rows}
        result = []
        for r in rows:
            c = card_map.get(r["id"])
            if c:
                c["score"] = r["score"]
                result.append(c)
        if bump_search:
            _bump_search_counts(conn, [r["id"] for r in result])
        return result


def search_hybrid(query: str, query_vec: list[float], top: int = 10,
                  ns: str | None = None, type_: str | None = None,
                  tags: list[str] | None = None) -> list[dict[str, Any]]:
    """RRF (Reciprocal Rank Fusion) of FTS + vector."""
    fts_results = search_fts(query, top=top * 3, ns=ns, type_=type_, tags=tags, bump_search=False)
    vec_results = search_vector(query_vec, top=top * 3, ns=ns, type_=type_, tags=tags, bump_search=False)
    rrf_k = 60
    scores: dict[str, float] = {}
    meta: dict[str, dict] = {}
    for rank, r in enumerate(fts_results):
        cid = r["id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        meta[cid] = {k: v for k, v in r.items() if k != "score"}
    for rank, r in enumerate(vec_results):
        cid = r["id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        meta[cid] = {k: v for k, v in r.items() if k != "score"}
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top]
    results = [{**meta[cid], "score": s} for cid, s in ranked]
    with connect() as conn:
        _bump_search_counts(conn, [r["id"] for r in results])
    return results


def _bump_search_counts(conn: sqlite3.Connection, ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE cards SET search_count=search_count+1 WHERE id IN ({placeholders})",
        ids,
    )


# ---------------------------------------------------------------------------
# L4 index
# ---------------------------------------------------------------------------

def rebuild_l4_index() -> int:
    """Scan all L4 cards, write data/l4_index.md. Returns count of L4 cards."""
    cards = list_cards_by_layer("L4")
    lines = ["# L4 索引（自动生成，勿手编）", "", "## 你可以调用的思考方向（Loom 沉淀的 L4 模式）", ""]
    for c in sorted(cards, key=lambda x: x["id"]):
        first_para = c["content"].lstrip().split("\n\n", 1)[0].replace("\n", " ").strip()
        # keep it compact: take first ~120 chars
        if len(first_para) > 200:
            first_para = first_para[:200].rstrip() + "…"
        lines.append(f"- {first_para}  (`{c['id']}`)")
    _write_private_text(L4_INDEX_PATH, "\n".join(lines) + "\n")
    return len(cards)


def rebuild_directory() -> int:
    """生成 orient.md：namespace 全貌 + L4 全量（含核心命题摘要）。
    与 rebuild_l4_index 的区别：l4_index 是简短单行列表，orient 是带命题摘要的立体目录。
    两者都是 pull 模式派生物——读时按需重建（cmd_orient / cmd_read_l4_index 带缓存检查）。
    返回 L4 卡数。
    """
    lines = ["# Loom 目录（自动生成，勿手编）", ""]

    # namespace 全貌
    with connect() as conn:
        rows = conn.execute("""
            SELECT substr(id, 1, instr(id, ':')-1) AS ns, COUNT(*) AS cnt
            FROM cards WHERE instr(id, ':') > 0
            GROUP BY ns ORDER BY ns
        """).fetchall()
    ns_counts = {r["ns"]: r["cnt"] for r in rows}
    total = sum(ns_counts.values())
    ns_line = " / ".join(f"{ns} ({cnt})" for ns, cnt in ns_counts.items())
    lines += [
        "## loom 全貌",
        f"{total} 张卡，{len(ns_counts)} 个 namespace：{ns_line}",
        "",
    ]

    # L4 全量 + 核心命题摘要
    l4_cards = list_cards_by_layer("L4")
    if l4_cards:
        lines += ["## L4 元层模式", ""]
        for c in sorted(l4_cards, key=lambda x: x["id"]):
            first_para = c["content"].lstrip().split("\n\n", 1)[0].strip()
            lines.append(f"### `{c['id']}` {c['title']}")
            lines.append("")
            lines.append(first_para)
            lines.append("")

    _write_private_text(ORIENT_PATH, "\n".join(lines) + "\n")
    return len(l4_cards)


# ---------------------------------------------------------------------------
# Task trace
# ---------------------------------------------------------------------------

def start_task(task_id: str, plan: dict[str, Any], session_id: str = "") -> None:
    """Register task start. Idempotent: 已 done/failed/salvaged 的任务不覆盖（防 hook 重复触发）。

    session_id 用于追踪"创建 task 的 agent runtime session"——hook 据此过滤，
    避免在 batch agent 模式下扫到别的 session 的 task 写出无人接收的 block 信号。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT status, session_id FROM task_trace WHERE task_id=?", (task_id,)
        ).fetchone()
        if row and row["status"] in ("done", "failed", "salvaged"):
            return  # 任务已完成，不重复注册
        conn.execute(
            "INSERT INTO task_trace(task_id, plan_json, started_at, status, session_id) "
            "VALUES (?,?,?, 'running', ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "  plan_json=excluded.plan_json, "
            "  started_at=excluded.started_at, "
            "  status='running', "
            "  session_id=CASE "
            "    WHEN excluded.session_id != '' THEN excluded.session_id "
            "    ELSE task_trace.session_id "
            "  END "
            "WHERE task_trace.status NOT IN ('done', 'failed', 'salvaged')",
            (task_id, json.dumps(plan, ensure_ascii=False), now(), session_id),
        )


def end_task(task_id: str, status: str, drafts_count: int = 0,
             committed_count: int = 0, retries: int = 0,
             set_ended_at: bool = True,
             committed_ids: list[str] | None = None) -> None:
    """更新 task_trace 状态。

    status 取值：running / computed_passed / done / failed / timeout / salvaged
    - computed_passed 是中间状态（计算层通过、等语义自检），set_ended_at=False
      不写 ended_at（任务没真正结束）
    - 终态（done/failed/timeout/salvaged）默认 set_ended_at=True
    """
    committed_ids_json = (
        json.dumps(committed_ids, ensure_ascii=False)
        if committed_ids is not None else None
    )
    with connect() as conn:
        fields = ["status=?", "drafts_count=?", "committed_count=?", "retries=?"]
        params: list[Any] = [status, drafts_count, committed_count, retries]
        if set_ended_at:
            fields.insert(0, "ended_at=?")
            params.insert(0, now())
        if committed_ids is not None:
            fields.append("committed_ids=?")
            params.append(committed_ids_json)
        params.append(task_id)
        conn.execute(
            f"UPDATE task_trace SET {', '.join(fields)} WHERE task_id=?",
            params,
        )


def log_reject(task_id: str | None, card_id: str | None,
               check_id: str, reason: str, stage: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO reject_log(ts, task_id, card_id, check_id, reason, stage) "
            "VALUES (?,?,?,?,?,?)",
            (now(), task_id, card_id, check_id, reason, stage),
        )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        by_layer: dict[str, int] = {}
        for r in conn.execute("SELECT layer, COUNT(*) FROM cards GROUP BY layer"):
            by_layer[r[0]] = r[1]
        by_type: dict[str, int] = {}
        for r in conn.execute("SELECT type, COUNT(*) FROM cards GROUP BY type"):
            by_type[r[0]] = r[1]
        by_origin: dict[str, int] = {}
        for r in conn.execute("SELECT origin, COUNT(*) FROM cards GROUP BY origin"):
            by_origin[r[0]] = r[1]
        tag_count = conn.execute("SELECT COUNT(DISTINCT tag) FROM card_tags").fetchone()[0]
        links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        orphans = conn.execute(
            """SELECT COUNT(*) FROM cards c WHERE
               NOT EXISTS (SELECT 1 FROM links l WHERE l.source_id = c.id)
               AND NOT EXISTS (SELECT 1 FROM links l WHERE l.target_id = c.id)"""
        ).fetchone()[0]
        active_use = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE use_count > 0").fetchone()[0]
        active_search = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE search_count > 0").fetchone()[0]
    return {
        "total_cards": total,
        "by_layer": by_layer,
        "by_type": by_type,
        "by_origin": by_origin,
        "tag_count": tag_count,
        "links_count": links_count,
        "orphan_cards": orphans,
        "orphan_rate": (orphans / total) if total else 0.0,
        "cards_with_use_count": active_use,
        "cards_with_search_count": active_search,
    }


def namespaces() -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT substr(id, 1, instr(id, ':')-1) AS ns FROM cards WHERE instr(id, ':') > 0").fetchall()
        return sorted(r["ns"] for r in rows if r["ns"])
