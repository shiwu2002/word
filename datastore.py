# -*- coding: utf-8 -*-
"""数据存储层：班级（由名单生成）+ 学生名单 + 版本快照（支持回退）。

数据模型：
- classes   班级（name 唯一）
- students  学生名单（class_id, name）；外键级联，删班级连带删名单
- versions  每次课时数据合并的全量快照版本；当前数据 = 最新版本

业务流程：
1. 导入名单文件 → 每个工作表创建/更新一个班级（表名即班名）及其学生名单。
2. 上传课时数据 → 按学生姓名路由到其所属班级，合并进该班级当前数据并生成新版本。
3. 学生名单是班级的“固定框架”：不在名单中的学生记录会被跳过；
   汇总时以名单为框架，未上课的学生也显示（统计为 0）。
4. 回退 = 删除某班级最新版本，回到上一版本。
"""
import os
import re
import sys
import json
import sqlite3
from datetime import datetime

# 数据库路径：打包后放 exe 同级目录（避免每次启动丢失）；开发时放源码目录。
if getattr(sys, "frozen", False):
    DB_PATH = os.path.join(os.path.dirname(sys.executable), "data.db")
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

# 默认班级名：上传课时时不在任何正式班级名单中的学生，会被记入这个默认班。
DEFAULT_CLASS_NAME = "默认名单"


def get_conn():
    """创建并返回数据库连接。开启 Row 行工厂与外键约束。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 行包装成 dict 风格，可用 row["name"] 取值
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite 默认不开启外键，需连接级打开
    return conn


def _ensure_writable(path):
    """Windows 只读属性会让 SQLite 写入报 'attempt to write a readonly database'
    （读、AI 生成都正常，最后保存时才失败）。启动时自动清除该属性，数据无损。"""
    try:
        if os.path.exists(path) and not os.access(path, os.W_OK):
            os.chmod(path, 0o666)
    except OSError:
        pass  # 清不掉时让写入路径自行报错，不阻断启动


def init_db():
    """初始化表结构（已存在则跳过，幂等）。

    students 表通过 FOREIGN KEY ... ON DELETE CASCADE 绑定 classes，
    删除班级时其名单与版本会自动级联删除。
    """
    _ensure_writable(DB_PATH)
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS classes (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS students (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            name     TEXT NOT NULL,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
            UNIQUE (class_id, name)              -- 同一班级学生不重复
        );
        CREATE TABLE IF NOT EXISTS versions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id      INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            data_json     TEXT NOT NULL,
            source_filename TEXT,
            record_count  INTEGER NOT NULL DEFAULT 0,
            new_count     INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
            UNIQUE (class_id, version_number)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS evaluations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id    INTEGER NOT NULL,
            batch_id    INTEGER NOT NULL DEFAULT 0,
            student_name TEXT NOT NULL,
            evaluation  TEXT NOT NULL,
            fields_used TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS achievement_results (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_name   TEXT NOT NULL,
            lesson_time   TEXT NOT NULL,
            student_name  TEXT NOT NULL,
            question_key  TEXT NOT NULL,
            result        TEXT NOT NULL,          -- 对 / 错
            correct_count INTEGER,
            score_rate    REAL,
            trophies      INTEGER,
            source_class  TEXT,
            created_at    TEXT NOT NULL,
            UNIQUE (lesson_name, student_name, question_key)
        );
        CREATE TABLE IF NOT EXISTS question_templates (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_name    TEXT NOT NULL,
            question_key   TEXT NOT NULL,
            qtype          TEXT,
            question_text  TEXT,
            options        TEXT,
            correct_option TEXT,
            knowledge_point TEXT,
            wrong_students TEXT,                  -- JSON [{name, choice}]
            reply_template TEXT DEFAULT '',
            updated_at     TEXT NOT NULL,
            UNIQUE (lesson_name, question_key)
        );
        """
    )
    # OJ 通过数（班级数据导出）：课后作业/课后拓展 OJ 通过数，总数在 parser.OJ_TOTAL 写死为 2
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oj_results (
            student_name TEXT NOT NULL,
            lesson_name  TEXT NOT NULL,
            hw_passed    INTEGER NOT NULL DEFAULT 0,  -- 课后作业OJ题通过数
            ext_passed   INTEGER NOT NULL DEFAULT 0,  -- 课后拓展OJ题通过数
            imported_at  TEXT NOT NULL,
            UNIQUE (student_name, lesson_name)
        );
        """
    )
    # 兼容已有数据库：若 evaluations 表缺少 batch_id 列则添加（已存在则忽略）
    try:
        conn.execute("ALTER TABLE evaluations ADD COLUMN batch_id INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    _normalize_lesson_names(conn)
    conn.commit()
    conn.close()


def _normalize_lesson_names(conn):
    """一次性清洗历史数据：课测/错题模板/OJ 表里旧版导入的长课节名统一为规范名。

    新版导入在解析时已规范化（parse_upload / parse_achievement / parse_oj_export），
    这里只处理旧版本代码入库的「U260724-xxx - P9-递推算法（二）」式长名——
    不清洗的话，明细记录/进步曲线等按「学生+课节名」匹配时会与课时数据对不上。
    目标规范名已存在（新旧数据并存）时跳过该条，避免撞 UNIQUE 约束。
    """
    from parser import canonical_lesson

    for table in ("achievement_results", "question_templates", "oj_results"):
        rows = conn.execute(f"SELECT DISTINCT lesson_name FROM {table}").fetchall()
        for r in rows:
            name = r["lesson_name"]
            canon = canonical_lesson(name)
            if not canon or canon == name:
                continue
            dup = conn.execute(
                f"SELECT COUNT(*) n FROM {table} WHERE lesson_name=?", (canon,)
            ).fetchone()["n"]
            if dup:
                continue  # 规范名已有数据（新版导入），保留现状不动旧行
            conn.execute(
                f"UPDATE {table} SET lesson_name=? WHERE lesson_name=?", (canon, name)
            )


def _now():
    """当前时间格式化字符串（内部辅助函数，下划线前缀表示内部用）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 班级与名单 ----------------

