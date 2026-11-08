"""考试复习提醒：按考试名称匹配学科，给出简短、可执行的复习建议。"""
from __future__ import annotations

from .models import Exam

# (关键词, 建议)，关键词按出现顺序匹配，最多合并两条
_SUBJECT_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("高等数学", "高数", "微积分", "数学分析"),
     "先梳理极限、导数、积分的核心公式，再刷两套往年题，重点复盘错题。"),
    (("线性代数", "线代"),
     "熟记矩阵运算与特征值结论，动手推一遍典型证明题。"),
    (("概率论", "数理统计"),
     "整理常见分布与期望方差公式，重做课后计算题。"),
    (("大学物理", "物理"),
     "按章节梳理公式适用条件，重做典型计算与实验题。"),
    (("电路", "电工", "电子"),
     "复习电路分析方法与常用定理，重做实验报告里的计算。"),
    (("化学",),
     "背熟反应方程式与实验现象，回看章节错题。"),
    (("英语",),
     "每天保持听力和阅读，背高频词，准备一套作文模板。"),
    (("数据结构", "算法", "程序设计", "c语言", "python", "java", "编程", "计算机"),
     "手写核心代码与伪代码，复习时间/空间复杂度，把错题重敲一遍。"),
    (("数据库", "sql"),
     "复习建表、查询与事务要点，动手写几段 SQL 并检查执行计划。"),
    (("操作系统", "计算机网络", "组成原理", "编译原理"),
     "按章节梳理核心概念和常考大题，配合历年题查漏补缺。"),
    (("马克思", "马原", "毛概", "毛泽东思想", "思修", "近代史", "史纲", "政治"),
     "按章节梳理时间线与核心概念，配合选择题题库过一遍。"),
    (("管理", "经济", "会计", "金融"),
     "整理概念框架和计算题套路，通读案例与课后题。"),
    (("机械", "工程", "制图", "力学"),
     "回看公式与图纸规范，重做典型计算题。"),
]

_DEFAULT = "按章节回看笔记，列出薄弱知识点，优先攻克老师强调的高频考点。"


def advice_for(name: str) -> str:
    """按考试名称给 1~2 条复习建议；匹配不到时给通用建议。"""
    raw = str(name or "").strip()
    low = raw.lower()
    hints: list[str] = []
    for keys, hint in _SUBJECT_HINTS:
        if any(k.lower() in low for k in keys) and hint not in hints:
            hints.append(hint)
        if len(hints) >= 2:
            break
    return " ".join(hints) if hints else _DEFAULT


def review_message(exam: Exam, days_left: int) -> str:
    """考试复习提醒正文。"""
    lines = [exam.name or "考试", exam.display_time()]
    place = " ".join(p for p in (
        f"@{exam.room}" if exam.room else "",
        f"座位 {exam.seat}" if exam.seat else "",
        exam.campus,
    ) if p)
    if place:
        lines.append(place)
    lines.append(f"距考试还有 {days_left} 天")
    lines.append("复习建议：" + advice_for(exam.name))
    if days_left <= 3:
        lines.append("时间不多，优先看老师划的重点和自己的错题。")
    return "\n".join(line for line in lines if line)
