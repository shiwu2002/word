# -*- coding: utf-8 -*-
"""课时数据「按姓名路由 → 合并 → 生成版本」的公共流水线。

app.py 的上传课时、以及 reimport.py 的批量导入共用同一套路由逻辑，
确保两处行为一致，也避免把这一段业务逻辑复制两份。
"""
from datastore import (
    get_real_student_map,
    get_or_create_default_class,
    get_default_class_id,
    get_roster,
    add_students_to_class,
    remove_students_from_class,
    get_current_records,
    get_class,
    save_version,
)
from parser import merge_records


def route_merge_lessons(all_records, source):
    """把解析好的课时记录按学生姓名路由到对应班级，并合并生成新版本。

    参数:
      all_records: parser.parse_upload 产出的记录列表。
      source: 来源文件名字符串（写入版本记录），如文件名列表或 "reimport"。

    返回 dict:
      affected:  ["班级名(v版本，+新增~更新)", ...]  已生成版本的班级
      unmatched: {"names": [...], "default_created": bool}  落入「默认名单」的学生
      claimed:   [姓名]  从「默认名单」迁移到正式班的学生

    业务规则（与系统原语义一致）:
      1. 正式班名单内学生 → 路由到对应正式班。
      2. 不在任何正式班名单的学生 → 记入「默认名单」班。
      3. 「默认名单」中后续被正式班认领的学生 → 名单移除 + 历史课时迁入正式班，
         迁移记录排在本批上传之前，确保本次上传能覆盖其旧值。
    """
    real_map = get_real_student_map()  # {学生姓名: [正式班class_id,...]}，排除默认班

    # 1) 路由：正式班名单内的进对应班；其余收集为未匹配
    class_records = {}     # class_id -> [记录]
    unmatched = []         # 不在任何正式班名单中的记录
    for r in all_records:
        cids = real_map.get(r["学生姓名"])
        if cids:
            for cid in cids:
                class_records.setdefault(cid, []).append(r)
        else:
            unmatched.append(r)

    # 2) 未匹配学生 → 记入默认班：登记名单 + 路由记录
    default_cid = None
    default_created = False
    if unmatched:
        default_cid, default_created = get_or_create_default_class()
        add_students_to_class(default_cid, sorted({r["学生姓名"] for r in unmatched}))
        class_records.setdefault(default_cid, []).extend(unmatched)

    # 3) 默认班中已被正式班认领的学生 → 迁移到正式班
    claimed_names = []
    if default_cid is None:
        default_cid = get_default_class_id()
    if default_cid:
        claimed_names = [name for name in get_roster(default_cid) if name in real_map]
        if claimed_names:
            remove_students_from_class(default_cid, claimed_names)
            default_records, _ = get_current_records(default_cid)
            for name in claimed_names:
                student_records = [
                    r for r in default_records if r["学生姓名"] == name
                ]
                if not student_records:
                    continue
                for cid in real_map[name]:
                    # 迁移记录放在本次上传记录之前，确保本次上传覆盖迁移的旧值
                    class_records.setdefault(cid, [])[:0] = student_records
            # 确保默认班在 class_records 中，以便步骤 4 剔除迁出的学生
            class_records.setdefault(default_cid, [])

    # 4) 每个受影响班级：与当前数据合并并生成新版本
    affected = []
    for cid, recs in class_records.items():
        existing, _ = get_current_records(cid)
        if cid == default_cid and claimed_names:
            existing = [r for r in existing if r["学生姓名"] not in claimed_names]
        merged, n_new, n_upd = merge_records(existing, recs)
        ver = save_version(cid, merged, source, n_new, n_upd)
        cls = get_class(cid)
        affected.append(f"{cls['name']}(v{ver}，+{n_new}~{n_upd})")

    return {
        "affected": affected,
        "unmatched": {
            "names": sorted({r["学生姓名"] for r in unmatched}),
            "default_created": default_created,
        },
        "claimed": sorted(claimed_names),
    }
