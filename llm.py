# -*- coding: utf-8 -*-
"""LLM 评价生成层：调用 DeepSeek API 为每个学生生成评价。

DeepSeek 兼容 OpenAI 接口格式：
    POST {base_url}/chat/completions
    Header: Authorization: Bearer {api_key}

结构化输出约定：让模型按固定 JSON 数据结构回复，再解析该结构取每个学生的评价，
避免按行解析时因评价折行、姓名带修饰等导致的解析错误。
    {"students": [{"name": "学生姓名", "evaluation": "评价内容"}, ...]}

调用流程：
    generate_evaluations(summary, fields, api_key) → (evaluations, error)
"""
import json
import re

import requests

import datastore

try:
    import config
except ImportError:
    config = None


_LOW_RATE_HIDE = 0.7  # 正确率低于此值时不作为素材提供给模型（照顾学生心理）

# 复姓表：{name} 去姓留名时整体剥掉
_COMPOUND_SURNAMES = (
    "欧阳", "司马", "上官", "诸葛", "夏侯", "皇甫", "尉迟", "长孙", "慕容", "端木",
    "东方", "独孤", "南宫", "西门", "司徒", "司空", "公孙", "令狐", "宇文", "轩辕",
    "呼延", "鲜于", "钟离", "公冶", "太叔", "申屠", "澹台", "濮阳", "淳于", "赫连",
    "宗政", "闻人", "第五",
)


def given_name(full):
    """去掉姓只保留名：复姓整体剥掉，单姓去掉首字（王帅杰→帅杰，柯含→含）。"""
    name = str(full or "").strip()
    for cs in _COMPOUND_SURNAMES:
        if name.startswith(cs) and len(name) > len(cs):
            return name[len(cs):]
    return name[1:] if len(name) >= 2 else name


def _format_value(field, val):
    """格式化单个字段值用于 prompt 展示。"""
    if field == "课中正确率":
        return f"{val * 100:.1f}%"
    if field == "平均得分率":
        return f"{val * 100:.1f}%"
    return str(val)


def _achievement_lines(ach):
    """把单个学生的课测逐题数据格式化成 prompt 行（无数据返回空串）。

    得分率低于 _LOW_RATE_HIDE 时不再展示具体得分，只保留错题与讲评模板，
    避免低分数字进入模型输出打击学生信心。
    """
    if not ach:
        return ""
    oj = ach.get("oj")
    oj_lines = []
    if oj:
        oj_ok = oj["hw_passed"] >= oj["hw_total"] and oj["ext_passed"] >= oj["ext_total"]
        oj_lines = [f"   📘 OJ 通过：课后作业 {oj['hw_passed']}/{oj['hw_total']}，"
                    f"课后拓展 {oj['ext_passed']}/{oj['ext_total']}"
                    + ("" if oj_ok else "（未全部通过，评价中必须提醒家长）")]
    if not ach.get("lesson_name"):
        # 只有 OJ 数据、没有课测记录的学生：只给 OJ 行
        lines = []
    else:
        head = f"   本次课测（{_short_lesson(ach['lesson_name'])}，{ach['lesson_time'][:10]}）："
        if ach.get("missing"):
            lines = [head, f"   ⚠️ 未完成：{'、'.join(ach['missing'])}（评价中必须提醒家长督促孩子尽快完成）"]
        elif ach["score_rate"] < _LOW_RATE_HIDE:
            lines = [head]
        else:
            lines = [head + f"得分率 {ach['score_rate'] * 100:.0f}%（正确 {ach['correct_count']} 题）"]
    lines = lines + oj_lines
    if ach["wrongs"]:
        lines.append("   答错题目（供讲评参考）：")
        for w in ach["wrongs"]:
            stem = re.sub(r"\s+", "", w["stem"])[:60]
            chosen = f"，该生选了 {w['choice']}" if w["choice"] else ""
            lines.append(
                f"   - {w['question_key']}（知识点：{w['knowledge_point'] or '未知'}）："
                f"{stem}…（正确答案 {w['correct_option']}{chosen}）"
            )
            if w["reply_template"]:
                lines.append(f"     讲评模板：{w['reply_template']}")
    return "\n".join(lines)