def list_classes():
    """所有班级，含当前版本号、版本数、学生数。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.*,
                  (SELECT MAX(version_number) FROM versions v WHERE v.class_id = c.id) AS current_version,
                  (SELECT COUNT(*) FROM versions v WHERE v.class_id = c.id) AS version_count,
                  (SELECT COUNT(*) FROM students s WHERE s.class_id = c.id) AS student_count
           FROM classes c ORDER BY c.id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_class(cid):
    """按 id 查单个班级，返回 dict；不存在返回 None。"""
    conn = get_conn()
    r = conn.execute("SELECT * FROM classes WHERE id=?", (cid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def rename_class(cid, name):
    """重命名班级。重名会抛 IntegrityError（唯一约束）。"""
    conn = get_conn()
    conn.execute("UPDATE classes SET name=? WHERE id=?", (name, cid))
    conn.commit()
    conn.close()


def delete_class(cid):
    """删除班级，外键级联删除其名单与全部版本。"""
    conn = get_conn()
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()
    conn.close()


def import_roster(classes_data):
    """导入名单：每个 {name, students} 创建或替换班级名单。

    - 班级名已存在 → 替换其名单（删旧插新），保留课时版本数据。
    - 班级名不存在 → 新建班级并写入名单。
    - 推班迁移：名单落库后按新旧名单差异迁移课时数据（见 _migrate_promotions）。
    返回 (result, migrations)：
      result: [{name, student_count, created}]
      migrations: [迁移说明字符串]，供页面 flash 提示。
    """
    now = _now()
    result = []
    conn = get_conn()
    try:
        # 替换名单前先快照各正式班现有学生：{学生姓名: {class_id, ...}}，用于判定推班
        old_map = {}
        for r in conn.execute(
            """SELECT s.name, s.class_id FROM students s
               JOIN classes c ON s.class_id = c.id WHERE c.name <> ?""",
            (DEFAULT_CLASS_NAME,),
        ):
            old_map.setdefault(r["name"], set()).add(r["class_id"])
        for item in classes_data:
            name = item["name"]
            students = item.get("students", [])
            row = conn.execute("SELECT id FROM classes WHERE name=?", (name,)).fetchone()
            if row:
                cid = row["id"]
                conn.execute("DELETE FROM students WHERE class_id=?", (cid,))  # 清旧名单
                created = False
            else:
                cur = conn.execute(
                    "INSERT INTO classes (name, created_at) VALUES (?, ?)", (name, now)
                )
                cid = cur.lastrowid  # 新插入行的主键
                created = True
            for sname in students:
                # INSERT OR IGNORE：遇到 (class_id,name) 重复时跳过，不报错。
                conn.execute(
                    "INSERT OR IGNORE INTO students (class_id, name) VALUES (?, ?)",
                    (cid, sname),
                )
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM students WHERE class_id=?", (cid,)
            ).fetchone()["c"]
            result.append({"name": name, "student_count": cnt, "created": created})
        conn.commit()
    finally:
        conn.close()  # 无论是否异常都关闭连接
    migrations = _migrate_promotions(old_map)  # 名单落库后再迁移课时数据
    return result, migrations


def _migrate_promotions(old_map):
    """名单导入后的课时数据迁移（推班 / 认领 / 留存）。

    规则（与「默认名单被正式班认领则迁移」的既有语义一致）：
    1. 推班：学生从 A 班旧名单消失且出现在 B 班新名单 → 课时数据从 A 班剥离、合入 B 班。
    2. 留存：学生离开所有正式班 → 课时数据转入「默认名单」，不丢数据。
    3. 认领：「默认名单」中的学生出现在新正式班名单 → 立即迁移到正式班。
    返回迁移说明列表（给页面 flash 用）。
    """
    from parser import merge_records  # 局部导入避免潜在循环依赖（同 _normalize_lesson_names）

    conn = get_conn()
    new_map = {}
    for r in conn.execute(
        """SELECT s.name, s.class_id FROM students s
           JOIN classes c ON s.class_id = c.id WHERE c.name <> ?""",
        (DEFAULT_CLASS_NAME,),
    ):
        new_map.setdefault(r["name"], set()).add(r["class_id"])
    conn.close()

    def cname(cid):
        c = get_class(cid)
        return c["name"] if c else str(cid)

    migrations = []

    # 规则 1/2：新旧正式班名单对比
    for name in sorted(set(old_map) | set(new_map)):
        old_cids = old_map.get(name, set())
        new_cids = new_map.get(name, set())
        leaves = sorted(old_cids - new_cids)   # 旧班中已不在新名单的
        joins = sorted(new_cids - old_cids)    # 新加入的正式班
        if not leaves:
            continue
        # 从旧班剥离该生记录（生成新版本快照）
        moved_records = []
        stripped_from = []
        for old_cid in leaves:
            records, _ = get_current_records(old_cid)
            student_records = [r for r in records if r["学生姓名"] == name]
            if not student_records:
                continue  # 该生在旧班没有课时数据，无需动版本
            kept = [r for r in records if r["学生姓名"] != name]
            save_version(old_cid, kept, "名单导入推班迁移", 0, 0)
            moved_records += student_records
            stripped_from.append(cname(old_cid))
        if not stripped_from:
            continue
        # 目标：新加入的正式班；一个都没进 → 转入默认名单留存
        to_default = not joins
        if to_default:
            default_cid, _ = get_or_create_default_class()
            add_students_to_class(default_cid, [name])
            targets = [default_cid]
        else:
            targets = joins
        for cid in targets:
            existing, _ = get_current_records(cid)
            merged, n_new, n_upd = merge_records(existing, moved_records)
            save_version(cid, merged, "名单导入推班迁移", n_new, n_upd)
        if to_default:
            migrations.append(
                f"{name}：已离开 {'、'.join(stripped_from)}，课时数据转入「默认名单」留存"
            )
        else:
            migrations.append(
                f"{name}：{'、'.join(stripped_from)} → "
                f"{'、'.join(cname(c) for c in targets)}，课时数据已随迁"
            )

    # 规则 3：默认名单学生被正式班认领 → 立即迁移
    default_cid = get_default_class_id()
    if default_cid:
        claimed = [n for n in get_roster(default_cid) if n in new_map]
        if claimed:
            remove_students_from_class(default_cid, claimed)
            default_records, _ = get_current_records(default_cid)
            kept = [r for r in default_records if r["学生姓名"] not in set(claimed)]
            if len(kept) != len(default_records):
                save_version(default_cid, kept, "名单导入认领迁移", 0, 0)
            for name in claimed:
                student_records = [r for r in default_records if r["学生姓名"] == name]
                dst = "、".join(cname(c) for c in sorted(new_map[name]))
                if student_records:
                    for cid in sorted(new_map[name]):
                        existing, _ = get_current_records(cid)
                        merged, n_new, n_upd = merge_records(existing, student_records)
                        save_version(cid, merged, "名单导入认领迁移", n_new, n_upd)
                    migrations.append(f"{name}：已从「默认名单」迁入 {dst}（含课时数据）")
                else:
                    migrations.append(f"{name}：已从「默认名单」认领至 {dst}")
    return migrations


def get_roster(cid):
    """返回班级学生名单（按姓名排序的列表）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM students WHERE class_id=? ORDER BY name", (cid,)
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def get_real_student_map():
    """返回 {学生姓名: [class_id, ...]}，用于课时数据路由。

    排除「默认名单」班——默认班只是未匹配学生的收容所，不应作为路由依据。
    这样一个学生只要在正式班名单中，就路由到正式班，而不会重复进默认班。

    【Python 基础】JOIN ... WHERE c.name <> ? 用参数排除默认班；
    setdefault(name, []).append(...) 实现按学生分组追加。
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.name, s.class_id FROM students s
           JOIN classes c ON s.class_id = c.id
           WHERE c.name <> ?""",
        (DEFAULT_CLASS_NAME,),
    ).fetchall()
    conn.close()
    m = {}
    for r in rows:
        m.setdefault(r["name"], []).append(r["class_id"])
    return m


