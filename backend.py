# -*- coding: utf-8 -*-
"""
后端对接层 —— 两种模式，按需启用

模式 A（推荐）：HTTP 接口
    机器人 -> 调你后端的 REST 接口 -> 后端查库/写库 -> 返回 JSON
    好处：数据库密码不用落在这台 Windows 电脑上；业务逻辑在后端，改起来方便。

模式 B：直连 MySQL
    机器人 -> 直接连数据库
    好处：不用后端配合，能直接查。
    坏处：Windows 机器上要存数据库密码；这台机器还得能连到库。

两个可以同时开，也可以只开一个，不配置就自动禁用。

安全设计：
  - 写操作默认关闭（allow_write=False），要显式打开
  - SQL 一律走参数化（%s 占位符），不做字符串拼接，防注入
  - 群消息里的内容永远只能作为【参数】传入，不能拼进 SQL 语句本身
"""

import json
import time
import logging
import threading

try:
    import requests
except ImportError:
    requests = None

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    pymysql = None
    DictCursor = None

log = logging.getLogger("bot")


# ====================================================================
# 模式 A：HTTP 后端接口
# ====================================================================
class BackendAPI:
    def __init__(self, base_url="", token="", timeout=10, retries=2,
                 headers=None, token_header="Authorization",
                 token_prefix="Bearer "):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.extra_headers = headers or {}

    @property
    def enabled(self):
        return bool(self.base_url) and requests is not None

    def _headers(self):
        h = {"Content-Type": "application/json"}
        h.update(self.extra_headers)
        if self.token:
            h[self.token_header] = f"{self.token_prefix}{self.token}"
        return h

    def get(self, path, params=None):
        return self._call("GET", path, params=params)

    def post(self, path, data=None):
        return self._call("POST", path, data=data)

    def _call(self, method, path, params=None, data=None):
        """成功返回解析后的 dict/list；失败返回 None。"""
        if not self.enabled:
            log.warning("[后端] 未配置 base_url 或没装 requests，跳过")
            return None

        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

        for attempt in range(self.retries + 1):
            try:
                if method == "GET":
                    resp = requests.get(url, params=params,
                                        headers=self._headers(), timeout=self.timeout)
                else:
                    body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8")
                    resp = requests.post(url, data=body,
                                         headers=self._headers(), timeout=self.timeout)

                if resp.status_code != 200:
                    log.warning(f"[后端] {method} {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
                    if 400 <= resp.status_code < 500:
                        return None      # 客户端错误，重试也没用
                else:
                    try:
                        return resp.json()
                    except ValueError:
                        log.warning(f"[后端] 返回的不是 JSON: {resp.text[:200]}")
                        return None

            except Exception as e:
                log.warning(f"[后端] {method} {url} 异常({attempt + 1}/{self.retries + 1}): {e}")

            if attempt < self.retries:
                time.sleep(1 + attempt)

        return None


# ====================================================================
# 模式 B：直连 MySQL
# ====================================================================
class Database:
    def __init__(self, host="", port=3306, user="", password="", database="",
                 charset="utf8mb4", allow_write=False, timeout=10):
        self.cfg = dict(host=host, port=int(port), user=user, password=password,
                        database=database, charset=charset,
                        connect_timeout=timeout, autocommit=True)
        self.allow_write = allow_write
        self._conn = None
        self._lock = threading.Lock()   # 回调跑在监听线程，必须加锁

    @property
    def enabled(self):
        return bool(self.cfg["host"] and self.cfg["database"]) and pymysql is not None

    def _connect(self):
        if pymysql is None:
            log.error("[DB] 没装 pymysql: pip install pymysql")
            return None
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:
                self._conn = None
        try:
            self._conn = pymysql.connect(cursorclass=DictCursor, **self.cfg)
            log.info(f"[DB] 已连接 {self.cfg['host']}/{self.cfg['database']}")
        except Exception as e:
            log.error(f"[DB] 连接失败: {e}")
            self._conn = None
        return self._conn

    def query(self, sql, params=None, limit=50):
        """
        查询。sql 用 %s 占位符，params 传元组/列表。
        绝对不要把群消息内容直接拼进 sql —— 一律走 params。
        返回 list[dict]，失败返回 None。
        """
        if not self.enabled:
            log.warning("[DB] 未配置或没装 pymysql，跳过")
            return None

        with self._lock:
            conn = self._connect()
            if not conn:
                return None
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params or ())
                    rows = cur.fetchmany(limit)
                    return rows
            except Exception as e:
                log.error(f"[DB] 查询失败: {e} | SQL: {sql[:120]}")
                self._conn = None
                return None

    def execute(self, sql, params=None):
        """
        写操作（INSERT / UPDATE）。默认被 allow_write 挡住。
        返回影响行数，失败返回 None。
        """
        if not self.enabled:
            return None

        if not self.allow_write:
            log.warning("[DB] 写操作被拒绝：config 里 allow_write 是 False")
            return None

        # 这个机器人由群消息触发，DELETE / DROP / TRUNCATE 一律不允许，
        # 避免有人在群里发一句话就把表删了
        lowered = sql.strip().lower()
        for danger in ("delete", "drop", "truncate", "alter"):
            if lowered.startswith(danger):
                log.error(f"[DB] 拒绝执行 {danger.upper()} 语句。"
                          f"这类操作请走后端接口并做好鉴权，不要由群消息触发。")
                return None

        with self._lock:
            conn = self._connect()
            if not conn:
                return None
            try:
                with conn.cursor() as cur:
                    n = cur.execute(sql, params or ())
                    return n
            except Exception as e:
                log.error(f"[DB] 写入失败: {e} | SQL: {sql[:120]}")
                self._conn = None
                return None

    def close(self):
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