def _short_lesson(name):
    """课节长名缩写（如 U260724-…-P9-递推算法（二） → P9-递推算法（二））。"""
    m = re.search(r"(P\d+-\S+)", str(name))
    return m.group(1) if m else str(name)


# ---------------- 自定义模板的占位符体系 ----------------
# 模板中 {xxx} 会在生成时替换为每位学生的真实数据。系统锁定四个评价参数：
#   {name} 学生名字（去姓）  {课中正确率} 正确率数值（低于阈值不提供）
#   {课后小测} 全对原文 / 讲评模板 / 未完成提醒
#   {课后作业OJ和课后扩展OJ的完成情况} 完成 / 未完成（提醒家长）
# 其余占位符一律在输出中删除。
_PLACEHOLDER_RE = re.compile(r"\{([^{}\n]+)\}")


def _quiz_value(ach):
    """{课后小测} 的替换值（准确二态：全对 / 未完成，无模糊表述）。

    - 无课堂小测错题 → 原文替换「课后小测」（句子「{课后小测}全部通过✅」成立）；
    - 有错题且配了讲评模板 → 给出讲评模板内容，替换「全部通过✅」表述；
    - 有错题无模板 → 按知识点简要提及并鼓励；
    - 无课测数据/未作答 → 未完成，提醒家长（不出现「待确认」类模糊说法）。
    """
    if not ach or not ach.get("lesson_name"):
        return ("（需改写所在句子：该生课后小测未完成，请在此提醒家长督促孩子尽快完成，"
                "不得表述为「全部通过」）")
    if any("课堂小测" in c for c in ach.get("missing", [])):
        return ("（需改写所在句子：该生未完成课后小测，请在此提醒家长督促孩子尽快完成，"
                "不得表述为「全部通过」）")
    wrong = [w for w in ach["wrongs"] if str(w["question_key"]).startswith("课堂小测")]
    if not wrong:
        return "课后小测"
    parts = []
    for w in wrong:
        kp = w["knowledge_point"] or "未知知识点"
        if w.get("reply_template"):
            parts.append(f"「{w['question_key']}·{kp}」讲评模板：{w['reply_template']}")
        else:
            parts.append(f"「{w['question_key']}·{kp}」简要提及并鼓励（无讲评模板）")
    return (f"课后小测讲评（该生有 {len(wrong)} 题答错，需用以下内容替换「全部通过✅」的表述，"
            f"保持语句连贯自然）：" + "；".join(parts))


def _oj_overall_value(ach):
    """{课后作业OJ和课后扩展OJ的完成情况} 的替换值（准确二态：完成 / 未完成）。

    全部完成 → 直接给可复制的自然语句；
    有未完成 → 以「（需改写所在句子：…）」开头的改写指示（指令前置，杜绝模型把
    指示文字当正文复制），并要求删除模板中与之矛盾的表述。
    """
    oj = (ach or {}).get("oj") or {}
    hw_ok = oj.get("hw_passed", 0) >= oj.get("hw_total", 2)
    ext_ok = oj.get("ext_passed", 0) >= oj.get("ext_total", 2)
    if hw_ok and ext_ok:
        return "课后作业OJ和课后拓展OJ都已完成"
    parts = []
    for label, ok in (("课后作业OJ", hw_ok), ("课后拓展OJ", ext_ok)):
        parts.append(f"{label}{'已完成' if ok else '还未完成'}")
    return ("（需改写所在句子：该生" + "、".join(parts)
            + "，请如实表述并在此提醒家长督促孩子尽快补上未完成的部分，"
              "并删除模板中「作业完成情况很好」等与之矛盾的表述）")


