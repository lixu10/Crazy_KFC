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


def _frag(value) -> str:
    """规整用于搜索的文本：去首尾及内部空白、转小写。"""
    return "".join(str(value or "").split()).lower()


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
    """在教学班行中做“包含”模糊匹配：课程名/课程代码/教师命中任一即算。

    留空 query 时返回全部行，供“浏览该类型全部课程”使用。逐字规整空白与大写，
    因此“数据”、“数据结构与算法”、课程代码片段、教师姓名片段都可命中。
    """
    q = _frag(query)
    out: List[dict] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("SKSJ"):
            continue
        if not q:
            out.append(r)
            continue
        s = _sksj(r)
        hay = _frag(" ".join((
            str(s.get("KCM") or r.get("KCM") or ""),
            str(s.get("KCH") or r.get("KCH") or ""),
            str(s.get("SKJS") or r.get("SKJS") or ""),
        )))
        if q in hay:
            out.append(r)
    return out


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


def selected_public_full(row: dict) -> dict:
    """“已选课程详情”的降级视图：当列表里找不到该教学班时使用。

    字段与 normalize_section 对齐（教师/时间/地点/容量等置空），保证前端
    只用一种结构渲染；容量类字段为 None 表示“未知口径”，界面显示 —。
    """
    p = selected_public(row)
    code = p["class_type"]
    ref = CODE_TO_TYPE.get(code)
    p.update({
        "type_name": ref.name if ref else (code or "未知类型"),
        "code": str(row.get("KCH") or ""),
        "teacher": "",
        "place": "",
        "weeks": "",
        "schedule": "",
        "capacity": None,
        "selected": None,
        "has_slot": None,
        "internal": {"capacity": None, "selected": None},
        "external": {"capacity": None, "selected": None},
        "tjbj": "",
    })
    return p