def get_or_create_default_class():
    """返回默认班级 id，不存在则创建。返回 (cid, created)。"""
    conn = get_conn()
    row = conn.execute("SELECT id FROM classes WHERE name=?", (DEFAULT_CLASS_NAME,)).fetchone()
    if row:
        cid = row["id"]
        created = False
    else:
        cur = conn.execute(
            "INSERT INTO classes (name, created_at) VALUES (?, ?)",
            (DEFAULT_CLASS_NAME, _now()),
        )
        cid = cur.lastrowid
        created = True
    conn.commit()
    conn.close()
    return cid, created


def get_default_class_id():
    """返回默认班级 id，不存在返回 None（不创建）。"""
    conn = get_conn()
    row = conn.execute("SELECT id FROM classes WHERE name=?", (DEFAULT_CLASS_NAME,)).fetchone()
    conn.close()
    return row["id"] if row else None


def add_students_to_class(cid, names):
    """把学生加入指定班级名单（已存在则忽略）。"""
    if not names:
        return
    conn = get_conn()
    for name in names:
        # INSERT OR IGNORE：遇 (class_id,name) 重复时跳过，不报错。
        conn.execute(
            "INSERT OR IGNORE INTO students (class_id, name) VALUES (?, ?)", (cid, name)
        )
    conn.commit()
    conn.close()


def remove_students_from_class(cid, names):
    """从指定班级名单移除这些学生（不存在则忽略）。"""
    if not names:
        return
    conn = get_conn()
    for name in names:
        conn.execute("DELETE FROM students WHERE class_id=? AND name=?", (cid, name))
    conn.commit()
    conn.close()