def _placeholder_value(key, s, ach):
    """单个占位符的替换值。

    系统锁定四个评价参数：{name}、{课中正确率}、{课后小测}、
    {课后作业OJ和课后扩展OJ的完成情况}；其余占位符返回 None（输出时删除）。
    """
    k = key.strip()
    if k in ("name", "姓名", "学生姓名"):
        # 称呼用：去掉姓只保留名（王帅杰→帅杰，欧阳皓辰→皓辰）
        return given_name(s["学生姓名"])
    if k == "课中正确率":
        rate = s.get("课中正确率", 0)
        if rate < _LOW_RATE_HIDE:
            return "（课中正确率偏低不提供——请改写所在句子，严禁出现任何具体正确率数字）"
        return f"{rate * 100:.0f}"
    if k == "课后小测":
        return _quiz_value(ach)
    if "OJ" in k.upper() and "完成情况" in k:
        return _oj_overall_value(ach)
    return None


def _placeholder_section(template, students, achievement, previous_evals=None):
    """拼装「模板 + 每位学生占位符取值表」的 prompt 段落。"""
    keys = []
    for m in _PLACEHOLDER_RE.finditer(template):
        k = m.group(1).strip()
        if k not in keys:
            keys.append(k)
    parts = [
        "评价模板（所有学生共用，输出时必须逐字保留模板文字与换行，仅替换 {占位符}）：",
        template,
        "",
        "每位学生的占位符取值：",
    ]
    for i, s in enumerate(students, 1):
        ach = (achievement or {}).get(s["学生姓名"])
        rows = [f"{i}. {s['学生姓名']}"]
        for k in keys:
            v = _placeholder_value(k, s, ach)
            if v is None:
                v = f"（未知占位符——输出时删除 {{{k}}} 及其紧邻的说明性文字）"
            rows.append(f"   {{{k}}} = {v}")
        if previous_evals and s["学生姓名"] in previous_evals:
            rows.append(f"   历史评价：{' ｜ '.join(previous_evals[s['学生姓名']])}")
        parts.append("\n".join(rows))
    return "\n".join(parts)


