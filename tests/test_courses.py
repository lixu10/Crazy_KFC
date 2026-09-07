"""搜索语义回归：课程名/代码/教师“包含”模糊匹配，留空浏览全部。"""
import unittest

from kfc_college.courses import search_rows


def row(kcm, kch="B000000000", skjs="", jxbid=""):
    return {
        "JXBID": jxbid or ("JXBID-" + kch),
        "KCM": kcm, "KCH": kch, "SKJS": skjs,
        "SKSJ": [{"KCM": kcm, "KCH": kch, "SKJS": skjs}],
    }


ROWS = [
    row("数据结构与算法", "B060031011", "张老师"),
    row("编译原理", "B060031012", "李四"),
    row("高等数学(下)", "B2B011010", "王五"),
    row("大学英语（一）", "A030101001", "赵老师"),
]


class CourseSearchTests(unittest.TestCase):
    def test_empty_query_returns_all_rows_for_browse(self):
        out = search_rows(ROWS, "")
        self.assertEqual([r["KCH"] for r in out],
                         ["B060031011", "B060031012", "B2B011010", "A030101001"])

    def test_course_name_fragment_matches(self):
        out = search_rows(ROWS, "数据")
        self.assertEqual([r["KCH"] for r in out], ["B060031011"])
        out = search_rows(ROWS, "结构")
        self.assertEqual([r["KCH"] for r in out], ["B060031011"])

    def test_full_name_still_matches(self):
        out = search_rows(ROWS, "编译原理")
        self.assertEqual([r["KCH"] for r in out], ["B060031012"])

    def test_course_code_fragment_matches(self):
        out = search_rows(ROWS, "0600")
        self.assertEqual(len(out), 2)
        out = search_rows(ROWS, "B2B011")
        self.assertEqual([r["KCH"] for r in out], ["B2B011010"])

    def test_teacher_fragment_matches(self):
        out = search_rows(ROWS, "张")
        self.assertEqual([r["KCH"] for r in out], ["B060031011"])
        out = search_rows(ROWS, "赵老师")
        self.assertEqual([r["KCH"] for r in out], ["A030101001"])

    def test_whitespace_and_case_are_ignored(self):
        self.assertEqual(len(search_rows(ROWS, "  数据  ")), 1)
        self.assertEqual(len(search_rows(ROWS, "b2b011")), 1)
        self.assertEqual(len(search_rows(ROWS, "b060031011")), 1)

    def test_row_without_schedule_is_excluded_even_for_browse(self):
        extra = [row("有课表课程", jxbid="J1"), {"KCM": "缺SKSJ"}]
        self.assertEqual(len(search_rows(ROWS + extra, "")), 5)

    def test_no_match_returns_empty(self):
        self.assertEqual(search_rows(ROWS, "量子物理"), [])


if __name__ == "__main__":
    unittest.main()
