# -*- coding: utf-8 -*-
"""解析与整合层：文档解析、按学生键覆盖合并、按学生汇总、导出 Excel。

字段约定（课堂成就明细）：
    课节名称 / 开课时间 / 学生姓名 / 正确数 / 得分率 / 奖杯数 / 出勤总奖杯数

去重键：(课节名称, 开课时间, 学生姓名)。再次上传相同键的记录时，新值覆盖旧值。
"""
# io 提供 BytesIO：把字节流当成“文件”来读写，pandas 可以直接从内存读取，无需落盘。
import io
import re  # 用于从课节名里提取 P1/P2 等短名字
from collections import defaultdict  # defaultdict：访问不存在的键时自动创建默认值
from datetime import datetime

import pandas as pd  # 第三方库 pandas：表格数据处理

# 模块级常量（列表）。全大写命名表示常量，供本模块各函数共享。
# EXPECTED_COLS 是完整字段（导出/明细展示用）；REQUIRED_COLS 是解析必须存在的列，
# 其余如「出勤总奖杯数」有的文档没有，缺失时自动补 0（否则很多真实明细会解析报错）。
EXPECTED_COLS = ["课节名称", "开课时间", "学生姓名", "正确数", "得分率", "奖杯数", "出勤总奖杯数"]
REQUIRED_COLS = ["课节名称", "开课时间", "学生姓名", "正确数", "得分率", "奖杯数"]
OPTIONAL_COLS = ["出勤总奖杯数"]  # 缺失时按 0 处理
INT_COLS = ["正确数", "奖杯数", "出勤总奖杯数"]
FLOAT_COLS = ["得分率"]

# 进步曲线阈值：低/高分与「进步」判定。
LOW_ACC = 0.5      # 得分率 < 50% 视为低（弱项课节）
HIGH_ACC = 0.8     # 得分率 >= 80% 视为高（强项课节）
IMPROVE_DELTA = 0.15  # 末段均分 - 首段均分 >= +15% → 判定为「进步」

# 列名归一化别名：把实际文件中的各种写法映射到标准列名（去空白后精确匹配）
# 值用集合 set（无序、去重、in 查找快 O(1)）。
COLUMN_ALIASES = {
    "课节名称": {"课节名称", "课节", "课程名称", "课程", "节课名称"},
    "开课时间": {"开课时间", "上课时间", "上课日期", "开课日期", "时间"},
    "学生姓名": {"学生姓名", "姓名", "学生", "学生名"},
    "正确数": {"正确数", "正确题数", "答对数", "正确数量"},
    "得分率": {"得分率", "正确率", "得分率/正确率"},
    "奖杯数": {"奖杯数", "奖杯", "获得奖杯数"},
    "出勤总奖杯数": {"出勤总奖杯数", "加出勤总奖杯数", "加出勤奖杯数", "出勤奖杯数", "总奖杯数", "出勤总奖杯"},
}

# 名单文件中学生姓名列的别名（不同工作表可能用「学生姓名」「学员姓名」等）。
ROSTER_NAME_ALIASES = {"学生姓名", "学员姓名", "姓名", "学生", "学生名"}


def normalize_columns(df):
    """把 DataFrame 列名归一化到 EXPECTED_COLS 中的标准名。返回 (df, 缺失列名列表)。

    【Python 基础】
    - 字典推导式 {k: v for ...}：一行构建字典。
    - dict.items() 同时遍历键和值。
    - [c for c in EXPECTED_COLS if c not in df.columns]：列表推导式 + 过滤。
    """
    renamed = {}
    # 字典推导式：把每列去空白后的名字映射到自身，方便后续按名查找原始列名。
    raw = {str(c).strip(): str(c).strip() for c in df.columns}
    for canonical, aliases in COLUMN_ALIASES.items():  # 同时取“标准名”和“别名集合”
        for alias in aliases:
            if alias in raw and canonical not in renamed:  # 找到匹配且未重复映射
                renamed[raw[alias]] = canonical
    if renamed:
        df = df.rename(columns=renamed)  # pandas 按字典批量重命名列
    missing = [c for c in REQUIRED_COLS if c not in df.columns]  # 还缺哪些必要列（可选列不强制）
    return df, missing


# ---------------- 工具 ----------------

