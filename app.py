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
import tempfile
import threading
import time
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
    get_setting, set_setting, get_api_key, get_model_name,
    save_evaluations, get_evaluations,
    get_previous_evals_by_student, get_history_evaluations,
    save_achievement, get_achievement_questions, save_question_template,
    get_achievement_for_students,
)
from parser import (
    parse_upload, parse_roster, aggregate_for_class,
    export_excel, progress_for_class, rankings_for_class,
    cross_class_rankings, session_owners, annotate_makeup, EXPECTED_COLS,
    parse_achievement,
)
from integration import route_merge_lessons
from config import SUMMARY_FIELDS
from llm import generate_evaluations, list_models

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
    achievement_questions = []
    progress, selected_student = {"rows": [], "class_sessions": []}, ""
    rankings = {"lessons": []}
    # 先取所有正式班（排除「默认名单」）的当前记录：既用于跨班对比，也用于判定补课归属
    class_records = []
    for c in list_classes():
        if c["name"] == "默认名单":
            continue
        recs, _ = get_current_records(c["id"])
        if recs:
            class_records.append({"class_id": c["id"], "class_name": c["name"], "records": recs})
    owners = session_owners(class_records) if class_records else {}
    cross_class = (
        cross_class_rankings(class_records) if class_records else {"lessons": [], "classes": []}
    )

    if current_class:
        roster = get_roster(cid)                  # 该班级学生名单（固定框架）
        records, version_info = get_current_records(cid)
        versions = list_versions(cid)
        # 补课标注：本班学生若上了别班时段的课（该时段归属班≠本班）记「补课」。「默认名单」不标
        if current_class["name"] != "默认名单":
            annotate_makeup(records, owners, cid)
        summary = aggregate_for_class(records, roster)  # 以名单为框架汇总
        progress = progress_for_class(records, roster)   # 进步曲线数据
        rankings = rankings_for_class(records)           # 本班各课节正确率/奖杯排行
        selected_student = request.args.get("student", "").strip()
        evaluations = get_evaluations(cid)              # 最新一批评价
        history_evaluations = get_history_evaluations(cid)  # 全部历史评价（按批次）
        achievement_questions = get_achievement_questions()  # 课测题目与讲评模板

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
        rankings=rankings,
        cross_class=cross_class,
        evaluations=evaluations,
        history_evaluations=history_evaluations,
        achievement_questions=achievement_questions,
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


# ---------------- 课堂成就明细（逐题）导入与讲评模板 ----------------

@app.route("/achievement/import", methods=["POST"])
def achievement_import():
    """导入课堂成就明细 Excel（逐题明细 + 错题清单），保存逐题对错与题目元数据。"""
    f = request.files.get("file")
    if not f or not f.filename:
        flash("请选择课堂成就明细文件", "error")
        return redirect(url_for("index"))
    try:
        data = parse_achievement(f.read(), filename=f.filename)
    except ValueError as e:
        flash(f"成就明细解析失败：{e}", "error")
        return redirect(url_for("index"))
    n_students, n_questions = save_achievement(data)
    wrong_total = sum(len(q["wrong_students"]) for q in data["questions"])
    # 实际逐题结果（答对/答错）的道数：来自学生每题 results 的并集（CSV 无错题清单时用）
    q_keys = set()
    for st in data["students"]:
        q_keys.update(st["results"])
    n_qkeys = len(q_keys)
    flash(
        f"已导入课堂成就明细：{data['lesson_name']}（{data['lesson_time'][:10]}）— "
        f"{n_students} 名学生、{max(n_questions, n_qkeys)} 道题、{wrong_total} 人次答错。"
        "可到「学生评价」标签为每道题配置讲评模板。",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/achievement/template", methods=["POST"])
def achievement_template_save():
    """保存某课节某题的错题讲评模板（评价页 AJAX 调用）。"""
    lesson = request.form.get("lesson_name", "").strip()
    qkey = request.form.get("question_key", "").strip()
    template = request.form.get("reply_template", "").strip()
    if not lesson or not qkey:
        return {"ok": False, "error": "参数缺失"}, 400
    save_question_template(lesson, qkey, template)
    return {"ok": True}


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
    """设置页面：配置 DeepSeek API key 与模型名称。"""
    if request.method == "POST":
        key = request.form.get("api_key", "").strip()
        model = request.form.get("model", "").strip()
        if key:
            set_setting("deepseek_api_key", key)
            flash("API key 已保存", "success")
        if model:
            set_setting("deepseek_model", model)
            flash(f"模型名称已保存：{model}", "success")
        if not key and not model:
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
        model_name=get_model_name(),
    )


@app.route("/api/models")
def api_models():
    """返回当前 API key 可用的模型列表（来自 /models 端点），供设置页下拉。"""
    models, error = list_models()
    return {"models": models, "error": error}


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
    # 课测逐题数据（答错题目 + 用户配置的讲评模板），按班级名单学生过滤
    achievement = get_achievement_for_students([s["学生姓名"] for s in summary])
    evaluations, error = generate_evaluations(
        summary, fields, api_key, previous_evals, custom_style, achievement
    )
    if error:
        flash(f"评价生成失败：{error}", "error")
        return redirect(url_for("index", class_id=cid))

    fields_used = fields + (["错题讲评模板"] if achievement else [])
    save_evaluations(cid, evaluations, fields_used)
    flash(f"已为 {len(evaluations)} 名学生生成评价（依据：{'、'.join(fields)}）", "success")
    return redirect(url_for("index", class_id=cid))


