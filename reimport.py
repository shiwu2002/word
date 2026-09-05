# -*- coding: utf-8 -*-
"""清空并重新导入：先备份 data.db，再重建班级名单，最后导入全部课节明细。

背景：之前用旧解析器导入，因字段不一致（如部分文档缺「出勤总奖杯数」列、
得分率是百分比字符串、P8 是 GBK 编码 CSV）导致导入中断，库里落下残缺数据。
本次运行修复后的解析器全新导入，得到一份干净、完整的数据。

用法:
    python reimport.py

可反复运行——每次都会先把当前 data.db 备份成 data.db.backup-<时间戳>，
再做一次从零导入。
"""
import datetime
import glob
import os
import shutil
import sys

# Windows 终端下打印中文（避免乱码）。非 Windows 环境只是多余但无害。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import datastore  # noqa: E402  （下同，先保证 sys.path 就绪再 import）
from datastore import init_db, get_conn, import_roster, list_classes, get_current_records  # noqa: E402
from parser import parse_roster, parse_upload  # noqa: E402
from integration import route_merge_lessons  # noqa: E402

# 数据目录：课堂成就明细（脚本所在目录下）
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "课堂成就明细")


def backup_db():
    """复制当前 data.db 为带时间戳的备份，返回备份路径。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    src = datastore.DB_PATH
    dst = f"{src}.backup-{ts}"
    shutil.copyfile(src, dst)
    print(f"[备份] {src}  ->  {dst}")
    return dst


def wipe_classes():
    """删除所有班级，级联清空其名单/版本/评价。"""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM classes")
        conn.commit()
        print("[清空] 已删除所有班级（其名单/版本/评价随之级联清空）")
    finally:
        conn.close()


def _is_roster_file(path):
    b = os.path.basename(path)
    return b.startswith("0724") or "学生档案" in b


def main():
    init_db()  # 幂等：确保表结构存在（含历史的 batch_id 兼容列）

    backup_db()
    wipe_classes()

    # ---------- 1) 导入名单：每个工作表 → 一个班级 ----------
    roster_files = sorted(glob.glob(os.path.join(DATA_DIR, "*学生档案*.xlsx")))
    if not roster_files:
        print("[错误] 未找到名单文件（*学生档案*.xlsx），终止。")
        return 1
    rf = roster_files[0]
    print(f"[名单] 使用 {os.path.basename(rf)}")
    classes = parse_roster(open(rf, "rb").read())
    valid = [c for c in classes if not c.get("skipped")]
    skipped = [c["name"] for c in classes if c.get("skipped")]
    import_roster(valid)
    for c in valid:
        print(f"    班级「{c['name']}」: {len(c['students'])} 人")
    if skipped:
        print(f"    [跳过] 未找到姓名列的工作表：{'、'.join(skipped)}")

    # ---------- 2) 解析全部课节文件 ----------
    lesson_files = [
        f
        for f in sorted(glob.glob(os.path.join(DATA_DIR, "*")))
        if f.lower().endswith((".xlsx", ".xls", ".csv")) and not _is_roster_file(f)
    ]
    all_records = []
    errors = []
    for f in lesson_files:
        try:
            recs = parse_upload(os.path.basename(f), open(f, "rb").read())
            all_records += recs
        except Exception as e:
            errors.append(f"{os.path.basename(f)}: {e}")

    print(f"[课节] 解析 {len(lesson_files)} 个文件，共 {len(all_records)} 条记录")
    for e in errors:
        print("    [解析失败]", e)

    # ---------- 3) 按姓名路由到班级并生成版本 ----------
    result = route_merge_lessons(all_records, "reimport")
    print("[合并] 受影响班级：")
    for a in result["affected"]:
        print("    ", a)
    if result["unmatched"]["names"]:
        print("[注意] 以下学生不在任何班级名单，已记入「默认名单」，请核对是否要补进名单：")
        print("    ", "、".join(result["unmatched"]["names"]))
    if result["claimed"]:
        print("[迁移] 已从「默认名单」迁到正式班：", "、".join(result["claimed"]))

    # ---------- 4) 班级现状 ----------
    print("\n班级现状：")
    for c in list_classes():
        records, _ = get_current_records(c["id"])
        print(f"    {c['name']}: 学生 {c['student_count']} 人 | 版本 {c['version_count']} 个 | 当前 {len(records)} 条明细")
    return 0


if __name__ == "__main__":
    sys.exit(main())