def normalize_time(val):
    """把各种时间表示统一成 'YYYY-MM-DD HH:MM:SS' 字符串。

    【Python 基础】
    - isinstance(val, (pd.Timestamp, datetime))：判断类型，第二个参数可以是元组表示“多种之一”。
    - str.lower()：转小写；in (...) 判断是否在元组里。
    - try/except ValueError: continue：尝试多种格式，失败就跳过下一种。
    - strptime 把字符串按格式解析成 datetime，strftime 再格式化输出。
    """
    if val is None:
        return ""
    if isinstance(val, float) and pd.isna(val):  # 空单元格在 pandas 里常是 NaN（float）
        return ""
    if isinstance(val, (pd.Timestamp, datetime)):  # 已经是时间类型
        return val.strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip()
    if not s or s.lower() in ("nan", "nat", "none"):  # 各种“空”的字符串形态
        return ""
    # 逐个尝试常见格式，匹配上就转换
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
        "%Y年%m月%d日 %H:%M", "%Y年%m月%d日",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue  # 该格式不匹配，试下一个
    return s  # 都不匹配，原样返回


def safe_num(val):
    """把任意值安全转成 float，失败/空值返回 0。

    【Python 基础】
    - try/except 同时捕获两种异常：(ValueError, TypeError) 用元组写出。
    - 捕获异常后返回兜底值 0，避免一处脏数据导致整张表解析崩溃。
    """
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return 0
        return float(val)
    except (ValueError, TypeError):
        return 0


def parse_accuracy(val):
    """把「得分率/正确率」的多种写法统一成 [0,1] 的 float。

    真实文档里这个字段很乱：有的是小数（0.60、1.0）、
    有的是带百分号的字符串（"86%"、"100%"）、有的是纯数字 100 表示 100%。
    - '86%' → 0.86        (去 % 后 float，>1 则除以 100)
    - 0.6 / 0.60 → 0.6    (已是小数，直接返回)
    - '100' / 1.0 → 1.0   (纯 100 视作 100%，>1 则除以 100；1.0 保持不变)
    - 空/脏值 → 0.0       (兜底，避免一处脏数据崩整张表)
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    s = str(val).strip().rstrip("%")  # 去空白与末尾百分号
    if not s or s.lower() in ("nan", "nat", "none", "-"):
        return 0.0
    try:
        f = float(s)
    except ValueError:
        return 0.0
    if f > 1.0:    # 大于 1 视为百分数值（86 → 0.86、100 → 1.0）
        f = f / 100.0
    return min(max(f, 0.0), 1.0)  # 钳到 [0,1]


def record_key(r):
    """返回一条记录的去重键（三元组）。

    【Python 基础】元组 tuple 是“可哈希”的，可以作为字典的键；
    而列表 list 不可哈希，不能当键。这是去重用元组的原因。
    """
    return (r["课节名称"], r["开课时间"], r["学生姓名"])


# ---------------- 名单解析 ----------------

def parse_roster(filebytes):
    """解析名单文件：每个工作表 → 一个班级名单。

    返回 [{name: 工作表名, students: [姓名], skipped: 是否跳过}]。
    工作表找不到姓名列时 skipped=True、students 为空。

    【Python 基础】
    - pd.read_excel(..., sheet_name=None)：一次读全部工作表，返回 {表名: DataFrame} 字典。
    - .items() 同时遍历字典的键（表名）和值（DataFrame）。
    - 用集合 seen 去重并保持首次出现顺序。
    """
    sheets = pd.read_excel(io.BytesIO(filebytes), sheet_name=None)  # {表名: df}
    result = []
    for sheet_name, df in sheets.items():
        # 在列里找姓名列（按别名匹配）
        name_col = None
        for c in df.columns:
            if str(c).strip() in ROSTER_NAME_ALIASES:
                name_col = c
                break
        if name_col is None:
            result.append({"name": sheet_name, "students": [], "skipped": True})
            continue
        seen = set()
        students = []
        for v in df[name_col]:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            s = str(v).strip()
            if s and s.lower() != "nan" and s not in seen:  # 去重保序
                seen.add(s)
                students.append(s)
        result.append({"name": sheet_name, "students": students, "skipped": False})
    return result


# ---------------- 解析 ----------------

def _read_csv_flexible(filebytes):
    """按多种编码尝试读取 CSV，返回 DataFrame。

    有的 CSV 是 utf-8（含 BOM），有的是 GBK（如 P8 递推算法）。先试
    utf-8-sig（能吃掉 BOM），再试 gb18030（GBK 超集），都失败才上抛。
    """
    last = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(io.BytesIO(filebytes), encoding=enc)
        except UnicodeDecodeError as e:
            last = e
    raise last


def parse_upload(filename, filebytes):
    """解析单个上传文件，返回标准化记录列表。失败抛 ValueError。

    【Python 基础】
    - 三元表达式 A if cond else B：这里根据扩展名选解析方式。
    - rsplit('.', 1)[-1]：从右按 '.' 切一次，取最后一段作为扩展名；[-1] 取列表最后一项。
    - f"...{ext}..."：f-string 格式化字符串。
    - except ValueError: raise —— 捕获后原样抛出（不吞掉自己抛的业务异常）。
    - for _, row in df.iterrows()：遍历每行；下划线 _ 表示“用不到的变量”（行索引）。
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        if ext in ("xlsx", "xls"):
            df = pd.read_excel(io.BytesIO(filebytes))  # 从字节流读 Excel
        elif ext == "csv":
            df = _read_csv_flexible(filebytes)  # CSV 编码不一，逐个尝试
        else:
            raise ValueError(f"不支持的文件格式: .{ext}（仅支持 .xlsx/.xls/.csv）")
    except ValueError:
        raise  # 自己抛的 ValueError 原样上抛，交给上层提示用户
    except Exception as e:
        raise ValueError(f"读取文件失败: {e}")  # 其它异常统一包成 ValueError

    # 列名归一化（兼容「加出勤总奖杯数」等变体）
    df, missing = normalize_columns(df)
    if missing:
        raise ValueError(f"缺少必要列: {', '.join(missing)}；需要: {', '.join(EXPECTED_COLS)}")

    # 过滤掉学生姓名为空的行：用布尔条件给 DataFrame 做行筛选。
    df = df[df["学生姓名"].notna() & (df["学生姓名"].astype(str).str.strip() != "")]

    records = []
    for _, row in df.iterrows():  # _ 是行索引（用不到），row 是该行数据
        records.append(
            {
                "课节名称": str(row["课节名称"]).strip(),
                "开课时间": normalize_time(row["开课时间"]),
                "学生姓名": str(row["学生姓名"]).strip(),
                "正确数": int(safe_num(row["正确数"])),       # 计数类转 int
                "得分率": parse_accuracy(row["得分率"]),        # 兼容小数/百分比混合写法
                "奖杯数": int(safe_num(row["奖杯数"])),
                # 可选列：有的文档没有「出勤总奖杯数」，缺失按 0 处理
                "出勤总奖杯数": int(safe_num(row.get("出勤总奖杯数", 0))),
            }
        )
    return records


