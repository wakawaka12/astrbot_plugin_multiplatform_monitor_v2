"""本地 SQLite 信息库：元数据/价格/缓存优先读本地。

图片二进制不入库（封面/头像仍走文件与在线 URL）。
库路径：{data_dir}/local_store.db
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None
_DB_PATH: str = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT,
  ts REAL
);
CREATE TABLE IF NOT EXISTS games (
  appid TEXT PRIMARY KEY,
  name TEXT,
  name_zh TEXT,
  name_en TEXT,
  itad_id TEXT,
  content_type TEXT,
  publishers TEXT,
  header_image_url TEXT,
  parent_appid TEXT,
  type_source TEXT,
  type_checked_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS prices (
  appid TEXT NOT NULL,
  region TEXT NOT NULL,
  currency TEXT,
  current_price REAL,
  regular_price REAL,
  cut INTEGER,
  lowest REAL,
  lowest_currency TEXT,
  lowest_note TEXT,
  observed_at REAL,
  source TEXT,
  PRIMARY KEY (appid, region)
);
CREATE TABLE IF NOT EXISTS sale_ends (
  appid TEXT PRIMARY KEY,
  end_ts INTEGER,
  end_text TEXT,
  fetched_at REAL
);
CREATE TABLE IF NOT EXISTS search_map (
  title_key TEXT PRIMARY KEY,
  appid TEXT,
  source TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS wish_cache (
  sid TEXT PRIMARY KEY,
  appids_json TEXT,
  player_name TEXT,
  fetched_at REAL,
  ts REAL
);
CREATE TABLE IF NOT EXISTS wish_sale_state (
  sid TEXT PRIMARY KEY,
  last_cuts_json TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS wish_sale_settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS bind_data (
  qq TEXT PRIMARY KEY,
  sid TEXT,
  nickname TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS steam_groups (
  group_id TEXT PRIMARY KEY,
  steam_ids_json TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS owned_games (
  sid TEXT PRIMARY KEY,
  appids_json TEXT,
  names_json TEXT,
  sig TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS perfect_scan (
  sid TEXT PRIMARY KEY,
  games_json TEXT,
  checked INTEGER,
  owned_sig TEXT,
  ts REAL
);
CREATE TABLE IF NOT EXISTS price_history (
  appid TEXT NOT NULL,
  region_label TEXT NOT NULL,
  price REAL,
  currency TEXT,
  date TEXT,
  PRIMARY KEY (appid, region_label)
);
CREATE TABLE IF NOT EXISTS hltb_cache (
  key TEXT PRIMARY KEY,
  payload TEXT,
  ts REAL
);
CREATE INDEX IF NOT EXISTS idx_prices_cut ON prices(cut);
CREATE INDEX IF NOT EXISTS idx_search_appid ON search_map(appid);
CREATE INDEX IF NOT EXISTS idx_games_itad ON games(itad_id);
"""


def _normalize_title_key(title: str) -> str:
    t = str(title or "").strip().casefold()
    t = re.sub(r"\s+", "", t)
    return t


def get_store(data_dir: str = "") -> sqlite3.Connection:
    """获取（或打开）本地库连接。data_dir 变化时重开。"""
    global _CONN, _DB_PATH
    path = os.path.join(str(data_dir or ""), "local_store.db")
    with _LOCK:
        if _CONN is not None and _DB_PATH == path:
            try:
                _CONN.execute("SELECT 1")
                return _CONN
            except sqlite3.Error:
                try:
                    _CONN.close()
                except Exception:
                    pass
                _CONN = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        # 旧库升级：补充 DLC/本体关联字段
        for col, decl in (
            ("parent_appid", "TEXT"),
            ("type_source", "TEXT"),
            ("type_checked_at", "REAL"),
        ):
            try:
                conn.execute(f"ALTER TABLE games ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_games_type ON games(content_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_games_parent ON games(parent_appid)")
        except sqlite3.Error:
            pass
        _CONN = conn
        _DB_PATH = path
        return conn


def db_path(data_dir: str = "") -> str:
    return os.path.join(str(data_dir or ""), "local_store.db")


