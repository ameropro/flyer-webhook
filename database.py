import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = Path("data.db")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            referrer_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            reward INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS task_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            proof TEXT,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sponsor_channels (
            chat_id TEXT PRIMARY KEY,
            title TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            reward INTEGER NOT NULL,
            expires_at TIMESTAMP,
            uses_left INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subscription_watch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            reward INTEGER NOT NULL,
            task_id INTEGER,
            due_at INTEGER NOT NULL,
            stage TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def add_user_if_not_exists(user_id: int, referrer_id: Optional[int] = None) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (id, balance, level, referrer_id) VALUES (?, 0, 1, ?)",
            (user_id, referrer_id),
        )
    elif referrer_id:
        cur.execute(
            "UPDATE users SET referrer_id = COALESCE(referrer_id, ?) WHERE id = ?",
            (referrer_id, user_id),
        )
    conn.commit()
    conn.close()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def increment_balance(user_id: int, delta: int) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (delta, user_id))
    conn.commit()
    conn.close()


def update_level(user_id: int, level: int) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET level = ? WHERE id = ?", (level, user_id))
    conn.commit()
    conn.close()


def create_task(
    task_type: str,
    title: str,
    description: str,
    reward: int,
    payload: Dict[str, Any],
    created_by: Optional[int] = None,
) -> int:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (type, title, description, reward, payload, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (task_type, title, description, reward, json.dumps(payload), created_by),
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


def list_tasks(limit: int = 20) -> List[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    conn.close()
    return row


def create_assignment(task_id: int, user_id: int, status: str = "pending") -> int:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO task_assignments (task_id, user_id, status) VALUES (?, ?, ?)",
        (task_id, user_id, status),
    )
    assignment_id = cur.lastrowid
    conn.commit()
    conn.close()
    return assignment_id


def get_assignment(assignment_id: int) -> Optional[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM task_assignments WHERE id = ?", (assignment_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_assignment_by_task_user(task_id: int, user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM task_assignments WHERE task_id = ? AND user_id = ?",
        (task_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


def update_assignment_status(
    assignment_id: int, status: str, proof: Optional[str] = None, comment: Optional[str] = None
) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE task_assignments SET status = ?, proof = COALESCE(?, proof), comment = ?,"
        " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, proof, comment, assignment_id),
    )
    conn.commit()
    conn.close()


def add_sponsor(chat_id: int, title: str) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO sponsor_channels (chat_id, title) VALUES (?, ?)",
        (chat_id, title),
    )
    conn.commit()
    conn.close()


def remove_sponsor(chat_id: int) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM sponsor_channels WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def list_sponsors() -> List[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sponsor_channels ORDER BY title")
    rows = cur.fetchall()
    conn.close()
    return rows


def create_promocode(code: str, reward: int, expires_at: Optional[str], uses_left: Optional[int]) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO promocodes (code, reward, expires_at, uses_left) VALUES (?, ?, ?, ?)",
        (code.upper(), reward, expires_at, uses_left),
    )
    conn.commit()
    conn.close()


def redeem_promocode(user_id: int, code: str) -> Tuple[bool, str, int]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, reward, expires_at, uses_left FROM promocodes WHERE code = ?",
        (code.upper(),),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        return False, "Промокод не найден", 0
    if row[2] and row[2] < datetime.utcnow().isoformat():
        conn.close()
        return False, "Срок действия промокода истёк", 0
    if row[3] is not None and row[3] <= 0:
        conn.close()
        return False, "Промокод больше не активен", 0
    cur.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (row[1], user_id))
    if row[3] is not None:
        cur.execute(
            "UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?",
            (row[0],),
        )
    conn.commit()
    conn.close()
    return True, "Промокод применён", row[1]


def schedule_subscription_watch(user_id: int, chat_id: str, reward: int, task_id: int, due_at: int, stage: str) -> int:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO subscription_watch (user_id, chat_id, reward, task_id, due_at, stage)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, chat_id, reward, task_id, due_at, stage),
    )
    watch_id = cur.lastrowid
    conn.commit()
    conn.close()
    return watch_id


def delete_subscription_watch(watch_id: int) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM subscription_watch WHERE id = ?", (watch_id,))
    conn.commit()
    conn.close()


def list_subscription_watches() -> List[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM subscription_watch")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_subscription_watch(watch_id: int) -> Optional[sqlite3.Row]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM subscription_watch WHERE id = ?", (watch_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_stats() -> Dict[str, int]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tasks")
    tasks = cur.fetchone()[0]
    cur.execute("SELECT SUM(balance) FROM users")
    balance_sum = cur.fetchone()[0] or 0
    conn.close()
    return {"users": users, "tasks": tasks, "balance_sum": balance_sum}