# ---------------- 课堂成就明细（逐题）解析 ----------------

# 逐题明细的基础列（题目列 = 除这些之外的全部列，如 课堂小测1、课后作业1）
ACHIEVEMENT_BASE_COLS = set(EXPECTED_COLS) | {"班级"}

# 对错取值归一化：不同导出端的写法映射到「对 / 错」
RIGHT_MARKS = {"对", "正确", "√", "✓", "right", "true", "yes", "y"}
WRONG_MARKS = {"错", "错误", "×", "x", "wrong", "false", "no", "n"}


def _norm_result(val):
    """把逐题明细里的对错写法统一成「对 / 错」，无法识别返回空串。"""
    s = str(val).strip().lower()
    if not s or s in ("nan", "none", "-"):
        return ""
    if s in RIGHT_MARKS:
        return "对"
    if s in WRONG_MARKS:
        return "错"
    return ""


def _clean_text(val):
    """单元格文本清洗：去空白，空值/NaN 返回空串。"""
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan", "none", "nat") else s


def parse_wrong_students(text):
    """把「错题学生明细」解析成 [{name, choice}]。

    形如 "顾龚昊(选B); 金浩祺(选A)"（分号中英文皆可、括号全半角皆可）。
    没有选择信息的裸姓名也能解析（choice 为空串）。
    """
    students = []
    for part in re.split(r"[;；\n]", str(text or "")):
        part = part.strip()
        if not part or part.lower() == "nan":
            continue
        m = re.match(r"([^（(]+?)[（(]\s*选?\s*([A-Za-z])\s*[)）]", part)
        if m:
            students.append({"name": m.group(1).strip(), "choice": m.group(2).upper()})
        else:
            students.append({"name": part, "choice": ""})
    return students