def _build_prompt(summary, fields, previous_evals=None, custom_style="", achievement=None):
    """构建发送给 LLM 的 prompt。

    summary: 有记录的学生汇总列表
    fields: 选定的依据字段列表
    previous_evals: {学生姓名: [历史评价文本, ...]}，用于综合参考
    custom_style: 自定义评价语模板，LLM 据此生成风格一致的评价
    achievement: {学生姓名: {课测逐题数据}}，含答错题目与用户配置的讲评模板
    """
    placeholder_mode = bool(custom_style) and bool(_PLACEHOLDER_RE.search(custom_style))
    has_template = any(
        w.get("reply_template") for a in (achievement or {}).values() for w in a.get("wrongs", [])
    )
    template_rule = ""
    privacy_rule = ""
    if placeholder_mode:
        # 占位符模板模式：模板 + 每位学生取值表，模型只做替换与必要的句子微调
        students_text = _placeholder_section(custom_style, summary, achievement, previous_evals)
        field_labels = ""
        style_hint = ""
        requirements = (
            "要求：\n"
            "1. 系统锁定四个评价参数：{name}、{课中正确率}、{课后小测}、"
            "{课后作业OJ和课后扩展OJ的完成情况}；无论模板如何变化，都只用这四个参数替换\n"
            "2. 模板文字逐字保留（包括换行分段、表情符号、标点），只把 {占位符} 替换成该学生对应的取值；"
            "{name} 替换为该学生的名字（去掉姓只保留名，如 王帅杰→帅杰、柯含→含），"
            "模板中如出现示例学生姓名（如「柯含」），出现的每一处同样替换成该学生的名字\n"
            "3. {课后小测} 和 OJ 完成情况的取值是准确结论，只有「完成/全对」与「未完成」两种状态，"
            "必须按取值如实表述，严禁出现「待确认」「数据暂缺」等模糊说法；未完成的必须提醒家长督促完成\n"
            "4. 取值以「（需改写」或「讲评（」开头的为改写/替换指示，必须按说明执行，"
            "但指示文字本身严禁原样复制进评价；其余取值必须逐字复制，不得改写、缩写或遗漏任何字词"
            "（如「课后作业OJ」不得写成「课后作业O」或「课后拓展0」）；"
            "除此之外严禁出现任何具体的正确率、得分、答错数量或排名数字\n"
            "5. 模板表述与取值冲突时（如模板写「都完成了」但取值为未完成），以取值为准微调该句\n"
            "6. 普通取值直接替换；未知/废弃占位符按取值说明删除，输出中不得残留任何 { 或 } 字符\n"
            "7. 全文对家长口吻，语气鼓励、积极向上；如有历史评价，参考并体现进步\n"
        )
        opening = "请按「评价模板」与每位学生的占位符取值，为每个学生生成最终评价。"
    else:
        field_labels = "、".join(fields)
        lines = []
        for i, s in enumerate(summary, 1):
            data_parts = []
            for f in fields:
                val = s.get(f, 0)
                # 低正确率的学生：不把该字段提供给模型（照顾学生心理）
                if f in ("课中正确率", "平均得分率") and val < _LOW_RATE_HIDE:
                    continue
                data_parts.append(f"{f}={_format_value(f, val)}")
            line = f"{i}. {s['学生姓名']}" + (f" — {'，'.join(data_parts)}" if data_parts else "")
            # 若有历史评价，附在该学生数据下方供 LLM 参考
            if previous_evals and s["学生姓名"] in previous_evals:
                evals = previous_evals[s["学生姓名"]]
                line += f"\n   历史评价：{' ｜ '.join(evals)}"
            ach_text = _achievement_lines((achievement or {}).get(s["学生姓名"]))
            if ach_text:
                line += f"\n{ach_text}"
            lines.append(line)

        students_text = "\n".join(lines)
        style_hint = ""
        if custom_style:
            style_hint = (
                "参考评价模板（以下是一位学生「柯含」的完整范文，所有学生的评价都必须套用这个结构逐段生成）：\n"
                f"{custom_style}\n\n"
            )
            requirements = (
                "要求：\n"
                "1. 每位学生的评价必须严格套用「参考评价模板」的结构：段落顺序、开头称呼"
                "（如 XX妈妈/爸爸）、表情符号、结尾风格都与模板一致，总长度与模板相当；"
                "模板中的换行与分段必须原样保留，不要合并成一段\n"
                "2. 模板中的课程内容段落（本周学了什么、课中练习了哪些题型等）是班级共性的，"
                "原样保留不要改写；学生个人表现部分（到课情况、课中正确率、作业完成情况等）"
                "用该学生的真实数据替换\n"
                "3. 模板中的姓名占位符（如 XX、{姓名}、{学生姓名}、柯含 等出现的每一处）"
                "一律替换成当前学生的真实姓名；其余共用内容保持不变\n"
                "4. 基于该学生的数据给出具体、有针对性的评价，语气鼓励、积极向上\n"
                "5. 如有历史评价，请参考并体现学生的进步与变化\n"
            )
        else:
            requirements = (
                "要求：\n"
                "1. 每个学生一段评价，50-100字\n"
                "2. 以发给家长的视角撰写：开头用「XX妈妈/爸爸」（XX 为学生名字，去掉姓只保留名，"
                "如 王帅杰→帅杰），向家长同步孩子本节课的课堂表现，语气亲切自然\n"
                "3. 基于选定数据给出具体、有针对性的评价，先肯定优点再给改进建议\n"
                "4. 不要使用「XX同学」这类称呼，正文中用「孩子」或学生姓名指代\n"
                "5. 如有历史评价，请参考并体现学生的进步与变化\n"
            )
        if has_template:
            template_rule = (
                "6. 若学生带有「讲评模板」，评价中必须围绕该模板的讲解思路展开："
                "点出错因、结合正确答案说明该知识点，并给出鼓励；没有讲评模板的错题只需简要提及\n"
            )
        privacy_rule = (
            "7. 隐私保护：数据中未给出正确率/得分的学生，说明其正确率偏低——评价中严禁出现"
            "任何具体的正确率、得分、答错数量或排名数字，成绩部分用「还有很大的进步空间，"
            "继续加油」这类鼓励性表述自然带过\n"
        )
        opening = (
            "请参照「参考评价模板」的结构，结合每位学生的课堂表现数据，为每个学生生成一条评价消息。"
            if custom_style
            else "你是一位经验丰富的教师，请根据以下学生的课堂表现数据，为每个学生写一段简短的评价。"
        )
    head = f"{opening}\n\n"
    if field_labels:
        head += f"评价依据字段：{field_labels}\n\n"
    return (
        f"{head}"
        f"学生数据：\n{students_text}\n\n"
        f"{style_hint}"
        f"{requirements}"
        f"{template_rule}"
        f"{privacy_rule}\n"
        f"请严格按以下 JSON 数据结构输出，students 数组必须包含上面列出的每一位学生：\n"
        f'{{"students": [{{"name": "学生姓名", "evaluation": "该学生的评价内容"}}, ...]}}\n'
        f"其中 name 必须与上面给出的学生姓名完全一致（不要加序号、引号或「同学」后缀）；\n"
        f"evaluation 中的换行必须保留（JSON 字符串里换行写作 \\n），段落结构与模板一致；\n"
        f"只输出 JSON，不要输出任何其它文字，不要用 markdown 代码块包裹。\n"
    )


