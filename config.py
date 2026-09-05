# -*- coding: utf-8 -*-
"""全局配置：DeepSeek LLM 接口参数。

API key 的优先级：数据库（网页设置）> 此处 DEEPSEEK_API_KEY。
也就是说，用户既可以在网页「设置」中填写 key（保存到数据库），
也可以直接在下面填写 DEEPSEEK_API_KEY（适合不想每次都手动输入的场景）。
"""

# DeepSeek API 地址（兼容 OpenAI 接口格式）
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 使用的模型名
DEEPSEEK_MODEL = "deepseek-chat"

# 可在此处填写 API key（也可在网页设置中填写，网页优先）
DEEPSEEK_API_KEY = ""

# 汇总字段的中文名映射，供评价面板复选框与 prompt 使用
SUMMARY_FIELDS = [
    {"key": "课节数",       "label": "课节数"},
    {"key": "总正确数",     "label": "总正确数"},
    {"key": "平均得分率",   "label": "平均得分率"},
    {"key": "总奖杯数",     "label": "总奖杯数"},
    {"key": "总出勤奖杯数", "label": "总出勤奖杯数"},
]