def parse_achievement(filebytes):
    """解析课堂成就明细 Excel（含「逐题明细」「错题清单」两个工作表）。

    逐题明细：每个学生一行，基础列（课节名称/开课时间/学生姓名/正确数/得分率/
              奖杯数/出勤总奖杯数/班级）+ 动态题目列（列名即题号，值为 对/错）。
    错题清单：每题一行（题目/类型/题干/选项/正确选项/知识点/错题学生明细）。

    返回:
      {
        "lesson_name": 课节名称, "lesson_time": 开课时间, "source_class": 班级,
        "students": [{学生姓名, 正确数, 得分率, 奖杯数, 出勤总奖杯数, results: {题号: 对/错}}],
        "questions": [{question_key, qtype, stem, options, correct_option,
                       knowledge_point, wrong_students: [{name, choice}]}],
      }
    一个文件只允许包含一个课节（真实导出即如此），多个课节抛 ValueError。
    """
    sheets = pd.read_excel(io.BytesIO(filebytes), sheet_name=None)
    detail = wrong = None
    for df in sheets.values():
        cols = {str(c).strip() for c in df.columns}
        if {"课节名称", "学生姓名"} <= cols:
            detail = df
        elif {"题干", "题目"} <= cols:
            wrong = df
    if detail is None:
        raise ValueError("未找到「逐题明细」工作表（需含 课节名称 / 学生姓名 列）")

    detail, _ = normalize_columns(detail)
    df = detail[detail["学生姓名"].notna() & (detail["学生姓名"].astype(str).str.strip() != "")]
    if df.empty:
        raise ValueError("逐题明细中没有学生数据")

    lesson_name = str(df.iloc[0]["课节名称"]).strip()
    lesson_time = normalize_time(df.iloc[0]["开课时间"])
    lessons = {str(v).strip() for v in df["课节名称"].dropna()}
    if len(lessons) > 1:
        raise ValueError("该文件包含多个课节，请按课节逐个文件导入：" + "、".join(sorted(lessons)))

    # 题目列 = 基础列之外的动态列（课堂小测N / 课后作业N ...）
    q_cols = [c for c in df.columns if str(c).strip() not in ACHIEVEMENT_BASE_COLS]

    students = []
    for _, row in df.iterrows():
        results = {}
        for c in q_cols:
            r = _norm_result(row[c])
            if r:
                results[str(c).strip()] = r
        students.append(
            {
                "学生姓名": str(row["学生姓名"]).strip(),
                "正确数": int(safe_num(row["正确数"])),
                "得分率": parse_accuracy(row["得分率"]),
                "奖杯数": int(safe_num(row["奖杯数"])),
                "出勤总奖杯数": int(safe_num(row.get("出勤总奖杯数", 0))),
                "results": results,
            }
        )

    source_class = ""
    if "班级" in df.columns:
        vals = [str(v).strip() for v in df["班级"].dropna() if str(v).strip()]
        source_class = vals[0] if vals else ""

    questions = []
    if wrong is not None:
        for _, row in wrong.iterrows():
            questions.append(
                {
                    "question_key": _clean_text(row["题目"]),
                    "qtype": _clean_text(row.get("类型", "")),
                    "stem": _clean_text(row.get("题干", "")),
                    "options": _clean_text(row.get("选项", "")),
                    "correct_option": _clean_text(row.get("正确选项", "")),
                    "knowledge_point": _clean_text(row.get("知识点", "")),
                    "wrong_students": parse_wrong_students(row.get("错题学生明细", "")),
                }
            )
    return {
        "lesson_name": lesson_name,
        "lesson_time": lesson_time,
        "source_class": source_class,
        "students": students,
        "questions": questions,
    }


# ---------------- 合并 ----------------

def merge_records(existing, new_rows):
    """把 new_rows 合并进 existing。相同 key 覆盖旧值。

    返回 (merged_list, new_count, updated_count)。

    【Python 基础】
    - 用字典 by_key 做去重（键唯一），用列表 order 记录插入顺序以保持稳定排序。
    - 多重赋值 n_new = n_updated = 0：两个变量同时赋 0。
    - 返回元组 (列表, int, int)，调用方解包。
    """
    by_key = {}
    order = []
    for r in existing:
        k = record_key(r)
        if k not in by_key:
            order.append(k)
        by_key[k] = r

    n_new = n_updated = 0
    for r in new_rows:
        k = record_key(r)
        if k not in by_key:
            order.append(k)  # 新键，登记顺序
            n_new += 1
        else:
            n_updated += 1
        by_key[k] = r  # 覆盖：新值替换旧值（已存在则更新，不存在则新增）
    return [by_key[k] for k in order], n_new, n_updated  # 按登记顺序输出列表