def generate_evaluations(summary, fields, api_key, previous_evals=None, custom_style="", achievement=None):
    """调用 DeepSeek API 为每个学生生成评价。

    参数：
        summary: aggregate_for_class 返回的汇总列表
        fields:  选定的依据字段列表，如 ["课节数", "总正确数"]
        api_key: DeepSeek API key
        previous_evals: {学生姓名: [历史评价, ...]}，供 LLM 综合参考
        custom_style: 自定义评价语，作为风格模板

    返回 (evaluations, error)：
        成功 → ([{"student_name": ..., "evaluation": ...}, ...], None)
        失败 → ([], "错误信息")
    """
    if not api_key:
        return [], "未配置 API key，请先在设置中填写"
    if not fields:
        return [], "请至少选择一个依据字段"

    # 只为有课时记录的学生生成评价
    students = [s for s in summary if s.get("有记录")]
    if not students:
        return [], "该班级没有课时记录，无法生成评价"

    base_url = getattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com") if config else "https://api.deepseek.com"
    model = datastore.get_model_name()

    # 分批请求：每条评价的输出长度不可控（模板模式下 ≈ 模板长度），
    # 按模板长度估算单批人数，保证单次输出不超过 max_tokens 上限而截断。
    # 实测 deepseek-v4 系接受 32768+ 输出；预算留足后常规班级单批即可完成。
    out_per_student = (len(custom_style) + 200) if custom_style else 250
    batch_size = max(1, min(len(students), 30000 // out_per_student))

    def _call(chunk_prompt, use_json_mode):
        """请求一次 API。use_json_mode 控制 response_format，失败可降级重试。"""
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一位专业的、善于鼓励学生的教师。只输出 JSON。"},
                {"role": "user", "content": chunk_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 32768,
        }
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}  # DeepSeek 的 JSON 输出模式
        return requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(15, 600),  # 连接 15s；读 600s——模板模式全班输出上万 token，实际需 2-5 分钟
        )

    all_evaluations = []
    for i in range(0, len(students), batch_size):
        chunk = students[i:i + batch_size]
        chunk_prompt = _build_prompt(chunk, fields, previous_evals, custom_style, achievement)
        try:
            resp = _call(chunk_prompt, use_json_mode=True)
            # 个别模型/网关不支持 response_format 时降级为普通模式重试一次
            if resp.status_code == 400 and "response_format" in resp.text:
                resp = _call(chunk_prompt, use_json_mode=False)
        except requests.exceptions.Timeout:
            return [], "生成超时：本次请求超过 10 分钟未返回，请重试一次；若反复超时，可缩短模板或减少一次生成的学生数"
        except requests.exceptions.RequestException as e:
            return [], f"网络请求失败：{e}"

        if resp.status_code != 200:
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                err_msg = resp.text
            return [], f"API 返回错误({resp.status_code})：{err_msg}"

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return [], "API 返回格式异常，无法解析评价"

        evaluations = _parse_evaluations(content, chunk)
        if not evaluations:
            return [], "未能从返回内容中解析出学生评价，请重试"
        all_evaluations.extend(evaluations)
    return all_evaluations, None