# ====================================================================
# 消息入库 —— 把群消息存下来
# ====================================================================
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS wechat_group_messages (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_name  VARCHAR(128) NOT NULL COMMENT '群名',
    sender      VARCHAR(128) NOT NULL COMMENT '发送者昵称',
    msg_type    VARCHAR(32)  DEFAULT NULL COMMENT '消息类型 text/image/...',
    msg_attr    VARCHAR(32)  DEFAULT NULL COMMENT '来源 friend/self/system',
    content     TEXT COMMENT '消息内容',
    replied     TINYINT(1) DEFAULT 0 COMMENT '机器人是否回复了',
    reply_text  TEXT COMMENT '机器人的回复内容',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_group_time (group_name, created_at),
    INDEX idx_sender (sender)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='微信群消息记录';
"""

INSERT_SQL = """
INSERT INTO wechat_group_messages
    (group_name, sender, msg_type, msg_attr, content, replied, reply_text)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class MessageStore:
    """消息入库。可以走数据库直连，也可以走后端接口。"""

    def __init__(self, db=None, api=None, api_path="", enabled=False):
        self.db = db
        self.api = api
        self.api_path = api_path
        self._enabled = enabled
        self._table_ready = False

    @property
    def enabled(self):
        if not self._enabled:
            return False
        return (self.db and self.db.enabled) or (self.api and self.api.enabled)

    def ensure_table(self):
        """第一次写入前自动建表。"""
        if self._table_ready or not (self.db and self.db.enabled):
            return
        if not self.db.allow_write:
            log.warning("[入库] allow_write=False，无法自动建表。"
                        "请手动执行 backend.py 里的 CREATE_TABLE_SQL")
            self._table_ready = True
            return
        with self.db._lock:
            conn = self.db._connect()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(CREATE_TABLE_SQL)
                    log.info("[入库] 消息表已就绪")
                except Exception as e:
                    log.error(f"[入库] 建表失败: {e}")
        self._table_ready = True

    def save(self, group, sender, content, msg_type="", msg_attr="",
             replied=False, reply_text=""):
        if not self.enabled:
            return

        # 优先走后端接口（更安全）
        if self.api and self.api.enabled and self.api_path:
            self.api.post(self.api_path, {
                "group_name": group, "sender": sender,
                "msg_type": msg_type, "msg_attr": msg_attr,
                "content": content,
                "replied": 1 if replied else 0,
                "reply_text": reply_text,
            })
            return

        if self.db and self.db.enabled:
            self.ensure_table()
            self.db.execute(INSERT_SQL, (
                group, sender, msg_type, msg_attr, content,
                1 if replied else 0, reply_text,
            ))


# ====================================================================
# 工厂
# ====================================================================
def build(cfg):
    """从配置构造 (api, db, store)。cfg 结构见 config.py 的 BACKEND 段。"""
    api_cfg = cfg.get("http", {})
    api = BackendAPI(
        base_url=api_cfg.get("base_url", ""),
        token=api_cfg.get("token", ""),
        timeout=api_cfg.get("timeout", 10),
        retries=api_cfg.get("retries", 2),
        headers=api_cfg.get("headers", {}),
        token_header=api_cfg.get("token_header", "Authorization"),
        token_prefix=api_cfg.get("token_prefix", "Bearer "),
    ) if api_cfg.get("enabled") else None

    db_cfg = cfg.get("mysql", {})
    db = Database(
        host=db_cfg.get("host", ""),
        port=db_cfg.get("port", 3306),
        user=db_cfg.get("user", ""),
        password=db_cfg.get("password", ""),
        database=db_cfg.get("database", ""),
        allow_write=db_cfg.get("allow_write", False),
        timeout=db_cfg.get("timeout", 10),
    ) if db_cfg.get("enabled") else None

    store_cfg = cfg.get("save_messages", {})
    store = MessageStore(
        db=db, api=api,
        api_path=store_cfg.get("api_path", ""),
        enabled=store_cfg.get("enabled", False),
    )

    return api, db, store


# ====================================================================
# 自测：python backend.py
# ====================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import config

    print("=" * 62)
    print("  后端连通性自测")
    print("=" * 62)

    api, db, store = build(config.BACKEND)

    print(f"\nHTTP 接口 : {'已启用' if (api and api.enabled) else '未启用'}")
    if api and api.enabled:
        print(f"  base_url = {api.base_url}")
        path = config.BACKEND.get("http", {}).get("health_path", "")
        if path:
            print(f"  测试 GET {path} ...")
            print(f"  返回: {api.get(path)}")

    print(f"\nMySQL     : {'已启用' if (db and db.enabled) else '未启用'}")
    if db and db.enabled:
        print(f"  {db.cfg['host']}:{db.cfg['port']}/{db.cfg['database']}")
        print(f"  写权限: {'开' if db.allow_write else '关（只读）'}")
        rows = db.query("SELECT 1 AS ok")
        print(f"  测试查询: {rows}")

    print(f"\n消息入库  : {'已启用' if store.enabled else '未启用'}")
    print()