# ---------------- 汇总 ----------------

def aggregate_for_class(records, roster):
    """按班级名单框架汇总：名单内每个学生一行（无记录则统计为 0）。

    【Python 基础】
    - defaultdict(list)：访问不存在的键会自动创建空列表 []。
    - groups.get(name, [])：取值，键不存在时返回 [] 作为默认（不抛 KeyError）。
    - sum(r["x"] for r in rows)：生成器表达式逐项累加。
    - sort(key=lambda x: (x["有记录"], x["总奖杯数"]), reverse=True)：
      按元组排序——先按“是否有记录”分组，再按总奖杯数降序；有记录的排在前面。
    """
    groups = defaultdict(list)  # {学生姓名: [该学生的所有记录]}
    for r in records:
        groups[r["学生姓名"]].append(r)

    summary = []
    for name in roster:  # 以名单为框架：名单里的学生都出一行
        rows = groups.get(name, [])  # 没有课时记录的学生得到空列表
        n = len(rows)
        summary.append(
            {
                "学生姓名": name,
                "课节数": n,
                "总正确数": sum(r["正确数"] for r in rows),
                "平均得分率": (sum(r["得分率"] for r in rows) / n) if n else 0,
                "总奖杯数": sum(r["奖杯数"] for r in rows),
                "总出勤奖杯数": sum(r["出勤总奖杯数"] for r in rows),
                "补课节数": sum(1 for r in rows if r.get("补课")),  # 去别班时段补课的次数
                "有记录": n > 0,
            }
        )
    # 有记录的在前并按总奖杯数降序；无记录的在后
    summary.sort(key=lambda x: (x["有记录"], x["总奖杯数"]), reverse=True)
    return summary


def unique_students(records):
    """返回按姓名排序的学生列表。

    【Python 基础】
    - {r["学生姓名"] for r in records}：集合推导式，自动去重。
    - sorted()：返回新的有序列表（原集合不变）。
    """
    return sorted({r["学生姓名"] for r in records})


# ---------------- 进步分析 ----------------

def _mean(vals):
    """返回列表均值；空列表返回 0。"""
    return sum(vals) / len(vals) if vals else 0.0


def progress_for_class(records, roster):
    """计算某班级的进步曲线数据（以名单为框架）。

    对每位学生：按开课时间排序得到会话序列，
    比较「前半段」与「后半段」的平均正确率得出进步/退步/平稳，并找出最高/最低分课节。

    返回:
      rows: 每学生一行，按「是否有足够课节 → 变化量降序」排序，
            行字段: {学生姓名, n, sessions, first_avg, last_avg, delta, trend,
                    low, high, is_improved}
      class_sessions: 班级各课节（含时间）的平均正确率，按时间排序。
    """
    groups = defaultdict(list)
    for r in records:
        groups[r["学生姓名"]].append(r)

    rows = []
    for name in roster:
        # 只保留画曲线需要的字段，减轻传给前端 JSON 的体积
        sess = sorted(
            [
                {"课节名称": r["课节名称"], "开课时间": r["开课时间"], "得分率": r["得分率"]}
                for r in groups.get(name, [])
            ],
            key=lambda s: s["开课时间"],
        )
        n = len(sess)
        accs = [s["得分率"] for s in sess]
        half = n // 2
        if n >= 2:
            first_avg = _mean(accs[:half])
            last_avg = _mean(accs[half:])
            delta = last_avg - first_avg
        else:
            first_avg = accs[0] if n else 0.0
            last_avg = accs[0] if n else 0.0
            delta = 0.0

        if n < 2:
            trend = "数据不足"
        elif delta >= IMPROVE_DELTA:
            trend = "进步"
        elif delta <= -IMPROVE_DELTA:
            trend = "退步"
        else:
            trend = "平稳"

        low = min(sess, key=lambda s: s["得分率"]) if sess else None
        high = max(sess, key=lambda s: s["得分率"]) if sess else None

        rows.append(
            {
                "学生姓名": name,
                "n": n,
                "sessions": sess,
                "first_avg": round(first_avg, 4),
                "last_avg": round(last_avg, 4),
                "delta": round(delta, 4),
                "trend": trend,
                "low": low,
                "high": high,
                # 显著进步：课节足够、后段显著提高、且起步偏弱
                "is_improved": n >= 2 and trend == "进步" and first_avg < 0.7,
            }
        )

    # 有足够课节的在前，其余在后；同类按变化量降序（进步最多在最前）
    rows.sort(key=lambda x: (x["n"] < 2, -x["delta"]))

    # 班级各课节平均正确率（帮助判断哪堂课整体偏难）
    by_sess = defaultdict(list)
    for r in records:
        by_sess[(r["课节名称"], r["开课时间"])].append(r["得分率"])
    class_sessions = sorted(
        [
            {
                "课节名称": k[0],
                "开课时间": k[1],
                "avg": round(_mean(v), 4),
                "count": len(v),
            }
            for k, v in by_sess.items()
        ],
        key=lambda x: x["开课时间"],
    )
    return {"rows": rows, "class_sessions": class_sessions}


