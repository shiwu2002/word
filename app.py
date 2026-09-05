# -*- coding: utf-8 -*-
"""学生课堂数据整合系统 —— Flask 主应用。

新版业务流程：
1. 「导入名单」：上传名单 Excel，每个工作表 → 一个班级（表名即班名），名单即该班固定学生框架。
2. 「上传课时数据」：上传课堂成就明细，系统按学生姓名自动路由到其所属班级进行合并；
   不在任何名单中的学生会被跳过并提示。每次合并生成可回退的版本快照。
3. 查看某班级：按名单框架展示学生汇总 + 明细，可导出 Excel，可回退版本。
"""
import os
import sys
import sqlite3
import threading
import webbrowser

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_file, session,
)

import datastore
from datastore import (
    init_db, list_classes, get_class, rename_class, delete_class,
    import_roster, get_roster,
    get_current_records, list_versions, rollback,
    get_setting, set_setting, get_api_key,
    save_evaluations, get_evaluations,
    get_previous_evals_by_student, get_history_evaluations,
)
from parser import (
    parse_upload, parse_roster, aggregate_for_class,
    export_excel, progress_for_class, EXPECTED_COLS,
)
from integration import route_merge_lessons
from config import SUMMARY_FIELDS
from llm import generate_evaluations

def resource_path(rel):
    """定位资源文件路径，兼容 PyInstaller 打包环境。

    打包后资源被解压到 sys._MEIPASS 临时目录；开发时用源码所在目录。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)
app.secret_key = "student-classroom-data-merge-2026"

init_db()  # 模块加载时初始化表结构（幂等）


# ---------------- 辅助 ----------------

def selected_cid():
    """从 query 参数或 session 取当前选中班级 id（仅用于“查看”）。"""
    cid = request.args.get("class_id", type=int)
    if cid:
        session["class_id"] = cid
        return cid
    return session.get("class_id")


@app.context_processor
def inject_globals():
    """上下文处理器：给所有模板自动注入 all_classes（班级下拉用）。"""
    return {"all_classes": list_classes()}


# ---------------- 主页 ----------------

@app.route("/")
def index():
    """主页：展示选中班级的名单框架汇总、明细、版本历史、学生评价。"""
    cid = selected_cid()
    current_class = get_class(cid) if cid else None
    records, version_info, versions, summary, roster = [], None, [], [], []
    evaluations, history_evaluations = [], []
    progress, selected_student = {"rows": [], "class_sessions": []}, ""
    if current_class:
        roster = get_roster(cid)                  # 该班级学生名单（固定框架）
        records, version_info = get_current_records(cid)
        versions = list_versions(cid)
        summary = aggregate_for_class(records, roster)  # 以名单为框架汇总
        progress = progress_for_class(records, roster)   # 进步曲线数据
        selected_student = request.args.get("student", "").strip()
        evaluations = get_evaluations(cid)              # 最新一批评价
        history_evaluations = get_history_evaluations(cid)  # 全部历史评价（按批次）
    return render_template(
        "index.html",
        current_class=current_class,
        records=records,
        summary=summary,
        roster=roster,
        versions=versions,
        version_info=version_info,
        expected_cols=EXPECTED_COLS,
        progress=progress,
        selected_student=selected_student,
        evaluations=evaluations,
        history_evaluations=history_evaluations,
        summary_fields=SUMMARY_FIELDS,
        api_key_configured=bool(get_api_key()),
    )


# ---------------- 名单导入 ----------------

@app.route("/roster/import", methods=["POST"])
def roster_import():
    """导入名单文件：每个工作表创建/更新一个班级及其学生名单。

    【Python 基础】
    - request.files.get("file")：取单个上传文件。
    - 列表推导式 [r for r in result if r["created"]] 按 created 过滤。
    - "、".join(...)：用顿号把名字列表拼成一句。
    """
    f = request.files.get("file")
    if not f or not f.filename:
        flash("请选择名单文件", "error")
        return redirect(url_for("index"))
    try:
        classes_data = parse_roster(f.read())  # [{name, students, skipped}]
    except Exception as e:
        flash(f"名单解析失败：{e}", "error")
        return redirect(url_for("index"))

    # 只导入找到姓名列的工作表
    valid = [c for c in classes_data if not c.get("skipped")]
    skipped_sheets = [c["name"] for c in classes_data if c.get("skipped")]
    if not valid:
        flash("未在名单文件中找到任何学生姓名列（需有“学生姓名/学员姓名”列）", "error")
        return redirect(url_for("index"))

    result = import_roster(valid)
    created = [r for r in result if r["created"]]
    updated = [r for r in result if not r["created"]]
    msg = f"名单导入完成：共 {len(result)} 个班级"
    if created:
        msg += "；新建 " + "、".join(f"{r['name']}({r['student_count']}人)" for r in created)
    if updated:
        msg += "；更新 " + "、".join(f"{r['name']}({r['student_count']}人)" for r in updated)
    flash(msg, "success")
    if skipped_sheets:
        flash(f"以下工作表未找到姓名列已跳过：{'、'.join(skipped_sheets)}", "error")
    return redirect(url_for("index"))


# ---------------- 课时数据上传（自动路由） ----------------

@app.route("/lessons/upload", methods=["POST"])
def lessons_upload():
    """上传课时数据：按学生姓名自动路由到其所属班级并合并。

    本函数只负责「解析文件 → 收集记录」，真正的路由/合并/版本生成逻辑已抽到
    integration.route_merge_lessons（app.py 与 reimport.py 共用，避免两套不一致）。
    路由规则见该函数：正式班名单内→对应班，其余→「默认名单」；
    默认班中后续被正式班认领的学生会自动迁移到正式班。

    【Python 基础】
    - request.files.getlist("files") 取同 name 的多个上传文件。
    - try/except ValueError 捕获解析业务异常，汇总到 errors 后统一提示。
    """
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("请选择至少一个文件", "error")
        return redirect(url_for("index"))

    # 1) 解析所有上传文件，汇总成记录
    all_records = []
    errors = []
    for f in files:
        try:
            all_records += parse_upload(f.filename, f.read())  # 列表拼接
        except ValueError as e:
            errors.append(f"{f.filename}：{e}")

    # 2) 按姓名路由到所属班级并合并（公共流水线，见 integration.py）
    source = "；".join(f.filename for f in files)
    result = route_merge_lessons(all_records, source)
    affected = result["affected"]
    unmatched = result["unmatched"]
    claimed_names = result["claimed"]

    # 3) 反馈
    if affected:
        flash("已按学生所属班级合并：" + "，".join(affected), "success")
    if unmatched["names"]:
        flash(
            f"以下学生不在任何班级名单，已记入「默认名单」：{'、'.join(unmatched['names'])}",
            "info",
        )
    if claimed_names:
        flash(
            "以下学生已被正式班级认领，已从「默认名单」迁移至对应班级："
            + "、".join(claimed_names),
            "info",
        )
    if errors:
        flash("以下文件解析失败：" + " | ".join(errors), "error")
    if not affected and not errors:
        flash("没有可解析的课时数据", "error")
    return redirect(url_for("index"))


# ---------------- 班级管理 ----------------

@app.route("/classes/<int:cid>/rename", methods=["POST"])
def classes_rename(cid):
    """重命名班级。"""
    name = request.form.get("name", "").strip()
    if not name:
        flash("班级名称不能为空", "error")
    else:
        try:
            rename_class(cid, name)
            flash("班级已重命名", "success")
        except sqlite3.IntegrityError:
            flash("班级名称已存在", "error")
    return redirect(url_for("index", class_id=cid))


@app.route("/classes/<int:cid>/delete", methods=["POST"])
def classes_delete(cid):
    """删除班级（级联删除名单与版本）。"""
    cls = get_class(cid)
    if not cls:
        flash("班级不存在", "error")
    else:
        delete_class(cid)
        if session.get("class_id") == cid:
            session.pop("class_id", None)
        flash(f"已删除班级：{cls['name']}（含名单与全部版本数据）", "success")
    return redirect(url_for("index"))


# ---------------- 回退 ----------------

@app.route("/classes/<int:cid>/rollback", methods=["POST"])
def classes_rollback(cid):
    """回退该班级到上一版本。"""
    if not get_class(cid):
        flash("班级不存在", "error")
        return redirect(url_for("index"))
    removed = rollback(cid)
    if removed:
        flash(f"已回退：删除版本 v{removed}，当前数据恢复到上一版本提交前的状态", "success")
    else:
        flash("没有可回退的版本", "error")
    return redirect(url_for("index", class_id=cid))


# ---------------- 导出 ----------------

@app.route("/classes/<int:cid>/export")
def classes_export(cid):
    """导出该班级整合数据为 Excel（汇总按名单框架 + 明细）。"""
    cls = get_class(cid)
    if not cls:
        flash("班级不存在", "error")
        return redirect(url_for("index"))
    records, _ = get_current_records(cid)
    roster = get_roster(cid)
    if not roster:
        flash("该班级名单为空，无法导出", "error")
        return redirect(url_for("index", class_id=cid))
    summary = aggregate_for_class(records, roster)
    out = export_excel(records, summary)
    return send_file(
        out,
        as_attachment=True,
        download_name=f"{cls['name']}_整合数据.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------- LLM 评价 ----------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    """设置页面：配置 DeepSeek API key。"""
    if request.method == "POST":
        key = request.form.get("api_key", "").strip()
        if key:
            set_setting("deepseek_api_key", key)
            flash("API key 已保存", "success")
        else:
            flash("API key 不能为空", "error")
        return redirect(url_for("settings"))
    current_key = get_api_key()
    # 脱敏显示：只显示末4位
    masked = ""
    if current_key:
        masked = "*" * (len(current_key) - 4) + current_key[-4:] if len(current_key) > 4 else "****"
    return render_template(
        "settings.html",
        masked_key=masked,
        has_key=bool(current_key),
    )


@app.route("/classes/<int:cid>/evaluate", methods=["POST"])
def classes_evaluate(cid):
    """根据用户选定的字段，调用 LLM 为每个学生生成评价。"""
    cls = get_class(cid)
    if not cls:
        flash("班级不存在", "error")
        return redirect(url_for("index"))

    api_key = get_api_key()
    if not api_key:
        flash("请先在「设置」中配置 DeepSeek API key", "error")
        return redirect(url_for("index", class_id=cid))

    # 收集用户选定的依据字段
    fields = request.form.getlist("fields")
    if not fields:
        flash("请至少选择一个依据字段", "error")
        return redirect(url_for("index", class_id=cid))

    # 获取汇总数据
    records, _ = get_current_records(cid)
    roster = get_roster(cid)
    summary = aggregate_for_class(records, roster)

    # 获取历史评价，供 LLM 综合参考
    previous_evals = get_previous_evals_by_student(cid)
    custom_style = request.form.get("custom_style", "").strip()
    evaluations, error = generate_evaluations(summary, fields, api_key, previous_evals, custom_style)
    if error:
        flash(f"评价生成失败：{error}", "error")
        return redirect(url_for("index", class_id=cid))

    save_evaluations(cid, evaluations, fields)
    flash(f"已为 {len(evaluations)} 名学生生成评价（依据：{'、'.join(fields)}）", "success")
    return redirect(url_for("index", class_id=cid))


# 只有直接运行本文件时才启动开发服务器。
if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        # 打包环境：延迟打开浏览器；不能用 debug（reloader 与 PyInstaller 不兼容）
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000/")).start()
        app.run(debug=False, host="127.0.0.1", port=5000)
    else:
        app.run(debug=True, host="127.0.0.1", port=5000)
