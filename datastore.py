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


def init_db():
    """初始化表结构（已存在则跳过，幂等）。

    students 表通过 FOREIGN KEY ... ON DELETE CASCADE 绑定 classes，
    删除班级时其名单与版本会自动级联删除。
    """
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
    # 兼容已有数据库：若 evaluations 表缺少 batch_id 列则添加（已存在则忽略）
    try:
        conn.execute("ALTER TABLE evaluations ADD COLUMN batch_id INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


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
    返回 [{name, student_count, created}]。
    """
    now = _now()
    result = []
    conn = get_conn()
    try:
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
    return result


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


def get_achievement_questions():
    """全部课节的题目与讲评模板（评价页编辑用），按课节时间倒序分组。

    返回 [{lesson_name, lesson_time, questions: [...]}]，question 含
    reply_template 与 wrong_students（已解析成 [{name, choice}]）。
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.*, t.question_text AS stem, MAX(a.lesson_time) AS lesson_time
           FROM question_templates t
           LEFT JOIN achievement_results a
             ON a.lesson_name = t.lesson_name AND a.question_key = t.question_key
           GROUP BY t.id
           ORDER BY lesson_time DESC, t.question_key"""
    ).fetchall()
    conn.close()
    lessons, lesson_map = [], {}
    for r in rows:
        q = dict(r)
        q["wrong_students"] = json.loads(q["wrong_students"] or "[]")
        lesson = lesson_map.get(q["lesson_name"])
        if not lesson:
            lesson = {"lesson_name": q["lesson_name"], "lesson_time": q["lesson_time"], "questions": []}
            lesson_map[q["lesson_name"]] = lesson
            lessons.append(lesson)
        lesson["questions"].append(q)
    return lessons


def save_question_template(lesson_name, question_key, template):
    """保存某课节某题的错题讲评模板（不存在该题记录则忽略）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE question_templates SET reply_template=?, updated_at=? "
        "WHERE lesson_name=? AND question_key=?",
        (template, _now(), lesson_name, question_key),
    )
    conn.commit()
    conn.close()


def get_achievement_for_students(names):
    """查询一组学生最新一次课测的逐题情况（评价生成用）。

    返回 {学生姓名: {lesson_name, lesson_time, score_rate, correct_count,
                     wrongs: [{question_key, stem, correct_option, knowledge_point,
                               choice, reply_template}]}}
    只取该生最近课节（按 lesson_time）；wrongs 只含答错题，choice 来自错题清单
    中该生的选择（无记录为空串），reply_template 为用户配置的讲评模板（可为空）。
    """
    if not names:
        return {}
    conn = get_conn()
    marks = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""SELECT a.student_name, a.lesson_name, a.lesson_time, a.result,
                   a.score_rate, a.correct_count, a.question_key,
                   t.question_text AS stem, t.correct_option, t.knowledge_point,
                   t.wrong_students, t.reply_template
            FROM achievement_results a
            LEFT JOIN question_templates t
              ON t.lesson_name = a.lesson_name AND t.question_key = a.question_key
            WHERE a.student_name IN ({marks})
            ORDER BY a.lesson_time DESC, a.question_key""",
        tuple(names),
    ).fetchall()
    conn.close()

    result = {}
    for r in rows:
        st = result.setdefault(
            r["student_name"],
            {"lesson_name": r["lesson_name"], "lesson_time": r["lesson_time"],
             "score_rate": r["score_rate"] or 0, "correct_count": r["correct_count"] or 0,
             "wrongs": []},
        )
        # 行已按 lesson_time 倒序：首次见到该学生即其最近课节，其后不同课节的行直接跳过
        if r["lesson_name"] != st["lesson_name"]:
            continue
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
    return result