# ---------------- 课节排行 ----------------

def band_of(avg):
    """按平均正确率判定颜色/状态等级（供课节排行用）。"""
    if avg >= HIGH_ACC:
        return "high"
    if avg >= LOW_ACC:
        return "mid"
    return "low"


def rankings_for_class(records):
    """计算「某班级各课节」的正确率排名与奖杯排名。

    以「课节的短名」为一个课程（如 P1、P2、先导课）。把该班学生在这门课的
    所有课时记录（含去别的班时段补课、以及被并入本班但标注为补课的记录）
    合并统计——这样同一门课不会因开课时间不同、或先导课文件后缀不同
    而出现两条重复项。

    返回: {lessons: [{fname, time, key, avg, count, trophies, attend, band,
                      rank_acc, rank_trophy, short, makeup}]}
    fname/short/key 都是短名（课程标识，唯一）；time 取该课程在本班最早的
    开课时间（作展示与排序）；makeup 是这门课里属于补课的记录数。
    """
    by_course = defaultdict(list)
    for r in records:
        by_course[_short_lesson(r["课节名称"])].append(r)

    lessons = []
    for fname, rows in by_course.items():
        avg = round(_mean([x["得分率"] for x in rows]), 4)
        times = [x["开课时间"] for x in rows if x["开课时间"]]
        lessons.append(
            {
                "fname": fname,          # 课程标识（P1/P2/先导课）
                "time": min(times) if times else "",  # 该课程最早一次时间，作展示/排序
                "key": fname,            # 唯一标识：直接用课程短名
                "avg": avg,
                "count": len(rows),      # 该班在本课程记录数（含补课）
                "trophies": sum(x["奖杯数"] for x in rows),
                "attend": sum(x["出勤总奖杯数"] for x in rows),
                "band": band_of(avg),
                "short": fname,
                "makeup": sum(1 for x in rows if x.get("补课")),  # 其中补课记录数
            }
        )

    # 先按最早上课时间排成日程顺序，再据此分配正确率/奖杯排名
    lessons.sort(key=lambda l: l["time"])
    for rank, i in enumerate(sorted(range(len(lessons)), key=lambda i: lessons[i]["avg"], reverse=True), 1):
        lessons[i]["rank_acc"] = rank
    for rank, i in enumerate(sorted(range(len(lessons)), key=lambda i: lessons[i]["trophies"], reverse=True), 1):
        lessons[i]["rank_trophy"] = rank
    return {"lessons": lessons}


def _short_lesson(name):
    """把完整课节名缩写为简短的展示名（与进步曲线里 shortLesson 逻辑一致）。"""
    if not name:
        return ""
    if "先导课" in name:
        return "先导课"
    m = re.match(r"^(P\d+)", name)
    return m.group(1) if m else name[:8]