def close_store() -> None:
    global _CONN, _DB_PATH
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None
        _DB_PATH = ""


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key,value,ts) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
        (str(key), json.dumps(value, ensure_ascii=False), time.time()),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (str(key),)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def upsert_game(conn: sqlite3.Connection, **fields) -> None:
    appid = str(fields.get("appid") or "").strip()
    if not appid:
        return
    # 确保列存在（旧库）
    try:
        conn.execute("SELECT parent_appid FROM games LIMIT 1")
    except sqlite3.OperationalError:
        for col, decl in (
            ("parent_appid", "TEXT"),
            ("type_source", "TEXT"),
            ("type_checked_at", "REAL"),
        ):
            try:
                conn.execute(f"ALTER TABLE games ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
    conn.execute(
        """
        INSERT INTO games(appid,name,name_zh,name_en,itad_id,content_type,publishers,header_image_url,parent_appid,type_source,type_checked_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(appid) DO UPDATE SET
          name=COALESCE(excluded.name, games.name),
          name_zh=COALESCE(excluded.name_zh, games.name_zh),
          name_en=COALESCE(excluded.name_en, games.name_en),
          itad_id=COALESCE(excluded.itad_id, games.itad_id),
          content_type=COALESCE(excluded.content_type, games.content_type),
          publishers=COALESCE(excluded.publishers, games.publishers),
          header_image_url=COALESCE(excluded.header_image_url, games.header_image_url),
          parent_appid=COALESCE(excluded.parent_appid, games.parent_appid),
          type_source=COALESCE(excluded.type_source, games.type_source),
          type_checked_at=COALESCE(excluded.type_checked_at, games.type_checked_at),
          updated_at=excluded.updated_at
        """,
        (
            appid,
            fields.get("name"),
            fields.get("name_zh"),
            fields.get("name_en"),
            fields.get("itad_id"),
            fields.get("content_type"),
            json.dumps(fields.get("publishers"), ensure_ascii=False)
            if isinstance(fields.get("publishers"), (list, tuple))
            else fields.get("publishers"),
            fields.get("header_image_url"),
            fields.get("parent_appid"),
            fields.get("type_source"),
            fields.get("type_checked_at"),
            float(fields.get("updated_at") or time.time()),
        ),
    )


def iter_library_appids(conn: sqlite3.Connection) -> list[str]:
    """全员库存并集（去重 appid）。"""
    rows = conn.execute("SELECT appids_json FROM owned_games").fetchall()
    s = set()
    for r in rows:
        try:
            for a in json.loads(r["appids_json"] or "[]"):
                a = str(a or "").strip()
                if a.isdigit():
                    s.add(a)
        except Exception:
            continue
    return sorted(s)


def pending_type_appids(conn: sqlite3.Connection, limit: int = 0) -> list[dict]:
    """待标注 type 的 appid：无 type_checked_at 或过旧。"""
    sql = (
        "SELECT appid, name, name_zh, content_type, parent_appid FROM games "
        "WHERE appid IN (SELECT appid FROM games) "
        "AND (type_checked_at IS NULL OR content_type IS NULL OR content_type='') "
        "ORDER BY appid"
    )
    rows = conn.execute(sql).fetchall()
    out = [dict(r) for r in rows]
    return out[:limit] if limit and limit > 0 else out


def apply_type_info(
    conn: sqlite3.Connection,
    appid: str,
    content_type: str = "",
    parent_appid: str = "",
    source: str = "",
    name: str = "",
) -> None:
    appid = str(appid or "").strip()
    if not appid.isdigit():
        return
    try:
        conn.execute("SELECT parent_appid FROM games LIMIT 1")
    except sqlite3.OperationalError:
        for col, decl in (
            ("parent_appid", "TEXT"),
            ("type_source", "TEXT"),
            ("type_checked_at", "REAL"),
        ):
            try:
                conn.execute(f"ALTER TABLE games ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
    now = time.time()
    conn.execute(
        """
        INSERT INTO games(appid,name,content_type,parent_appid,type_source,type_checked_at,updated_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(appid) DO UPDATE SET
          name=COALESCE(excluded.name, games.name),
          content_type=CASE WHEN excluded.content_type IS NOT NULL AND excluded.content_type!=''
            THEN excluded.content_type ELSE games.content_type END,
          parent_appid=COALESCE(excluded.parent_appid, games.parent_appid),
          type_source=COALESCE(excluded.type_source, games.type_source),
          type_checked_at=excluded.type_checked_at,
          updated_at=excluded.updated_at
        """,
        (
            appid,
            name or None,
            content_type or None,
            str(parent_appid) if parent_appid else None,
            source or None,
            now,
            now,
        ),
    )


def get_game(conn: sqlite3.Connection, appid: str):
    row = conn.execute("SELECT * FROM games WHERE appid=?", (str(appid),)).fetchone()
    return dict(row) if row else None


def list_dlc_for_parent(conn: sqlite3.Connection, parent_appid: str, sample: int = 3):
    """本体→DLC 摘要：返回 (total, sample_names)。

    查价只展示限量示例，不全量挂 DLC（Train Simulator 等可有数百个）。
    """
    pid = str(parent_appid or "").strip()
    if not pid.isdigit():
        return 0, []
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM games WHERE parent_appid=? AND content_type='dlc'",
            (pid,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0, []
    names = []
    if total:
        try:
            # 优先较短名，减少「本体名+超长后缀」同质示例
            rows = conn.execute(
                """
                SELECT appid, name FROM games
                WHERE parent_appid=? AND content_type='dlc'
                  AND name IS NOT NULL AND trim(name)!=''
                ORDER BY LENGTH(name), appid
                LIMIT ?
                """,
                (pid, max(0, int(sample) * 4)),
            ).fetchall()
            seen = set()
            for r in rows:
                nm = (r["name"] if isinstance(r, sqlite3.Row) else r[1]) or ""
                nm = str(nm).strip()
                key = nm.casefold()
                if nm and key not in seen:
                    seen.add(key)
                    names.append(nm)
                if len(names) >= max(0, int(sample)):
                    break
        except sqlite3.OperationalError:
            pass
    return int(total or 0), names


def get_parent_of_app(conn: sqlite3.Connection, appid: str):
    """查条目的 parent_appid（DLC/版本包 → 本体）。"""
    aid = str(appid or "").strip()
    if not aid.isdigit():
        return ""
    try:
        row = conn.execute(
            "SELECT parent_appid FROM games WHERE appid=?", (aid,)
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    if not row:
        return ""
    return str(row["parent_appid"] or "").strip()


def upsert_price(conn: sqlite3.Connection, appid: str, region: str, **fields) -> None:
    appid = str(appid or "").strip()
    region = str(region or "").strip()
    if not appid or not region:
        return
    conn.execute(
        """
        INSERT INTO prices(appid,region,currency,current_price,regular_price,cut,lowest,lowest_currency,lowest_note,observed_at,source)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(appid,region) DO UPDATE SET
          currency=excluded.currency,
          current_price=excluded.current_price,
          regular_price=excluded.regular_price,
          cut=excluded.cut,
          lowest=COALESCE(excluded.lowest, prices.lowest),
          lowest_currency=COALESCE(excluded.lowest_currency, prices.lowest_currency),
          lowest_note=COALESCE(excluded.lowest_note, prices.lowest_note),
          observed_at=excluded.observed_at,
          source=excluded.source
        """,
        (
            appid,
            region,
            fields.get("currency"),
            fields.get("current_price"),
            fields.get("regular_price"),
            fields.get("cut"),
            fields.get("lowest"),
            fields.get("lowest_currency"),
            fields.get("lowest_note"),
            float(fields.get("observed_at") or time.time()),
            fields.get("source") or "migrate",
        ),
    )


def get_prices_for_appid(conn: sqlite3.Connection, appid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM prices WHERE appid=? ORDER BY region", (str(appid),)
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_sale_end(conn: sqlite3.Connection, appid: str, end_ts=None, end_text=None) -> None:
    appid = str(appid or "").strip()
    if not appid:
        return
    conn.execute(
        """
        INSERT INTO sale_ends(appid,end_ts,end_text,fetched_at) VALUES(?,?,?,?)
        ON CONFLICT(appid) DO UPDATE SET
          end_ts=excluded.end_ts, end_text=excluded.end_text, fetched_at=excluded.fetched_at
        """,
        (appid, end_ts, end_text, time.time()),
    )


def get_sale_end(conn: sqlite3.Connection, appid: str, max_age_sec: float = 21600):
    row = conn.execute(
        "SELECT * FROM sale_ends WHERE appid=?", (str(appid),)
    ).fetchone()
    if not row:
        return None
    age = time.time() - float(row["fetched_at"] or 0)
    if max_age_sec and age > max_age_sec:
        return None
    return dict(row)


def set_search_map(conn: sqlite3.Connection, title: str, appid: str, source: str = "") -> None:
    key = _normalize_title_key(title)
    appid = str(appid or "").strip()
    if not key or not appid.isdigit():
        return
    conn.execute(
        """
        INSERT INTO search_map(title_key,appid,source,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(title_key) DO UPDATE SET appid=excluded.appid, source=excluded.source, updated_at=excluded.updated_at
        """,
        (key, appid, source, time.time()),
    )


def get_search_appid(conn: sqlite3.Connection, title: str) -> str:
    key = _normalize_title_key(title)
    if not key:
        return ""
    row = conn.execute("SELECT appid FROM search_map WHERE title_key=?", (key,)).fetchone()
    return str(row["appid"] if row else "") or ""


def set_wish_cache(conn: sqlite3.Connection, sid: str, appids: Iterable, player_name: str = "", fetched_at=None) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    conn.execute(
        """
        INSERT INTO wish_cache(sid,appids_json,player_name,fetched_at,ts) VALUES(?,?,?,?,?)
        ON CONFLICT(sid) DO UPDATE SET
          appids_json=excluded.appids_json, player_name=COALESCE(excluded.player_name, wish_cache.player_name),
          fetched_at=excluded.fetched_at, ts=excluded.ts
        """,
        (
            sid,
            json.dumps(list(appids or []), ensure_ascii=False),
            player_name,
            float(fetched_at or time.time()),
            time.time(),
        ),
    )


def get_wish_cache(conn: sqlite3.Connection, sid: str, max_age_sec: float = 0):
    row = conn.execute("SELECT * FROM wish_cache WHERE sid=?", (str(sid),)).fetchone()
    if not row:
        return None
    if max_age_sec and time.time() - float(row["fetched_at"] or 0) > max_age_sec:
        return None
    data = dict(row)
    try:
        data["appids"] = json.loads(data.get("appids_json") or "[]")
    except Exception:
        data["appids"] = []
    return data


def set_wish_sale_cuts(conn: sqlite3.Connection, sid: str, last_cuts: dict) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    conn.execute(
        """
        INSERT INTO wish_sale_state(sid,last_cuts_json,updated_at) VALUES(?,?,?)
        ON CONFLICT(sid) DO UPDATE SET last_cuts_json=excluded.last_cuts_json, updated_at=excluded.updated_at
        """,
        (sid, json.dumps(last_cuts or {}, ensure_ascii=False), time.time()),
    )


def get_wish_sale_cuts(conn: sqlite3.Connection, sid: str) -> dict:
    row = conn.execute(
        "SELECT last_cuts_json FROM wish_sale_state WHERE sid=?", (str(sid),)
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["last_cuts_json"] or "{}")
    except Exception:
        return {}


def set_wish_sale_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO wish_sale_settings(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(key), json.dumps(value, ensure_ascii=False), time.time()),
    )


def get_wish_sale_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM wish_sale_settings WHERE key=?", (str(key),)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def set_bind(conn: sqlite3.Connection, qq: str, sid: str, nickname: str = "") -> None:
    qq = str(qq or "").strip()
    if not qq:
        return
    conn.execute(
        """
        INSERT INTO bind_data(qq,sid,nickname,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(qq) DO UPDATE SET sid=excluded.sid, nickname=excluded.nickname, updated_at=excluded.updated_at
        """,
        (qq, str(sid or ""), str(nickname or ""), time.time()),
    )


def get_bind_map(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT qq,sid,nickname FROM bind_data").fetchall()
    return {
        r["qq"]: {"sid": r["sid"], "nickname": r["nickname"]}
        for r in rows
    }


def set_group(conn: sqlite3.Connection, group_id: str, steam_ids: Iterable) -> None:
    gid = str(group_id or "").strip()
    if not gid:
        return
    conn.execute(
        """
        INSERT INTO steam_groups(group_id,steam_ids_json,updated_at) VALUES(?,?,?)
        ON CONFLICT(group_id) DO UPDATE SET steam_ids_json=excluded.steam_ids_json, updated_at=excluded.updated_at
        """,
        (gid, json.dumps(list(steam_ids or []), ensure_ascii=False), time.time()),
    )


def get_groups(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT group_id,steam_ids_json FROM steam_groups").fetchall()
    out = {}
    for r in rows:
        try:
            out[str(r["group_id"])] = json.loads(r["steam_ids_json"] or "[]")
        except Exception:
            out[str(r["group_id"])] = []
    return out


def set_owned_games(conn: sqlite3.Connection, sid: str, appids: Iterable, names: Optional[dict] = None, sig: str = "") -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    conn.execute(
        """
        INSERT INTO owned_games(sid,appids_json,names_json,sig,updated_at) VALUES(?,?,?,?,?)
        ON CONFLICT(sid) DO UPDATE SET
          appids_json=excluded.appids_json, names_json=excluded.names_json,
          sig=excluded.sig, updated_at=excluded.updated_at
        """,
        (
            sid,
            json.dumps(list(appids or []), ensure_ascii=False),
            json.dumps(names or {}, ensure_ascii=False),
            str(sig or ""),
            time.time(),
        ),
    )


def get_owned_games(conn: sqlite3.Connection, sid: str):
    row = conn.execute("SELECT * FROM owned_games WHERE sid=?", (str(sid),)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["appids"] = json.loads(data.get("appids_json") or "[]")
    except Exception:
        data["appids"] = []
    try:
        data["names"] = json.loads(data.get("names_json") or "{}")
    except Exception:
        data["names"] = {}
    return data


def set_perfect_scan(conn: sqlite3.Connection, sid: str, games: list, checked: int = 0, owned_sig: str = "") -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    conn.execute(
        """
        INSERT INTO perfect_scan(sid,games_json,checked,owned_sig,ts) VALUES(?,?,?,?,?)
        ON CONFLICT(sid) DO UPDATE SET
          games_json=excluded.games_json, checked=excluded.checked,
          owned_sig=excluded.owned_sig, ts=excluded.ts
        """,
        (sid, json.dumps(games or [], ensure_ascii=False), int(checked or 0), str(owned_sig or ""), time.time()),
    )


def get_perfect_scan(conn: sqlite3.Connection, sid: str):
    row = conn.execute("SELECT * FROM perfect_scan WHERE sid=?", (str(sid),)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["games"] = json.loads(data.get("games_json") or "[]")
    except Exception:
        data["games"] = []
    return data


def set_price_history(conn: sqlite3.Connection, appid: str, region_label: str, price, currency: str = "", date: str = "") -> None:
    appid = str(appid or "").strip()
    region_label = str(region_label or "").strip()
    if not appid or not region_label or price is None:
        return
    # 仅保留更低观测价
    row = conn.execute(
        "SELECT price FROM price_history WHERE appid=? AND region_label=?",
        (appid, region_label),
    ).fetchone()
    if row and row["price"] is not None and float(price) >= float(row["price"]):
        return
    conn.execute(
        """
        INSERT INTO price_history(appid,region_label,price,currency,date) VALUES(?,?,?,?,?)
        ON CONFLICT(appid,region_label) DO UPDATE SET
          price=excluded.price, currency=excluded.currency, date=excluded.date
        """,
        (appid, region_label, float(price), str(currency or ""), str(date or "")),
    )


def get_price_history_map(conn: sqlite3.Connection, appid: str) -> dict:
    rows = conn.execute(
        "SELECT region_label,price,currency,date FROM price_history WHERE appid=?",
        (str(appid),),
    ).fetchall()
    return {
        r["region_label"]: {"price": r["price"], "currency": r["currency"], "date": r["date"]}
        for r in rows
    }


def set_hltb(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    conn.execute(
        """
        INSERT INTO hltb_cache(key,payload,ts) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, ts=excluded.ts
        """,
        (str(key), json.dumps(payload, ensure_ascii=False), time.time()),
    )


def stats(conn: sqlite3.Connection) -> dict:
    tables = [
        "meta", "games", "prices", "sale_ends", "search_map", "wish_cache",
        "wish_sale_state", "wish_sale_settings", "bind_data", "steam_groups",
        "owned_games", "perfect_scan", "price_history", "hltb_cache",
    ]
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        except sqlite3.Error:
            out[t] = -1
    return out
