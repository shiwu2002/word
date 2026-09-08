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


def _format_value(field, val):
    """格式化单个字段值用于 prompt 展示。"""
    if field == "平均得分率":
        return f"{val * 100:.1f}%"
    return str(val)


def _achievement_lines(ach):
    """把单个学生的课测逐题数据格式化成 prompt 行（无数据返回空串）。"""
    if not ach:
        return ""
    lines = [
        f"   本次课测（{_short_lesson(ach['lesson_name'])}，{ach['lesson_time'][:10]}）："
        f"得分率 {ach['score_rate'] * 100:.0f}%（正确 {ach['correct_count']} 题）"
    ]
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


def _build_prompt(summary, fields, previous_evals=None, custom_style="", achievement=None):
    """构建发送给 LLM 的 prompt。

    summary: 有记录的学生汇总列表
    fields: 选定的依据字段列表
    previous_evals: {学生姓名: [历史评价文本, ...]}，用于综合参考
    custom_style: 自定义评价语模板，LLM 据此生成风格一致的评价
    achievement: {学生姓名: {课测逐题数据}}，含答错题目与用户配置的讲评模板
    """
    field_labels = "、".join(fields)
    has_template = any(
        w.get("reply_template") for a in (achievement or {}).values() for w in a.get("wrongs", [])
    )
    lines = []
    for i, s in enumerate(summary, 1):
        data_parts = [f"{f}={_format_value(f, s.get(f, 0))}" for f in fields]
        line = f"{i}. {s['学生姓名']} — {'，'.join(data_parts)}"
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
            f"\n参考评价风格：\n{custom_style}\n\n"
            f"请模仿以上评价的语气、句式、措辞风格来生成每位学生的评价，但数据部分要根据每位学生的实际情况填写。\n\n"
        )
    template_rule = ""
    if has_template:
        template_rule = (
            "6. 若学生带有「讲评模板」，评价中必须围绕该模板的讲解思路展开："
            "点出错因、结合正确答案说明该知识点，并给出鼓励；没有讲评模板的错题只需简要提及\n"
        )
    return (
        f"你是一位经验丰富的教师，请根据以下学生的课堂表现数据，为每个学生写一段简短的评价。\n\n"
        f"评价依据字段：{field_labels}\n\n"
        f"学生数据：\n{students_text}\n\n"
        f"{style_hint}"
        f"要求：\n"
        f"1. 每个学生一段评价，50-100字\n"
        f"2. 基于选定字段的数据给出具体、有针对性的评价\n"
        f"3. 语气鼓励、积极向上，先肯定优点再给改进建议\n"
        f"4. 评价中必须用「XX同学」称呼学生，如「昊萌同学」，不要只写姓名\n"
        f"5. 如有历史评价，请参考并体现学生的进步与变化\n"
        f"{template_rule}\n"
        f"请严格按以下 JSON 数据结构输出，students 数组必须包含上面列出的每一位学生：\n"
        f'{{"students": [{{"name": "学生姓名", "evaluation": "该学生的评价内容"}}, ...]}}\n'
        f"其中 name 必须与上面给出的学生姓名完全一致（不要加序号、引号或「同学」后缀）；\n"
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

    prompt = _build_prompt(students, fields, previous_evals, custom_style, achievement)

    def _call(use_json_mode):
        """请求一次 API。use_json_mode 控制 response_format，失败可降级重试。"""
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一位专业的、善于鼓励学生的教师。只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
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
            timeout=120,
        )

    try:
        resp = _call(use_json_mode=True)
        # 个别模型/网关不支持 response_format 时降级为普通模式重试一次
        if resp.status_code == 400 and "response_format" in resp.text:
            resp = _call(use_json_mode=False)
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

    evaluations = _parse_evaluations(content, students)
    if not evaluations:
        return [], "未能从返回内容中解析出学生评价，请重试"
    return evaluations, None


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