def list_models():
    """调用 /models 端点获取当前 API key 可用的模型列表。

    DeepSeek 兼容 OpenAI 的 GET {base_url}/models，返回
    {"object": "list", "data": [{"id": "deepseek-chat", ...}, ...]}
    返回 (model_id 列表, 错误信息)。
    """
    api_key = datastore.get_api_key()
    if not api_key:
        return [], "未配置 API key"
    base_url = getattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com") if config else "https://api.deepseek.com"
    try:
        resp = requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return [], f"网络请求失败：{e}"
    if resp.status_code != 200:
        return [], f"获取模型列表失败({resp.status_code})"
    try:
        data = resp.json().get("data", [])
        return sorted(m["id"] for m in data if m.get("id")), None
    except (ValueError, KeyError, TypeError):
        return [], "模型列表返回格式异常"


def _extract_json(content):
    """从模型返回文本中尽力提取并解析 JSON 对象，失败返回 None。

    依次尝试：整段直解 → markdown 代码块 → 第一个配平的 {...} 片段。
    """
    content = content.strip()
    # 1) 返回内容本身就是纯 JSON
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        pass
    # 2) ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            pass
    # 3) 扫描第一个花括号配平的 {...} 片段（容忍模型在前后混入说明文字）
    start = content.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(content[start:i + 1])
                    except (ValueError, TypeError):
                        break  # 该片段不是合法 JSON，从下一个 { 继续
        start = content.find("{", start + 1)
    return None


def _norm_name(name):
    """姓名归一化：去空白、各类引号括号与「同学」后缀，便于匹配名单。"""
    s = re.sub(r"[\s「」『』'\'\"“”，,。.]", "", str(name))
    return s[:-2] if s.endswith("同学") else s


def _parse_evaluations(content, students):
    """解析模型按 JSON 数据结构返回的评价，匹配到班级名单后输出。

    兼容形态：
        {"students": [{"name": "张三", "evaluation": "..."}]}   （约定结构）
        {"students": {"张三": "..."}}                            （模型简写成映射）
    返回 [{"student_name": 名单姓名, "evaluation": 文本}, ...]
    """
    data = _extract_json(content)
    if not isinstance(data, dict):
        return []
    items = data.get("students")
    if isinstance(items, dict):  # 映射形态 → 转成列表
        items = [{"name": k, "evaluation": v} for k, v in items.items()]
    if not isinstance(items, list):
        return []

    # 名单姓名按归一化形式建索引：归一化名 → 名单原始姓名
    by_norm = {}
    for s in students:
        by_norm.setdefault(_norm_name(s["学生姓名"]), s["学生姓名"])

    evaluations, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        canonical = by_norm.get(_norm_name(it.get("name", "")))
        text = str(it.get("evaluation", "")).strip()
        if canonical and text and canonical not in seen:  # 每个学生只取第一条
            evaluations.append({"student_name": canonical, "evaluation": text})
            seen.add(canonical)
    return evaluations