def cross_class_rankings(class_records):
    """计算「同一课节跨班级」的正确率/奖杯对比。

    class_records: [{class_id, class_name, records, ...}]（一般不含「默认名单」）。
    以「课节短名」（如 P1、先导课）为单位，统计每个班在该课节的均分/人数/奖杯。
    用短名做身份，避免同一门先导课因文件后缀不同被拆成多行。

    返回: {lessons: [{fname, time, classes: {class_id: {avg,count,trophies,attend,band}}}],
           classes: [{id, name}]}
    """
    by_lesson = {}    # {课节短名: {fname, time, classes: {cid: {...}}}}
    class_meta = []
    for cr in class_records:
        class_meta.append({"id": cr["class_id"], "name": cr["class_name"]})
        for r in cr["records"]:
            fname = _short_lesson(r["课节名称"])
            time = r["开课时间"]
            if fname not in by_lesson:
                by_lesson[fname] = {"fname": fname, "time": "", "classes": {}}
            slot = by_lesson[fname]
            if time and (not slot["time"] or time < slot["time"]):
                slot["time"] = time  # 取该课节最早出现的时间作排序依据
            cell = slot["classes"].setdefault(
                str(cr["class_id"]), {"accs": [], "trophies": 0, "attend": 0}
            )
            cell["accs"].append(r["得分率"])
            cell["trophies"] += r["奖杯数"]
            cell["attend"] += r["出勤总奖杯数"]

    lessons = []
    for fname, slot in by_lesson.items():
        cells = {}
        for cid, cell in slot["classes"].items():
            avg = round(_mean(cell["accs"]), 4)
            cells[cid] = {
                "avg": avg,
                "count": len(cell["accs"]),
                "trophies": cell["trophies"],
                "attend": cell["attend"],
                "band": band_of(avg),
            }
        lessons.append({"fname": fname, "time": slot["time"], "classes": cells})
    lessons.sort(key=lambda l: l["time"])
    return {"lessons": lessons, "classes": class_meta}


# ---------------- 补课识别 ----------------

def session_owners(class_records):
    """判定每个 (课节名称, 开课时间) 由哪个班级「拥有」。

    各班级课程内容相同、仅上课时间不同；学生可能为补课去别的班时段上课。
    这里以「哪个班在当前时段记录数最多」（多数派）作为该时段的归属班。
    补课生的记录落在该时段，但因是少数派而不会成为归属班，故可据此标记补课。

    class_records: [{class_id, class_name, records}, ...]（一般不含「默认名单」）。
    返回 {(课节名称, 开课时间): 归属班级 class_id}。
    """
    counts = defaultdict(lambda: defaultdict(int))  # (课节,时间) -> {class_id: 条数}
    for cr in class_records:
        for r in cr["records"]:
            counts[(r["课节名称"], r["开课时间"])][cr["class_id"]] += 1
    # 每个时段取记录数最多的班作为归属班
    return {key: max(v, key=lambda c: v[c]) for key, v in counts.items()}


def annotate_makeup(records, owners, cid):
    """给每条记录补一个「补课」布尔标记。

    该记录的课节归属班 != 本班 cid，说明这名本班学生去了别班的时段补课，
    记为补课；否则（归属班就是本班）为正常上课。无需回写数据库，运行时标注即可。
    """
    for r in records:
        owner = owners.get((r["课节名称"], r["开课时间"]))
        r["补课"] = bool(owner and owner != cid)
    return records


# ---------------- 导出 ----------------

def export_excel(records, summary):
    """生成包含「学生汇总」与「明细记录」两个 Sheet 的 Excel 字节流。

    【Python 基础】
    - with 语句（上下文管理器）：离开 with 块时自动调用 writer.close()，确保写入完成。
    - .map(lambda x: f"{x*100:.1f}%")：对整列应用匿名函数做格式化。
    - out.seek(0)：把内存流的读写指针移回首部，供读取方从头读起。
    """
    out = io.BytesIO()  # 内存中的二进制流，作为“虚拟文件”
    with pd.ExcelWriter(out, engine="openpyxl") as writer:  # with 结束自动保存关闭
        df_sum = pd.DataFrame(
            summary,
            columns=["学生姓名", "课节数", "总正确数", "平均得分率", "总奖杯数", "总出勤奖杯数", "补课节数"],
        )
        df_sum["平均得分率"] = df_sum["平均得分率"].map(lambda x: f"{x * 100:.1f}%")
        df_sum.to_excel(writer, sheet_name="学生汇总", index=False)

        # 明细：补课列可选（无标注时不出现），补课显示「是/否」
        det_cols = EXPECTED_COLS + (["补课"] if any("补课" in r for r in records) else [])
        df_det = pd.DataFrame(records, columns=det_cols)
        df_det["得分率"] = df_det["得分率"].map(lambda x: f"{x * 100:.0f}%")
        if "补课" in det_cols:
            df_det["补课"] = df_det["补课"].map(lambda x: "是" if x else "否")
        df_det.to_excel(writer, sheet_name="明细记录", index=False)
    out.seek(0)  # 指针回到开头，否则 send_file 读到的是空
    return out
