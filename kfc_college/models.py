"""领域模型与共享枚举。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---- 课程类型 ----

@dataclass(frozen=True)
class CourseTypeRef:
    id: int
    code: str
    name: str


COURSE_TYPES = [
    CourseTypeRef(0, "TJKC", "班级推荐课程"),
    CourseTypeRef(1, "FANKC", "方案内课程"),
    CourseTypeRef(2, "FAWKC", "方案外课程"),
    CourseTypeRef(3, "CXKC", "重修课程"),
    CourseTypeRef(4, "YYKC", "英语课"),
    CourseTypeRef(5, "TYKC", "体育课"),
    CourseTypeRef(6, "XGKC", "通识选修课"),
    CourseTypeRef(7, "KYKT", "科研课堂"),
]
CODE_TO_TYPE = {t.code: t for t in COURSE_TYPES}
ID_TO_TYPE = {t.id: t for t in COURSE_TYPES}


def type_by_code(code: str) -> Optional[CourseTypeRef]:
    return CODE_TO_TYPE.get(code)


# ---- 课程目标 / 已选课程 ----

@dataclass
class CourseTarget:
    """一个待处理的目标教学班（前端安全，不含 secretVal）。"""
    class_type: str          # 后端类型代码，如 TJKC
    jxbid: str
    name: str = ""
    kxh: str = ""
    teacher: str = ""
    place: str = ""
    weeks: str = ""
    schedule: str = ""

    @property
    def key(self) -> str:
        return f"{self.class_type}:{self.jxbid}"

    def display(self) -> str:
        return f"{self.name}({self.kxh})" if self.kxh else self.name

    def to_dict(self) -> dict:
        return {
            "class_type": self.class_type,
            "jxbid": self.jxbid,
            "name": self.name,
            "kxh": self.kxh,
            "teacher": self.teacher,
            "place": self.place,
            "weeks": self.weeks,
            "schedule": self.schedule,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CourseTarget":
        return cls(
            class_type=str(d.get("class_type", "")),
            jxbid=str(d.get("jxbid", "")),
            name=str(d.get("name", "")),
            kxh=str(d.get("kxh", "")),
            teacher=str(d.get("teacher", "")),
            place=str(d.get("place", "")),
            weeks=str(d.get("weeks", "")),
            schedule=str(d.get("schedule", "")),
        )


@dataclass
class OldCourse:
    """改选时要退掉的已选课程（含回退所需的敏感字段，仅服务端使用）。"""
    jxbid: str
    name: str = ""
    kxh: str = ""
    class_type: str = ""       # teachingClassType
    secret_val: str = ""

    @property
    def key(self) -> str:
        return f"{self.class_type}:{self.jxbid}"

    def display(self) -> str:
        return f"{self.name}({self.kxh})" if self.kxh else self.name

    def to_public_dict(self) -> dict:
        return {
            "jxbid": self.jxbid,
            "name": self.name,
            "kxh": self.kxh,
            "class_type": self.class_type,
        }


@dataclass
class SwapPair:
    old: OldCourse
    target: CourseTarget


# ---- 任务 ----

class TaskMode:
    GRAB = "grab"
    POLL = "poll"
    SWAP = "swap"


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    WAITING_LOGIN = "waiting_login"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    STOPPED = "stopped"
    FAILED = "failed"
    MANUAL_ATTENTION = "manual_attention"


@dataclass
class TargetState:
    """任务运行中对单个目标/改选对的实时状态（前端可读）。"""
    key: str
    label: str = ""
    status: str = "watching"       # watching / ok / failed / manual / done
    detail: str = ""
    capacity: Optional[int] = None
    selected: Optional[int] = None
    has_slot: Optional[bool] = None
    last_check: Optional[str] = None
    notified: bool = False


@dataclass
class TaskEvent:
    seq: int
    ts: str
    level: str        # info / warn / success / error
    kind: str         # 事件类型
    message: str = ""

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "level": self.level,
                "kind": self.kind, "message": self.message}


@dataclass
class TaskRecord:
    id: int
    mode: str
    status: str = TaskStatus.PENDING
    created_ts: str = ""
    started_ts: str = ""
    ended_ts: str = ""
    stage: str = ""
    stop_requested: bool = False
    batch_id: str = ""                              # 启动时固定的批次，防止运行中被切换
    targets: list = field(default_factory=list)      # list[CourseTarget]（swap 时为各对 target 的扁平展示）
    swap_pairs: list = field(default_factory=list)   # list[SwapPair]
    states: dict = field(default_factory=dict)       # key -> TargetState
    last_check: Optional[str] = None
    summary: str = ""
    error: str = ""