def add_student_to_roster(cid, name):
    """班级内手动新增学生：登记名单；若该生在「默认名单」（含历史课时）则一并迁入本班。

    返回 (already_in_class, migrated, others)：
      already_in_class: 该生是否已在本班名单中（是则不做任何修改）
      migrated: 是否发生了「默认名单 → 本班」的课时数据迁移
      others: 该生同时在册的其他正式班 [{id, name}]（提示推班场景用）
    """
    conn = get_conn()
    dup = conn.execute(
        "SELECT 1 FROM students WHERE class_id=? AND name=?", (cid, name)
    ).fetchone()
    others = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT c.id, c.name FROM students s
               JOIN classes c ON s.class_id = c.id
               WHERE s.name = ? AND s.class_id <> ? AND c.name <> ?""",
            (name, cid, DEFAULT_CLASS_NAME),
        )
    ]
    conn.close()
    if dup:
        return True, False, others

    add_students_to_class(cid, [name])
    migrated = False
    default_cid = get_default_class_id()
    if default_cid and name in get_roster(default_cid):
        remove_students_from_class(default_cid, [name])
        default_records, _ = get_current_records(default_cid)
        student_records = [r for r in default_records if r["学生姓名"] == name]
        if student_records:
            from parser import merge_records

            kept = [r for r in default_records if r["学生姓名"] != name]
            save_version(default_cid, kept, "手动新增学生迁移", 0, 0)
            existing, _ = get_current_records(cid)
            merged, n_new, n_upd = merge_records(existing, student_records)
            save_version(cid, merged, "手动新增学生迁移", n_new, n_upd)
            migrated = True
    return False, migrated, others


def remove_student_from_roster(cid, name):
    """班级内手动删除学生：仅删名单行，课时数据随名单走、不丢失。

    - 该生同时在其他正式班名单 → 课时数据剥离本班、合入那些班（记录始终跟随在册班级）。
    - 否则 → 课时数据转入「默认名单」留存；日后再次加入正式班会自动随迁。
    返回 (existed, note)：existed 是否确有该生；note 为迁移说明（可能为空）。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM students WHERE class_id=? AND name=?", (cid, name)
    ).fetchone()
    other_cids = sorted(
        {
            r["class_id"]
            for r in conn.execute(
                """SELECT s.class_id FROM students s
                   JOIN classes c ON s.class_id = c.id
                   WHERE s.name = ? AND s.class_id <> ? AND c.name <> ?""",
                (name, cid, DEFAULT_CLASS_NAME),
            )
        }
    )
    conn.close()
    if not row:
        return False, ""

    conn = get_conn()
    conn.execute("DELETE FROM students WHERE class_id=? AND name=?", (cid, name))
    conn.commit()
    conn.close()

    records, _ = get_current_records(cid)
    student_records = [r for r in records if r["学生姓名"] == name]
    if not student_records:
        return True, ""  # 名单行已删，无课时数据可迁
    from parser import merge_records

    kept = [r for r in records if r["学生姓名"] != name]
    save_version(cid, kept, "删除学生迁移", 0, 0)
    if other_cids:
        for t in other_cids:
            existing, _ = get_current_records(t)
            merged, n_new, n_upd = merge_records(existing, student_records)
            save_version(t, merged, "删除学生迁移", n_new, n_upd)
        return True, "该生课时数据已迁至其在册的其他班级"
    default_cid, _ = get_or_create_default_class()
    add_students_to_class(default_cid, [name])
    existing, _ = get_current_records(default_cid)
    merged, n_new, n_upd = merge_records(existing, student_records)
    save_version(default_cid, merged, "删除学生迁入默认名单", n_new, n_upd)
    return True, "该生课时数据已转入「默认名单」留存，再次加入班级时会自动随迁"


# ---------------- 版本快照 ----------------

def get_current_version_number(cid):
    """该班级当前最新版本号；无版本返回 0。"""
    conn = get_conn()
    r = conn.execute(
        "SELECT MAX(version_number) AS m FROM versions WHERE class_id=?", (cid,)
    ).fetchone()
    conn.close()
    return r["m"] or 0  # MAX 在无行时返回 NULL → None，用 or 0 兜底


