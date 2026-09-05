# -*- coding: utf-8 -*-
"""LLM 评价生成层：调用 DeepSeek API 为每个学生生成评价。

DeepSeek 兼容 OpenAI 接口格式：
    POST {base_url}/chat/completions
    Header: Authorization: Bearer {api_key}

调用流程：
    generate_evaluations(summary, fields, api_key) → (evaluations, error)
"""
import re

import requests

try:
    import config
except ImportError:
    config = None


def _format_value(field, val):
    """格式化单个字段值用于 prompt 展示。"""
    if field == "平均得分率":
        return f"{val * 100:.1f}%"
    return str(val)


def _build_prompt(summary, fields, previous_evals=None, custom_style=""):
    """构建发送给 LLM 的 prompt。

    summary: 有记录的学生汇总列表
    fields: 选定的依据字段列表
    previous_evals: {学生姓名: [历史评价文本, ...]}，用于综合参考
    custom_style: 自定义评价语模板，LLM 据此生成风格一致的评价
    """
    field_labels = "、".join(fields)
    lines = []
    for i, s in enumerate(summary, 1):
        data_parts = [f"{f}={_format_value(f, s.get(f, 0))}" for f in fields]
        line = f"{i}. {s['学生姓名']} — {'，'.join(data_parts)}"
        # 若有历史评价，附在该学生数据下方供 LLM 参考
        if previous_evals and s["学生姓名"] in previous_evals:
            evals = previous_evals[s["学生姓名"]]
            line += f"\n   历史评价：{' ｜ '.join(evals)}"
        lines.append(line)

    students_text = "\n".join(lines)
    style_hint = ""
    if custom_style:
        style_hint = (
            f"\n参考评价风格：\n{custom_style}\n\n"
            f"请模仿以上评价的语气、句式、措辞风格来生成每位学生的评价，但数据部分要根据每位学生的实际情况填写。\n\n"
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
        f"5. 如有历史评价，请参考并体现学生的进步与变化\n\n"
        f"请严格按以下格式输出（每行一个学生，姓名后跟中文冒号和评价）：\n"
        f"学生姓名：评价内容\n"
    )


def generate_evaluations(summary, fields, api_key, previous_evals=None, custom_style=""):
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
    model = getattr(config, "DEEPSEEK_MODEL", "deepseek-chat") if config else "deepseek-chat"

    prompt = _build_prompt(students, fields, previous_evals, custom_style)

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一位专业的、善于鼓励学生的教师。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 4096,
            },
            timeout=60,
        )
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


def _parse_evaluations(content, students):
    """从 LLM 返回的文本中解析每个学生的评价。

    支持格式：姓名：评价 或 姓名: 评价
    自动去除行首序号前缀（如 "1. 张三"）。
    """
    student_names = {s["学生姓名"] for s in students}
    evaluations = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        for sep in ("：", ":"):
            if sep in line:
                name, _, eval_text = line.partition(sep)
                name = re.sub(r"^\d+\.\s*", "", name.strip())  # 去序号前缀
                eval_text = eval_text.strip()
                if name in student_names and eval_text:
                    evaluations.append({"student_name": name, "evaluation": eval_text})
                break
    return evaluations