# ---------------- 单实例 & 页面生命周期 ----------------
# 打包环境（console=False）没有任何窗口/托盘可用来退出，进程会一直挂在后台。
# 这里把应用做成「单例模型」：
#   1) 单实例锁：同一台机器只允许一个后端在跑；重复启动只打开已有页面后退出。
#   2) 页面守护：每个打开的页面定时上报心跳；当最后一个页面关闭（或浏览器崩溃、
#      被杀）后自动关停后端，避免「关闭网页后后端服务残留」。

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "biscuit-word-data-instance.lock")

_heartbeats = {}      # token -> 最近心跳时间
_leaving = {}         # token -> 请求离开时间（用于区分「同页签导航/刷新」与「真正关闭」）
_heartbeat_lock = threading.Lock()
_HEARTBEAT_TIMEOUT = 600.0   # 心跳超时兜底：浏览器崩溃/被系统杀掉后清理孤儿进程
_LEAVE_GRACE = 6.0           # leave 后宽限：同页签导航/刷新会在此窗口内重新注册心跳
_MONITOR_INTERVAL = 3.0
_STARTUP_GRACE = 20.0        # 启动宽限：给浏览器自动打开并注册心跳留时间

_server_handle = None        # werkzeug server（monitor 线程里用于关停）
_server_started = 0.0


class _SingleInstance:
    """跨平台单实例锁（OS 文件锁）。进程退出时内核自动释放，不会残留失效锁。"""

    def __init__(self, path):
        self._path = path
        self._fd = None

    def _close_fd(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def acquire(self):
        # 用局部 fd，避免重复 acquire 时覆盖 self._fd 而泄漏首次加锁的句柄
        fd = None
        try:
            parent = os.path.dirname(self._path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            self._fd = fd
            return True
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False

    def release(self):
        if self._fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            self._close_fd()


@app.route("/api/heartbeat", methods=["GET", "POST"])
def api_heartbeat():
    """页面生命周期心跳：页面上报「我还在 / 我要走了 / 我(重新)加载了」。

    - leave=1：该页签请求离开（真实关闭 → 在宽限期后清理；导航/刷新会有 reg=1 取消它）。
    - reg=1 ：页面(重新)加载（导航/刷新），取消同页签的 pending leave，重新算存活。
    - 其它  ：普通保活，只刷新心跳；**不**取消 pending leave（避免关闭瞬间的在途
              保活心跳把「我要走了」误判为「我还在」）。
    """
    payload = request.get_json(silent=True) or {}
    token = request.args.get("t") or payload.get("t")
    leave = request.args.get("leave") or payload.get("leave")
    reg = request.args.get("reg") or payload.get("reg")
    if token:
        now = time.time()
        with _heartbeat_lock:
            if leave in ("1", "true", True):
                _leaving[token] = now
            else:
                _heartbeats[token] = now
                if reg in ("1", "true", True):
                    _leaving.pop(token, None)  # 新页面加载：撤销先前的离开标记
    return {"ok": True}


def _prune_and_orphaned():
    """清理过期客户端；返回是否已无任何活动页面（应关停后端）。"""
    now = time.time()
    if now - _server_started < _STARTUP_GRACE:  # 启动期别误杀（等浏览器注册心跳）
        return False
    with _heartbeat_lock:
        for t in list(_heartbeats):
            if now - _heartbeats[t] > _HEARTBEAT_TIMEOUT:
                del _heartbeats[t]
                _leaving.pop(t, None)
        for t in list(_leaving):
            if now - _leaving[t] > _LEAVE_GRACE:
                del _leaving[t]
                _heartbeats.pop(t, None)
    return not _heartbeats and not _leaving


def _stop_server():
    h = _server_handle
    if h is not None:
        try:
            h.shutdown()
        except Exception:
            pass


def _monitor_loop():
    while True:
        time.sleep(_MONITOR_INTERVAL)
        if _prune_and_orphaned():
            _stop_server()
            return


# 只有直接运行本文件时才启动开发服务器。
if __name__ == "__main__":
    host, port = "127.0.0.1", 5000
    url = f"http://{host}:{port}/"
    if getattr(sys, "frozen", False):
        # 单实例：已有实例则只打开已有页面，不重复起服务
        inst = _SingleInstance(_LOCK_PATH)
        if not inst.acquire():
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
            sys.exit(0)
        try:
            from werkzeug.serving import make_server
            server = make_server(host, port, app, threaded=True)
        except OSError:
            # 端口被占用（如历史残留进程）：打开已有页面后退出
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
            sys.exit(0)
        _server_started = time.time()
        _server_handle = server
        threading.Thread(target=_monitor_loop, daemon=True).start()
        # 打包环境：延迟打开浏览器；不能用 debug（reloader 与 PyInstaller 不兼容）
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()
            inst.release()
    else:
        app.run(debug=True, host=host, port=port)