def get_current_records(cid):
    """返回当前版本的 (记录列表, 版本信息)；无版本则 ([], None)。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM versions WHERE class_id=? ORDER BY version_number DESC LIMIT 1",
        (cid,),
    ).fetchone()
    conn.close()
    if not row:
        return [], None
    return json.loads(row["data_json"]), dict(row)  # json.loads 把 JSON 字符串还原成列表


def list_versions(cid):
    """该班级全部版本（按版本号倒序）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM versions WHERE class_id=? ORDER BY version_number DESC", (cid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_version(cid, records, source_filename, new_count=0, updated_count=0):
    """以全量快照方式保存一个新版本，返回新版本号。"""
    n = get_current_version_number(cid) + 1
    conn = get_conn()
    conn.execute(
        """INSERT INTO versions
           (class_id, version_number, data_json, source_filename,
            record_count, new_count, updated_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cid,
            n,
            json.dumps(records, ensure_ascii=False),  # ensure_ascii=False：中文直接存
            source_filename,
            len(records),
            new_count,
            updated_count,
            _now(),
        ),
    )
    conn.commit()
    conn.close()
    return n


def rollback(cid):
    """删除当前最新版本，回退到上一版本。返回被删版本号，无可删返回 0。"""
    n = get_current_version_number(cid)
    if n == 0:
        return 0  # 早返回：没有版本可回退
    conn = get_conn()
    conn.execute(
        "DELETE FROM versions WHERE class_id=? AND version_number=?", (cid, n)
    )
    conn.commit()
    conn.close()
    return n


# ---------------- 设置（key-value） ----------------

def get_setting(key, default=None):
    """读取一个设置值，不存在返回 default。"""
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    """写入一个设置值（存在则覆盖）。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_api_key():
    """获取 DeepSeek API key：数据库优先，其次 config.py 配置。"""
    db_key = get_setting("deepseek_api_key")
    if db_key:
        return db_key
    try:
        import config
        return config.DEEPSEEK_API_KEY or ""
    except Exception:
        return ""


def get_model_name():
    """获取模型名称：数据库设置优先，其次 config.py 配置，兜底 deepseek-chat。"""
    db_model = get_setting("deepseek_model")
    if db_model and db_model.strip():
        return db_model.strip()
    try:
        import config
        return config.DEEPSEEK_MODEL or "deepseek-chat"
    except Exception:
        return "deepseek-chat"


# ---------------- 学生评价（留存历史） ----------------

def save_evaluations(cid, evaluations, fields_used):
    """追加保存某班级的学生评价（保留历史，不删除旧记录）。

    每次调用产生一个新批次（batch_id 递增），同一批次共享同一个 created_at。
    evaluations: [{student_name, evaluation}, ...]
    fields_used: ["课节数", ...] 使用的依据字段列表
    """
    now = _now()
    fields_json = json.dumps(fields_used, ensure_ascii=False)
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(batch_id) AS m FROM evaluations WHERE class_id=?", (cid,)
    ).fetchone()
    batch_id = (row["m"] or 0) + 1
    for ev in evaluations:
        conn.execute(
            """INSERT INTO evaluations (class_id, batch_id, student_name, evaluation, fields_used, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cid, batch_id, ev["student_name"], ev["evaluation"], fields_json, now),
        )
    conn.commit()
    conn.close()


def get_evaluations(cid):
    """返回最新一批评价（按学生姓名排序）。无评价返回 []。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(batch_id) AS m FROM evaluations WHERE class_id=?", (cid,)
    ).fetchone()
    if not row or not row["m"]:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT * FROM evaluations WHERE class_id=? AND batch_id=? ORDER BY student_name",
        (cid, row["m"]),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_previous_evals_by_student(cid):
    """返回 {学生姓名: [历史评价文本, ...]}，用于 LLM prompt 参考。

    只取最新批次之前的评价（不含当前最新批次），按 batch_id 正序排列。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(batch_id) AS m FROM evaluations WHERE class_id=?", (cid,)
    ).fetchone()
    if not row or not row["m"]:
        conn.close()
        return {}
    rows = conn.execute(
        "SELECT student_name, evaluation FROM evaluations "
        "WHERE class_id=? AND batch_id < ? ORDER BY batch_id",
        (cid, row["m"]),
    ).fetchall()
    conn.close()
    m = {}
    for r in rows:
        m.setdefault(r["student_name"], []).append(r["evaluation"])
    return m


def get_history_evaluations(cid):
    """返回全部评价，按批次(batch_id)降序分组。

    返回 [{batch_time, fields_used, items: [{student_name, evaluation}, ...]}]
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM evaluations WHERE class_id=? ORDER BY batch_id DESC, student_name",
        (cid,),
    ).fetchall()
    conn.close()
    batches = []
    batch_map = {}
    for r in rows:
        bid = r["batch_id"]
        if bid not in batch_map:
            batch = {"batch_time": r["created_at"], "fields_used": r["fields_used"], "items": []}
            batch_map[bid] = batch
            batches.append(batch)
        batch_map[bid]["items"].append(
            {"student_name": r["student_name"], "evaluation": r["evaluation"]}
        )
    return batches


# ---------------- 课堂成就明细（逐题）与错题讲评模板 ----------------

