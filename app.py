"""Local-only Agent Traffic Light. Python 3.10+, standard library only."""

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
SKU = "BOX-001"
ITEM_NAME = "示例貨品"
ORIGIN = "示例提案（固定規則，非即時 AI）"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RuleError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def exact_fields(body, required):
    if not isinstance(body, dict) or set(body) != set(required):
        raise RuleError("INVALID_FIELDS", "請求欄位不符；請重新整理後再操作。", 400)


def content_hash(task):
    # Revision prevents an old approval from becoming valid after an A -> B -> A edit.
    content = {key: task[key] for key in (
        "id", "revision", "origin", "source", "destination", "item", "quantity"
    )}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS warehouse (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    slot TEXT NOT NULL CHECK (slot IN ('A', 'B')),
                    item TEXT NOT NULL CHECK (item = 'BOX-001'),
                    quantity INTEGER NOT NULL CHECK (quantity = 1)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    origin TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source IN ('A', 'B')),
                    destination TEXT NOT NULL CHECK (destination IN ('A', 'B')),
                    item TEXT NOT NULL CHECK (item = 'BOX-001'),
                    quantity INTEGER NOT NULL CHECK (quantity = 1),
                    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','processed')),
                    fingerprint TEXT NOT NULL,
                    approved_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (source <> destination)
                );
                CREATE TABLE IF NOT EXISTS movements (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                    revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity = 1),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT REFERENCES tasks(id),
                    revision INTEGER,
                    action TEXT NOT NULL,
                    message TEXT NOT NULL,
                    before_slot TEXT NOT NULL,
                    after_slot TEXT NOT NULL,
                    content_json TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not conn.execute("SELECT 1 FROM warehouse").fetchone():
                    conn.execute("INSERT INTO warehouse VALUES (1, 'A', ?, 1)", (SKU,))
                    self._event(conn, None, "initialized", "本機倉儲初始化：1 件示例貨品位於 A。", "A")
                    self._create(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _slot(conn):
        return conn.execute("SELECT slot FROM warehouse WHERE id=1").fetchone()[0]

    @staticmethod
    def _task(conn, task_id):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise RuleError("NOT_FOUND", "找不到這筆提案。", 404)
        return dict(row)

    def _event(self, conn, task, action, message, before):
        conn.execute("""INSERT INTO events
            (task_id,revision,action,message,before_slot,after_slot,content_json,created_at)
            VALUES (?,?,?,?,?,?,?,?)""", (
            task["id"] if task else None, task["revision"] if task else None,
            action, message, before, self._slot(conn),
            json.dumps(task, ensure_ascii=False) if task else None, now()
        ))

    def _create(self, conn):
        slot = self._slot(conn)
        task = dict(id=str(uuid.uuid4()), revision=1, origin=ORIGIN,
                    source=slot, destination="B" if slot == "A" else "A",
                    item=SKU, quantity=1, status="pending", approved_fingerprint=None,
                    created_at=now(), updated_at=now())
        task["fingerprint"] = content_hash(task)
        conn.execute("""INSERT INTO tasks
            (id,revision,origin,source,destination,item,quantity,status,
             approved_fingerprint,created_at,updated_at,fingerprint)
            VALUES (:id,:revision,:origin,:source,:destination,:item,:quantity,:status,
             :approved_fingerprint,:created_at,:updated_at,:fingerprint)""", task)
        self._event(conn, task, "created", "建立示例提案；等待核准，貨品未移動。", slot)
        return task["id"]

    def _snapshot(self, conn):
        warehouse = dict(conn.execute("SELECT slot,item,quantity FROM warehouse WHERE id=1").fetchone())
        movements = {row["task_id"]: dict(row) for row in conn.execute("SELECT * FROM movements")}
        tasks = []
        for row in conn.execute("SELECT * FROM tasks ORDER BY rowid DESC"):
            task = dict(row)
            task["movement"] = movements.get(task["id"])
            tasks.append(task)
        events = []
        for row in conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 100"):
            event = dict(row)
            event["content"] = json.loads(event.pop("content_json") or "null")
            events.append(event)
        warehouse["bins"] = {slot: int(warehouse["slot"] == slot) for slot in ("A", "B")}
        warehouse["item_name"] = ITEM_NAME
        return dict(version=events[0]["seq"] if events else 0, warehouse=warehouse,
                    movement_count=len(movements), tasks=tasks, events=events, confirmed_at=now())

    def snapshot(self):
        with self.connection() as conn:
            conn.execute("BEGIN")
            result = self._snapshot(conn)
            conn.commit()
            return result

    @staticmethod
    def _match(task, body):
        if (type(body["expected_revision"]) is not int
                or body["expected_revision"] != task["revision"]
                or body["expected_fingerprint"] != task["fingerprint"]):
            raise RuleError("STALE_CONTENT", "提案內容或版本已變更；請檢視最新內容後重新核准。")

    def _apply(self, conn, action, task_id, body):
        if action == "create":
            exact_fields(body, ())
            return dict(code="CREATED", message="已建立新的示例提案。", task_id=self._create(conn), moved=False)
        task = self._task(conn, task_id)
        fields = ["expected_revision", "expected_fingerprint"]
        if action == "edit":
            fields += ["source", "destination", "item", "quantity"]
        exact_fields(body, fields)
        self._match(task, body)
        slot = self._slot(conn)
        if action == "execute" and task["status"] == "processed":
            receipt = dict(conn.execute("SELECT * FROM movements WHERE task_id=?", (task_id,)).fetchone())
            self._event(conn, task, "duplicate_ignored", "此任務已處理；重複送出未新增搬運。", slot)
            return dict(code="ALREADY_PROCESSED", message="此任務已完成，沒有再次搬運。",
                        task_id=task_id, moved=False, movement=receipt)
        if task["status"] in ("rejected", "processed"):
            raise RuleError("TERMINAL_TASK", "已拒絕或已處理的任務不可重新啟用；請建立新提案。")
        if action == "edit":
            if (body["source"] not in ("A", "B") or body["destination"] not in ("A", "B")
                    or body["source"] == body["destination"] or body["item"] != SKU
                    or type(body["quantity"]) is not int or body["quantity"] != 1):
                raise RuleError("INVALID_PROPOSAL", "M1 僅支援 A、B 間搬運 1 件 BOX-001。", 422)
            keys = ("source", "destination", "item", "quantity")
            if all(task[key] == body[key] for key in keys):
                raise RuleError("NO_CHANGE", "內容相同，沒有建立新版本。", 422)
            task.update({key: body[key] for key in keys})
            task.update(revision=task["revision"] + 1, status="pending", approved_fingerprint=None, updated_at=now())
            task["fingerprint"] = content_hash(task)
            conn.execute("""UPDATE tasks SET revision=:revision, source=:source, destination=:destination,
                item=:item, quantity=:quantity, status=:status, approved_fingerprint=NULL,
                fingerprint=:fingerprint, updated_at=:updated_at WHERE id=:id""", task)
            message = "內容已修改；原核准失效，必須重新核准。"
            self._event(conn, task, "edited", message, slot)
            return dict(code="EDITED", message=message, task_id=task_id, moved=False)
        if action == "approve":
            conn.execute("UPDATE tasks SET status='approved', approved_fingerprint=fingerprint, updated_at=? WHERE id=?",
                         (now(), task_id))
            message = "已核准此版本；尚未搬運。"
        elif action == "reject":
            conn.execute("UPDATE tasks SET status='rejected', approved_fingerprint=NULL, updated_at=? WHERE id=?",
                         (now(), task_id))
            message = "已拒絕提案；貨品未移動。"
        elif action == "execute":
            if task["status"] != "approved":
                raise RuleError("NOT_APPROVED", "尚未核准，後端已阻止搬運。")
            if task["approved_fingerprint"] != task["fingerprint"] or content_hash(task) != task["fingerprint"]:
                raise RuleError("APPROVAL_MISMATCH", "核准與目前內容不符；後端已阻止搬運。")
            if slot != task["source"]:
                raise RuleError("SOURCE_EMPTY", "來源貨格沒有這件貨品；後端已阻止搬運。")
            receipt = dict(id=str(uuid.uuid4()), task_id=task_id, revision=task["revision"],
                           fingerprint=task["fingerprint"], source=task["source"],
                           destination=task["destination"], item=task["item"],
                           quantity=task["quantity"], created_at=now())
            # These three writes and the audit event commit as one atomic transaction.
            conn.execute("""INSERT INTO movements
                (id,task_id,revision,fingerprint,source,destination,item,quantity,created_at)
                VALUES (:id,:task_id,:revision,:fingerprint,:source,:destination,:item,:quantity,:created_at)""", receipt)
            conn.execute("UPDATE warehouse SET slot=? WHERE id=1", (task["destination"],))
            conn.execute("UPDATE tasks SET status='processed', updated_at=? WHERE id=?", (now(), task_id))
            message = f"搬運完成：{task['source']} → {task['destination']}，BOX-001 × 1。"
            self._event(conn, self._task(conn, task_id), "executed", message, slot)
            return dict(code="EXECUTED", message=message, task_id=task_id, moved=True, movement=receipt)
        else:
            raise RuleError("UNKNOWN_ACTION", "不存在的操作。", 404)
        completed_action = {"approve": "approved", "reject": "rejected"}[action]
        self._event(conn, self._task(conn, task_id), completed_action, message, slot)
        return dict(code=completed_action.upper(), message=message, task_id=task_id, moved=False)

    def command(self, action, task_id, body):
        with self.connection() as conn:
            # SQLite serializes writers across threads AND separate server processes.
            conn.execute("BEGIN IMMEDIATE")
            before = self._slot(conn)
            conn.execute("SAVEPOINT operation")
            try:
                result = self._apply(conn, action, task_id, body)
                status = 200
                conn.execute("RELEASE operation")
            except RuleError as error:
                conn.execute("ROLLBACK TO operation")
                conn.execute("RELEASE operation")
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                self._event(conn, dict(row) if row else None, "blocked", error.message, before)
                result = dict(code=error.code, message=error.message, task_id=task_id, moved=False)
                status = error.status
            snapshot = self._snapshot(conn)
            conn.commit()
            return status, dict(ok=status == 200, result=result, state=snapshot)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentTrafficLight/1.0"

    def log_message(self, fmt, *args):
        print(f"[{now()}] {fmt % args}", file=sys.stderr)

    def _send(self, status, data, mime="application/json; charset=utf-8"):
        payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def _local_request(self, mutation=False):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            raise RuleError("INVALID_HOST", "僅接受本機存取。", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            raise RuleError("INVALID_ORIGIN", "不接受跨網站操作。", 403)
        if mutation and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise RuleError("JSON_REQUIRED", "操作必須使用 JSON。", 415)

    def do_GET(self):
        try:
            self._local_request()
            path = urlsplit(self.path).path
            if path == "/api/state":
                self._send(200, dict(ok=True, state=self.server.store.snapshot()))
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path not in assets:
                raise RuleError("NOT_FOUND", "找不到頁面。", 404)
            name, mime = assets[path]
            self._send(200, (ROOT / "static" / name).read_bytes(), mime)
        except RuleError as error:
            self._send(error.status, dict(ok=False, error=error.code, message=error.message))
        except Exception as error:
            self.log_error("GET failed: %s", error)
            self._send(500, dict(ok=False, message="本機讀取失敗；請查看伺服器紀錄。"))

    def do_POST(self):
        try:
            self._local_request(mutation=True)
            if self.headers.get("Transfer-Encoding"):
                raise RuleError("INVALID_LENGTH", "不支援此請求格式。", 400)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise RuleError("INVALID_LENGTH", "請求長度不正確。", 400)
            if not 0 < length <= 8192:
                raise RuleError("INVALID_LENGTH", "請求內容過大或為空。", 400)
            self.connection.settimeout(10)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise RuleError("INVALID_JSON", "JSON 格式不正確。", 400)
            if not isinstance(body, dict):
                raise RuleError("INVALID_JSON", "請提供 JSON 物件。", 400)
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts == ["api", "proposals"]:
                action, task_id = "create", None
            elif len(parts) == 4 and parts[:2] == ["api", "proposals"] and parts[3] in ("approve", "reject", "edit", "execute"):
                task_id, action = parts[2:]
            else:
                raise RuleError("NOT_FOUND", "不存在的操作。", 404)
            status, payload = self.server.store.command(action, task_id, body)
            self._send(status, payload)
        except RuleError as error:
            self._send(error.status, dict(ok=False, error=error.code, message=error.message))
        except Exception as error:
            self.log_error("POST failed: %s", error)
            self._send(500, dict(ok=False, message="本機操作結果未確認，請重新整理後檢查；不要假設已搬運。"))


def main():
    parser = argparse.ArgumentParser(description="Agent Traffic Light — local warehouse demo")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "warehouse.sqlite3")
    args = parser.parse_args()
    store = Store(args.db)
    try:
        server = Server(("127.0.0.1", args.port), store)
    except OSError as error:
        parser.exit(1, f"Cannot start local server: {error}\nTry --port 8766.\n")
    print(f"Agent Traffic Light: http://127.0.0.1:{server.server_address[1]}", flush=True)
    print(f"SQLite: {store.path}\nPress Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
