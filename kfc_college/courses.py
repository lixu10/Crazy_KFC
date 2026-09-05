"""课程数据的规范化、容量判断与搜索。

面向前端的规范化结果一律不包含 secretVal、token 或 cookie 等敏感字段。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .models import OldCourse, CourseTarget, CODE_TO_TYPE, type_by_code

# 内部 / 外部容量字段名
_INTERNAL_CAP = "internalCapacity"
_INTERNAL_SEL = "internalSelectedNum"
_EXTERNAL_CAP = "externalCapacity"
_EXTERNAL_SEL = "externalSelectedNum"


def _norm_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def capacity_of(row: dict, student_class: str) -> Tuple[int, int]:
    """根据学生班级是否在 TJBJ 中决定使用内部或外部容量，返回 (capacity, selected)。"""
    tjbj = [x.strip() for x in str(row.get("TJBJ", "")).replace("，", ",").split(",") if x.strip()]
    if student_class and student_class in tjbj:
        return _norm_int(row.get(_INTERNAL_CAP)), _norm_int(row.get(_INTERNAL_SEL))
    return _norm_int(row.get(_EXTERNAL_CAP)), _norm_int(row.get(_EXTERNAL_SEL))


def _sksj(row: dict) -> dict:
    arr = row.get("SKSJ")
    if isinstance(arr, list) and arr and isinstance(arr[0], dict):
        return arr[0]
    return {}


def matches_query(row: dict, query: str) -> bool:
    """保持原有语义：课程完整名称或课程代码精确匹配。"""
    if not query:
        return False
    q = query.strip()
    s = _sksj(row)
    if s.get("KCM") == q or s.get("KCH") == q:
        return True
    # 兼容字段直接位于行顶层的情况
    return row.get("KCM") == q or row.get("KCH") == q


def normalize_section(row: dict, class_type_code: str, student_class: str) -> dict:
    """把学校接口的一行转成前端可读、安全的课程信息。"""
    s = _sksj(row)
    cap, sel = capacity_of(row, student_class)
    return {
        "class_type": class_type_code,
        "type_name": type_by_code(class_type_code).name if type_by_code(class_type_code) else class_type_code,
        "jxbid": row.get("JXBID", ""),
        "name": s.get("KCM") or row.get("KCM") or "",
        "code": s.get("KCH") or row.get("KCH") or "",
        "kxh": s.get("KXH") or row.get("KXH") or "",
        "teacher": s.get("SKJS") or "",
        "place": s.get("YPSJDD") or "",
        "weeks": s.get("SKZCMC") or "",
        "schedule": s.get("SKSJMS") or s.get("JASMC") or "",
        "capacity": cap,
        "selected": sel,
        "has_slot": cap > sel,
        # 内外容量口径同时给出，便于界面展示“为何这样判定”
        "internal": {"capacity": _norm_int(row.get(_INTERNAL_CAP)),
                     "selected": _norm_int(row.get(_INTERNAL_SEL))},
        "external": {"capacity": _norm_int(row.get(_EXTERNAL_CAP)),
                     "selected": _norm_int(row.get(_EXTERNAL_SEL))},
        "tjbj": str(row.get("TJBJ", "")),
    }


def search_rows(rows: List[dict], query: str) -> List[dict]:
    """返回 rows 中与 query 精确匹配的教学班行。"""
    return [r for r in rows if isinstance(r, dict) and r.get("SKSJ") and matches_query(r, query)]


def to_target(sec: dict) -> CourseTarget:
    return CourseTarget(
        class_type=sec.get("class_type", ""),
        jxbid=sec.get("jxbid", ""),
        name=sec.get("name", ""),
        kxh=sec.get("kxh", ""),
        teacher=sec.get("teacher", ""),
        place=sec.get("place", ""),
        weeks=sec.get("weeks", ""),
        schedule=sec.get("schedule", ""),
    )


def old_from_selected_row(row: dict) -> Optional[OldCourse]:
    """从 /xsxk/elective/select 返回的行中构建 OldCourse（含回退所需字段）。"""
    jxbid = row.get("JXBID") or row.get("jxbid")
    if not jxbid:
        return None
    return OldCourse(
        jxbid=str(jxbid),
        name=str(row.get("KCM") or ""),
        kxh=str(row.get("KXH") or ""),
        class_type=str(row.get("teachingClassType") or row.get("clazzType") or ""),
        secret_val=str(row.get("secretVal") or ""),
    )


def selected_public(row: dict) -> dict:
    """已选课程行的前端安全表示。"""
    jxbid = row.get("JXBID") or row.get("jxbid") or ""
    return {
        "jxbid": str(jxbid),
        "name": str(row.get("KCM") or ""),
        "kxh": str(row.get("KXH") or ""),
        "class_type": str(row.get("teachingClassType") or row.get("clazzType") or ""),
    }