def save_achievement(data):
    """保存一次课堂成就明细导入（整体替换该课节数据）。

    - achievement_results：按课节删除旧记录后整批插入（重复导入 = 覆盖）。
    - question_templates：upsert 题目元数据，但保留已配置的 reply_template 不覆盖。
    返回 (学生数, 题目数)。
    """
    now = _now()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM achievement_results WHERE lesson_name=?", (data["lesson_name"],))
        for st in data["students"]:
            for qkey, result in st["results"].items():
                conn.execute(
                    """INSERT OR REPLACE INTO achievement_results
                       (lesson_name, lesson_time, student_name, question_key, result,
                        correct_count, score_rate, trophies, source_class, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data["lesson_name"], data["lesson_time"], st["学生姓名"], qkey, result,
                        st["正确数"], st["得分率"], st["奖杯数"], data.get("source_class", ""), now,
                    ),
                )
        for q in data["questions"]:
            conn.execute(
                """INSERT INTO question_templates
                   (lesson_name, question_key, qtype, question_text, options,
                    correct_option, knowledge_point, wrong_students, reply_template, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, '',
                           ?)
                   ON CONFLICT(lesson_name, question_key) DO UPDATE SET
                     qtype=excluded.qtype,
                     question_text=excluded.question_text,
                     options=excluded.options,
                     correct_option=excluded.correct_option,
                     knowledge_point=excluded.knowledge_point,
                     wrong_students=excluded.wrong_students,
                     updated_at=excluded.updated_at""",
                (
                    data["lesson_name"], q["question_key"], q["qtype"], q["stem"], q["options"],
                    q["correct_option"], q["knowledge_point"],
                    json.dumps(q["wrong_students"], ensure_ascii=False), now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return len(data["students"]), len(data["questions"])


def list_achievement_lessons():
    """已导入的课节列表：[{lesson_name, lesson_time, student_count, question_count}]。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT lesson_name, MAX(lesson_time) AS lesson_time,
                  COUNT(DISTINCT student_name) AS student_count,
                  COUNT(DISTINCT question_key) AS question_count
           FROM achievement_results GROUP BY lesson_name ORDER BY lesson_time DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_achievement_questions(class_id=None):
    """课节的题目与讲评模板（评价页编辑用），按课节时间倒序分组。

    传 class_id 时只保留该班名单内学生的答错记录（模板本身仍全局共享）；
    过滤后无本班学生且未配模板的题目不再展示。
    返回 [{lesson_name, lesson_time, questions: [...]}]，question 含
    reply_template 与 wrong_students（已解析成 [{name, choice}]）。

    课后作业OJ / 课后拓展OJ 的未交提醒模板与错题讲评模板同表存储
    （question_templates，question_key = 课后作业OJ / 课后拓展OJ），
    在每个课节下作为两个独立条目返回，附该班未交学生名单（oj_results 中
    通过数 < 总数）。「未交」与「未完成」同义：OJ 只有完成与未完成两种状态。
    """
    from parser import OJ_TOTAL

    conn = get_conn()
    roster = None
    if class_id:
        rrows = conn.execute(
            "SELECT name FROM students WHERE class_id=?", (class_id,)
        ).fetchall()
        roster = {r["name"] for r in rrows}
    rows = conn.execute(
        """SELECT t.*, t.question_text AS stem, MAX(a.lesson_time) AS lesson_time
           FROM question_templates t
           LEFT JOIN achievement_results a
             ON a.lesson_name = t.lesson_name AND a.question_key = t.question_key
           WHERE t.question_key NOT IN ('课后作业OJ', '课后拓展OJ')
           GROUP BY t.id
           ORDER BY lesson_time DESC, t.question_key"""
    ).fetchall()
    trows = conn.execute(
        "SELECT lesson_name, question_key, reply_template FROM question_templates "
        "WHERE question_key IN ('课后作业OJ', '课后拓展OJ')"
    ).fetchall()
    ojrows = conn.execute(
        "SELECT student_name, lesson_name, hw_passed, ext_passed FROM oj_results"
    ).fetchall()
    conn.close()

    oj_tpl = {}  # 课节 → {question_key: 未交提醒模板}
    for t in trows:
        oj_tpl.setdefault(t["lesson_name"], {})[t["question_key"]] = t["reply_template"] or ""

    # 各课节的 OJ 未交名单（仅统计本班学生）
    oj_missing = {}  # 课节 → {"课后作业OJ": [姓名...], "课后拓展OJ": [姓名...]}
    for r in ojrows:
        if roster is not None and r["student_name"] not in roster:
            continue
        d = oj_missing.setdefault(r["lesson_name"], {"课后作业OJ": [], "课后拓展OJ": []})
        if r["hw_passed"] < OJ_TOTAL:
            d["课后作业OJ"].append(r["student_name"])
        if r["ext_passed"] < OJ_TOTAL:
            d["课后拓展OJ"].append(r["student_name"])

    def _oj_question(lesson_name, qkey):
        """单个 OJ 未交提醒条目；本班无未交且未配模板时返回 None（减少干扰）。"""
        miss = oj_missing.get(lesson_name, {}).get(qkey, [])
        tpl = oj_tpl.get(lesson_name, {}).get(qkey, "")
        if not miss and not tpl.strip():
            return None
        return {
            "question_key": qkey,
            "stem": "OJ 未交提醒模板（未交学生的评价中使用）",
            "correct_option": "",
            "knowledge_point": "",
            "wrong_students": [{"name": n, "choice": ""} for n in miss],
            "reply_template": tpl,
            "is_oj": True,
        }

    lessons, lesson_map = [], {}

    def _lesson(name, lesson_time=""):
        lesson = lesson_map.get(name)
        if not lesson:
            lesson = {"lesson_name": name, "lesson_time": lesson_time, "questions": []}
            lesson_map[name] = lesson
            lessons.append(lesson)
        return lesson

    for r in rows:
        q = dict(r)
        wrong = json.loads(q["wrong_students"] or "[]")
        if roster is not None:
            wrong = [w for w in wrong if w.get("name") in roster]
            if not wrong and not (q["reply_template"] or "").strip():
                continue  # 与本班无关且未配模板的题目不展示
        q["wrong_students"] = wrong
        _lesson(q["lesson_name"], q["lesson_time"])["questions"].append(q)

    # OJ 条目挂在对应课节组末尾；只有 OJ 数据/模板、没有错题的课节单独成组
    for name in sorted(set(oj_missing) | set(oj_tpl)):
        lesson = lesson_map.get(name)
        for qkey in ("课后作业OJ", "课后拓展OJ"):
            q = _oj_question(name, qkey)
            if q:
                if lesson is None:
                    lesson = _lesson(name)
                lesson["questions"].append(q)
    return lessons


def save_question_template(lesson_name, question_key, template):
    """保存某课节某题的错题讲评模板。

    该课节该题尚无记录时（如课后作业OJ / 课后拓展OJ 的未交提醒模板，OJ 数据
    不走课堂成就导入、question_templates 里原本没有行）创建占位行，保证模板可保存。
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE question_templates SET reply_template=?, updated_at=? "
        "WHERE lesson_name=? AND question_key=?",
        (template, _now(), lesson_name, question_key),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO question_templates (lesson_name, question_key, reply_template, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (lesson_name, question_key, template, _now()),
        )
    conn.commit()
    conn.close()


def save_oj_results(rows):
    """保存 OJ 通过数（班级数据导出）。rows: parser.parse_oj_export 的返回值。

    同一（学生，课节）重复导入覆盖；返回写入行数。
    """
    conn = get_conn()
    now = _now()
    conn.executemany(
        """
        INSERT INTO oj_results (student_name, lesson_name, hw_passed, ext_passed, imported_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (student_name, lesson_name)
        DO UPDATE SET hw_passed = excluded.hw_passed,
                      ext_passed = excluded.ext_passed,
                      imported_at = excluded.imported_at
        """,
        [(r["姓名"], r["课程名称"], r["hw_passed"], r["ext_passed"], now) for r in rows],
    )
    conn.commit()
    n = conn.total_changes
    conn.close()
    return n


def get_oj_for_students(names):
    """返回 {学生姓名: {lesson_name, hw_passed, ext_passed, hw_total, ext_total}}。

    同一学生多条课节记录时取导入时间最新的一条；总数固定为 parser.OJ_TOTAL。
    """
    from parser import OJ_TOTAL

    if not names:
        return {}
    conn = get_conn()
    marks = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""
        SELECT student_name, lesson_name, hw_passed, ext_passed, imported_at
        FROM oj_results
        WHERE student_name IN ({marks})
        ORDER BY imported_at ASC
        """,
        list(names),
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:  # ASC 遍历，后写覆盖先写 → 留下最新导入
        out[r["student_name"]] = {
            "lesson_name": r["lesson_name"],
            "hw_passed": r["hw_passed"],
            "ext_passed": r["ext_passed"],
            "hw_total": OJ_TOTAL,
            "ext_total": OJ_TOTAL,
        }
    return out


def get_oj_map_for_rows(names):
    """返回 {(学生姓名, 课节名称): {hw_passed, hw_total, ext_passed, ext_total}}。

    明细记录按课节展示用：与 get_oj_for_students 不同，这里不取「最新一条」，
    而是保留每位学生每个课节的 OJ 通过数（总数据源 oj_results，总数写死 OJ_TOTAL）。
    """
    from parser import OJ_TOTAL

    if not names:
        return {}
    conn = get_conn()
    marks = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT student_name, lesson_name, hw_passed, ext_passed FROM oj_results "
        f"WHERE student_name IN ({marks})",
        list(names),
    ).fetchall()
    conn.close()
    return {
        (r["student_name"], r["lesson_name"]): {
            "hw_passed": r["hw_passed"], "hw_total": OJ_TOTAL,
            "ext_passed": r["ext_passed"], "ext_total": OJ_TOTAL,
        }
        for r in rows
    }


def get_quiz_map_for_rows(names):
    """返回 {(学生姓名, 课节名称): {"correct": 对题数, "total": 总题数}}（仅课堂小测）。

    明细记录展示用：按课堂小测题号前缀汇总该生该课节的对错行。
    """
    if not names:
        return {}
    conn = get_conn()
    marks = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""SELECT student_name, lesson_name,
                   SUM(CASE WHEN result = '对' THEN 1 ELSE 0 END) AS correct,
                   COUNT(*) AS total
            FROM achievement_results
            WHERE student_name IN ({marks}) AND question_key LIKE '课堂小测%'
            GROUP BY student_name, lesson_name""",
        list(names),
    ).fetchall()
    conn.close()
    return {
        (r["student_name"], r["lesson_name"]): {"correct": r["correct"], "total": r["total"]}
        for r in rows
    }


def get_achievement_for_students(names, lesson_name=None):
    """查询一组学生某次课测的逐题情况（评价生成用）。

    lesson_name 为 None 时取每位学生最近一次课节（按 lesson_time）；
    指定 lesson_name 时只用该课节的数据。

    返回 {学生姓名: {lesson_name, lesson_time, score_rate, correct_count,
                     wrongs: [{question_key, stem, correct_option, knowledge_point,
                               choice, reply_template}]}}
    wrongs 只含答错题，choice 来自错题清单中该生的选择（无记录为空串），
    reply_template 为用户配置的讲评模板（可为空）。
    """
    if not names:
        return {}
    conn = get_conn()
    marks = ",".join("?" for _ in names)
    where = f"a.student_name IN ({marks})"
    params = list(names)
    if lesson_name:
        where += " AND a.lesson_name = ?"
        params.append(lesson_name)
    rows = conn.execute(
        f"""SELECT a.student_name, a.lesson_name, a.lesson_time, a.result,
                   a.score_rate, a.correct_count, a.question_key,
                   t.question_text AS stem, t.correct_option, t.knowledge_point,
                   t.wrong_students, t.reply_template
            FROM achievement_results a
            LEFT JOIN question_templates t
              ON t.lesson_name = a.lesson_name AND t.question_key = a.question_key
            WHERE {where}
            ORDER BY a.lesson_time DESC, a.question_key""",
        tuple(params),
    ).fetchall()

    result = {}
    for r in rows:
        st = result.setdefault(
            r["student_name"],
            {"lesson_name": r["lesson_name"], "lesson_time": r["lesson_time"],
             "score_rate": r["score_rate"] or 0, "correct_count": r["correct_count"] or 0,
             "wrongs": [], "_answered": set()},
        )
        # 行已按 lesson_time 倒序：首次见到该学生即其最近课节，其后不同课节的行直接跳过
        if r["lesson_name"] != st["lesson_name"]:
            continue
        st["_answered"].add(r["question_key"])
        if r["result"] != "错":
            continue
        choice = ""
        for w in json.loads(r["wrong_students"] or "[]"):
            if w.get("name") == r["student_name"]:
                choice = w.get("choice", "")
                break
        st["wrongs"].append(
            {
                "question_key": r["question_key"],
                "stem": r["stem"] or "",
                "correct_option": r["correct_option"] or "",
                "knowledge_point": r["knowledge_point"] or "",
                "choice": choice,
                "reply_template": r["reply_template"] or "",
            }
        )

    # 未完成类别检测：该课节的题目（question_templates）中该生没有作答行的类别。
    # 未作答的题目在 achievement_results 里没有行（解析时空白单元格不入库）。
    if result:
        lessons = {st["lesson_name"] for st in result.values()}
        marks = ",".join("?" for _ in lessons)
        qrows = conn.execute(
            f"SELECT lesson_name, question_key, qtype FROM question_templates WHERE lesson_name IN ({marks}) "
            "AND question_key NOT IN ('课后作业OJ', '课后拓展OJ')",  # OJ 模板占位行不是真实题目
            list(lessons),
        ).fetchall()
        lesson_qs = {}
        for q in qrows:
            lesson_qs.setdefault(q["lesson_name"], {})[q["question_key"]] = _clean_qtype(q["qtype"], q["question_key"])
        for st in result.values():
            expected = lesson_qs.get(st["lesson_name"], {})
            missing = []
            for qkey, cat in expected.items():
                if qkey in st["_answered"] or cat in missing:
                    continue
                missing.append(cat)
            # 课堂小测放最后提醒，作业类缺失在前
            missing.sort(key=lambda c: c.startswith("课堂小测"))
            st["missing"] = missing
        for st in result.values():
            st.pop("_answered", None)
    # 合并 OJ 通过数（班级数据导出），评价生成时作为课后作业/拓展的真实参数。
    # 放在 if result 之外：全班都没有课测行、只有 OJ 数据时同样要建桩条目，
    # 保证 {课后作业OJ}/{课后拓展OJ} 参数与未交提醒模板可用。
    oj_all = get_oj_for_students(list(names))
    for name, oj in oj_all.items():
        st = result.get(name)
        if st is None:
            # 只有 OJ 数据、没有逐题作答记录的学生：生成桩条目，保证 OJ 参数可见
            result[name] = {
                "lesson_name": oj.get("lesson_name", ""), "lesson_time": "",
                "score_rate": 0, "correct_count": 0, "wrongs": [],
                "missing": [], "oj": oj,
            }
            continue
        st["oj"] = oj
        # OJ 数据已覆盖课后作业/课后拓展：通过数=总数代表全部完成且正确，
        # 逐题明细里没有对应行（无错题记录）属正常，不得按「未作答」误报提醒；
        # 未全对时的提醒由 OJ 通过数行（📘）负责，避免重复
        st["missing"] = [
            c for c in st.get("missing", [])
            if "作业" not in c and "拓展" not in c
        ]
    # OJ 未交提醒模板（与错题讲评模板同表存储，question_key = 课后作业OJ / 课后拓展OJ）
    oj_lessons = {st["lesson_name"] for st in result.values() if st.get("lesson_name")}
    if oj_lessons:
        marks = ",".join("?" for _ in oj_lessons)
        trows = conn.execute(
            f"""SELECT lesson_name, question_key, reply_template FROM question_templates
                WHERE lesson_name IN ({marks})
                  AND question_key IN ('课后作业OJ', '课后拓展OJ')""",
            list(oj_lessons),
        ).fetchall()
        for st in result.values():
            for t in trows:
                if st.get("lesson_name") != t["lesson_name"]:
                    continue
                if t["question_key"] == "课后作业OJ":
                    st["oj_tpl_hw"] = t["reply_template"] or ""
                else:
                    st["oj_tpl_ext"] = t["reply_template"] or ""
    conn.close()
    return result


def _clean_qtype(qtype, question_key):
    """题目类别显示名：优先从题号前缀推断（课堂小测1→课堂小测），qtype 列兜底。

    实际数据的 qtype 是「课堂答题/OJ编程」这类作答方式，与模板占位符
    （{课后小测}/{课后OJ}）的类别语义不一致，故题号前缀优先。
    """
    m = re.match(r"^(\D+)", str(question_key or ""))
    if m and m.group(1).strip():
        return m.group(1).strip()
    s = str(qtype or "").strip()
    return s if s and s.lower() not in ("nan", "none") else "题目"
