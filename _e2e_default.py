# -*- coding: utf-8 -*-
"""验证：未匹配学生记入「默认名单」班 + 被正式班认领后自动迁移。"""
import io, glob
import datastore
from app import app

datastore.init_db()
client = app.test_client()
client.testing = True

def lesson_data(path):
    # 每次返回新的 BytesIO，避免复用已读流的报错
    return {"files": (io.BytesIO(open(path, "rb").read()), path)}

f = sorted(glob.glob("课堂成就明细_*.xlsx"))[0]
import parser as P
file_students = sorted({r["学生姓名"] for r in P.parse_upload(f, open(f, "rb").read())})
print("[0] 课时文件学生数:", len(file_students))

# 1. 先不导入任何名单，直接上传课时 → 全部学生应进「默认名单」
resp = client.post("/lessons/upload", data=lesson_data(f),
                   content_type="multipart/form-data", follow_redirects=True)
body = resp.data.decode("utf-8")
print("[1] 无名单上传: 含默认名单提示:", "已记入「默认名单」" in body)
classes = datastore.list_classes()
default = [c for c in classes if c["name"] == "默认名单"]
print("    默认班存在:", bool(default))
if default:
    dc = default[0]
    roster = datastore.get_roster(dc["id"])
    recs, vinfo = datastore.get_current_records(dc["id"])
    print("    默认班人数:", len(roster), "(应=%d)" % len(file_students),
          "| 记录数:", len(recs), "| 版本v", vinfo["version_number"])
    print("    学生一致:", set(roster) == set(file_students))

# 2. 导入名单（3个正式班）
rb = open("0724学生档案1.xlsx", "rb").read()
client.post("/roster/import",
            data={"file": (io.BytesIO(rb), "0724学生档案1.xlsx")},
            content_type="multipart/form-data", follow_redirects=True)
real_map = datastore.get_real_student_map()
matched = sorted(s for s in file_students if s in real_map)
unmatched = sorted(s for s in file_students if s not in real_map)
print("[2] 导入名单后: 匹配正式班 %d 人, 未匹配 %d 人" % (len(matched), len(unmatched)))

# 3. 再次上传同一课时文件 → 已认领学生应从默认班迁移到正式班
resp = client.post("/lessons/upload", data=lesson_data(f),
                   content_type="multipart/form-data", follow_redirects=True)
body = resp.data.decode("utf-8")
print("[3] 二次上传: 含合并提示:", "已按学生所属班级合并" in body,
      "| 含迁移提示:", "已从「默认名单」迁移" in body)

# 4. 验证默认班名单：已认领学生应已移除，只剩未匹配学生
if default:
    dc = default[0]
    d_roster = datastore.get_roster(dc["id"])
    d_recs, _ = datastore.get_current_records(dc["id"])
    d_names = sorted({r["学生姓名"] for r in d_recs})
    print("[4] 默认班名单人数:", len(d_roster), "(应=%d 未匹配)" % len(unmatched))
    print("    默认班名单==未匹配:", set(d_roster) == set(unmatched))
    print("    默认班记录中的学生:", len(d_names), "(应=%d)" % len(unmatched))
    print("    默认班记录学生==未匹配:", set(d_names) == set(unmatched))
    # 已认领学生不应再出现在默认班
    leaked = [s for s in matched if s in d_roster]
    print("    已认领学生残留默认班名单:", leaked)

# 5. 验证正式班记录中包含已认领学生的历史记录
all_real_recs = []
for c in datastore.list_classes():
    if c["name"] == "默认名单":
        continue
    recs, _ = datastore.get_current_records(c["id"])
    all_real_recs.extend(recs)
real_names = sorted({r["学生姓名"] for r in all_real_recs})
print("[5] 正式班记录中的学生数:", len(real_names))
covered = [s for s in matched if s in real_names]
print("    已认领学生进入正式班记录:", len(covered), "/", len(matched))

# 6. 导出默认班
if default:
    resp = client.get("/classes/%d/export" % default[0]["id"])
    print("[6] 默认班导出 xlsx签名:", resp.data[:2] == b"PK", "字节:", len(resp.data))

# 清理
for c in datastore.list_classes():
    datastore.delete_class(c["id"])
print("DONE")
