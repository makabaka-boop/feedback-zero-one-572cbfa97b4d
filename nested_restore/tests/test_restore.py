"""
test_restore.py — 嵌套列还原作业完整验收。

覆盖：
  1. 独立正向编码 -> 反向还原完整对拍（两层列表 / 可选 struct / 空列表 /
     缺失叶 / 零值 / 空字符串 / 零行）。
  2. 逐列令牌的 r/d 序列人工断言。
  3. 篡改共享祖先（列表长度、null 存在性、两层列表元素边界）验证拒绝，
     且错误信息带列路径与令牌下标。
  4. 首 r 非零、行数不符、越级 r/d、r>d、类型错误、schema 非法、
     多余/缺失列、占位 v 非 null、行内 r=0、多余令牌。
  5. 单叶 schema 合法往返。
  6. CLI 成功原子替换、失败保留旧结果。
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from encoder import EncodeError, encode  # noqa: E402
from nested_restore import RestoreError, restore  # noqa: E402

CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py")


def build_schema():
    return {
        "name": "root",
        "mode": "required",
        "type": "struct",
        "children": [
            {"name": "id", "mode": "required", "type": "int"},
            {"name": "name", "mode": "optional", "type": "string"},
            {"name": "tags", "mode": "repeated", "type": "string"},
            {
                "name": "address",
                "mode": "optional",
                "type": "struct",
                "children": [
                    {"name": "city", "mode": "optional", "type": "string"},
                    {"name": "zip", "mode": "required", "type": "int"},
                ],
            },
            {
                "name": "groups",
                "mode": "repeated",
                "type": "struct",
                "children": [
                    {"name": "gname", "mode": "required", "type": "string"},
                    {"name": "score", "mode": "optional", "type": "int"},
                ],
            },
            {
                "name": "matrix",
                "mode": "repeated",
                "type": "struct",
                "children": [
                    {"name": "vals", "mode": "repeated", "type": "int"},
                    {"name": "kind", "mode": "optional", "type": "string"},
                ],
            },
            {"name": "code", "mode": "optional", "type": "int"},
        ],
    }


def build_records():
    return [
        {
            "id": 1,
            "name": "ada",
            "tags": ["a", "b"],
            "address": {"city": "NYC", "zip": 10001},
            "groups": [
                {"gname": "g1", "score": 10},
                {"gname": "g2", "score": None},
            ],
            "matrix": [
                {"vals": [1, 2], "kind": "m1"},
                {"vals": [3], "kind": "m2"},
            ],
            "code": 7,
        },
        {
            "id": 2,
            "name": None,
            "tags": [],
            "address": None,
            "groups": [],
            "matrix": [],
            "code": None,
        },
        {
            "id": 0,                 # 整数零值
            "name": "",              # 空字符串
            "tags": ["x"],
            "address": {"city": None, "zip": 2},
            "groups": [{"gname": "only", "score": 5}],
            "matrix": [{"vals": [], "kind": None}],
            "code": 9,
        },
    ]


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.schema = build_schema()
        self.records = build_records()
        self.payload = encode(self.schema, self.records)

    def test_full_round_trip(self):
        self.assertEqual(restore(self.payload), self.records)

    def test_expected_token_sequences(self):
        cols = self.payload["columns"]
        self.assertEqual(cols["id"], [
            {"r": 0, "d": 0, "v": 1},
            {"r": 0, "d": 0, "v": 2},
            {"r": 0, "d": 0, "v": 0},
        ])
        self.assertEqual(cols["name"], [
            {"r": 0, "d": 1, "v": "ada"},
            {"r": 0, "d": 0, "v": None},
            {"r": 0, "d": 1, "v": ""},
        ])
        self.assertEqual(cols["tags"], [
            {"r": 0, "d": 1, "v": "a"},
            {"r": 1, "d": 1, "v": "b"},
            {"r": 0, "d": 0, "v": None},   # 空列表
            {"r": 0, "d": 1, "v": "x"},
        ])
        self.assertEqual(cols["address.city"], [
            {"r": 0, "d": 2, "v": "NYC"},
            {"r": 0, "d": 0, "v": None},   # 整个 address 缺失
            {"r": 0, "d": 1, "v": None},   # city 自身缺失
        ])
        self.assertEqual(cols["address.zip"], [
            {"r": 0, "d": 1, "v": 10001},
            {"r": 0, "d": 0, "v": None},
            {"r": 0, "d": 1, "v": 2},
        ])
        self.assertEqual(cols["groups.gname"], [
            {"r": 0, "d": 1, "v": "g1"},
            {"r": 1, "d": 1, "v": "g2"},
            {"r": 0, "d": 0, "v": None},   # groups 空列表
            {"r": 0, "d": 1, "v": "only"},
        ])
        self.assertEqual(cols["groups.score"], [
            {"r": 0, "d": 2, "v": 10},
            {"r": 1, "d": 1, "v": None},   # 元素内 optional 叶缺失
            {"r": 0, "d": 0, "v": None},
            {"r": 0, "d": 2, "v": 5},
        ])
        # 两层列表：内层切换 r=2，外层切换 r=1
        self.assertEqual(cols["matrix.vals"], [
            {"r": 0, "d": 2, "v": 1},
            {"r": 2, "d": 2, "v": 2},
            {"r": 1, "d": 2, "v": 3},
            {"r": 0, "d": 0, "v": None},   # 外层空列表
            {"r": 0, "d": 1, "v": None},   # 内层空列表
        ])
        self.assertEqual(cols["matrix.kind"], [
            {"r": 0, "d": 2, "v": "m1"},
            {"r": 1, "d": 2, "v": "m2"},
            {"r": 0, "d": 0, "v": None},
            {"r": 0, "d": 1, "v": None},
        ])
        self.assertEqual(cols["code"], [
            {"r": 0, "d": 1, "v": 7},
            {"r": 0, "d": 0, "v": None},
            {"r": 0, "d": 1, "v": 9},
        ])

    def test_zero_rows(self):
        payload = encode(self.schema, [])
        self.assertEqual(payload["row_count"], 0)
        self.assertTrue(all(toks == [] for toks in payload["columns"].values()))
        self.assertEqual(restore(payload), [])

    def test_zero_rows_but_nonempty_column_rejected(self):
        payload = encode(self.schema, [])
        payload["columns"]["id"].append({"r": 0, "d": 0, "v": 1})
        with self.assertRaisesRegex(RestoreError, "row_count=0"):
            restore(payload)

    def test_single_leaf_schema(self):
        schema = {
            "name": "root", "mode": "required", "type": "struct",
            "children": [{"name": "v", "mode": "optional", "type": "int"}],
        }
        records = [{"v": 1}, {"v": None}, {"v": -3}]
        payload = encode(schema, records)
        self.assertEqual(restore(payload), records)


class TamperSharedAncestorTests(unittest.TestCase):
    def setUp(self):
        self.payload = encode(build_schema(), build_records())

    def test_tamper_list_length_shared_ancestor(self):
        # groups.gname 第 3 行（下标 2）原本是空列表占位，改成一个元素；
        # groups.score 仍为空列表 -> 合并时长度 1 != 0，必须拒绝。
        cols = self.payload["columns"]
        cols["groups.gname"][2] = {"r": 0, "d": 1, "v": "rogue"}
        with self.assertRaises(RestoreError) as ctx:
            restore(self.payload)
        msg = str(ctx.exception)
        self.assertIn("groups.gname", msg)
        self.assertIn("groups.score", msg)
        self.assertIn("令牌[2]", msg)

    def test_tamper_optional_existence_conflict(self):
        # address.city 把整行 null 改成有值，address.zip 仍是 null 占位。
        cols = self.payload["columns"]
        cols["address.city"][1] = {"r": 0, "d": 2, "v": "X"}
        with self.assertRaisesRegex(RestoreError, r"null 对非 null"):
            restore(self.payload)

    def test_tamper_two_level_list_boundary(self):
        # 把外层切换 r=1 改成内层切换 r=2：vals 列解出 1 个外层元素，
        # kind 列仍是 2 个 -> 共享 matrix 祖先长度矛盾。
        vals = self.payload["columns"]["matrix.vals"]
        self.assertEqual(vals[2], {"r": 1, "d": 2, "v": 3})
        vals[2]["r"] = 2
        with self.assertRaisesRegex(RestoreError, r"matrix.*(1 != 2|2 != 1)|(1 != 2|2 != 1)"):
            restore(self.payload)

    def test_tamper_does_not_mutate_on_failure(self):
        # restore 出错不返回任何部分结果；再次对未篡改副本还原仍成功。
        good = copy.deepcopy(self.payload)
        bad = copy.deepcopy(self.payload)
        bad["columns"]["tags"][3]["v"] = 123  # string 列塞 int
        with self.assertRaises(restore(bad) if False else RestoreError):
            restore(bad)
        self.assertEqual(restore(good), build_records())


class TokenLevelRejectTests(unittest.TestCase):
    def setUp(self):
        self.payload = encode(build_schema(), build_records())

    def reject(self, mutator, pattern=None):
        mutator(self.payload)
        if pattern is None:
            with self.assertRaises(RestoreError):
                restore(self.payload)
        else:
            with self.assertRaisesRegex(RestoreError, pattern):
                restore(self.payload)

    def test_first_r_nonzero(self):
        # id 列 max_rep=0：r=1 先触发 r>d 非法
        self.payload["columns"]["id"][0]["r"] = 1
        with self.assertRaisesRegex(RestoreError, r"首个令牌 r 必须为 0|r=1 大于 d=0"):
            restore(self.payload)
        # repeated 列 max_rep>=1：由行首检查拒绝
        self.setUp()
        self.payload["columns"]["tags"][0]["r"] = 1
        with self.assertRaisesRegex(RestoreError, "首个令牌 r 必须为 0"):
            restore(self.payload)

    def test_row_count_mismatch_declared(self):
        self.payload["row_count"] = 2
        with self.assertRaisesRegex(RestoreError, "行数不符"):
            restore(self.payload)

    def test_row_count_mismatch_column(self):
        # 行内插入 r=0 -> 该列 r=0 令牌数与 row_count 不符
        self.payload["columns"]["tags"].append(
            {"r": 0, "d": 1, "v": "tail"}
        )
        with self.assertRaisesRegex(RestoreError, "行数不符"):
            restore(self.payload)

    def test_jump_r(self):
        self.payload["columns"]["tags"][1]["r"] = 5  # max_rep=1
        with self.assertRaisesRegex(RestoreError, r"r=5 (越级|大于 d)"):
            restore(self.payload)

    def test_jump_d(self):
        self.payload["columns"]["id"][0]["d"] = 9  # max_def=0
        with self.assertRaisesRegex(RestoreError, "d=9 越级"):
            restore(self.payload)

    def test_r_greater_than_d(self):
        self.payload["columns"]["name"][1] = {"r": 1, "d": 0, "v": None}
        with self.assertRaisesRegex(RestoreError, r"r=1 大于 d=0"):
            restore(self.payload)

    def test_placeholder_value_not_null(self):
        self.payload["columns"]["name"][1]["v"] = "x"
        with self.assertRaisesRegex(RestoreError, "占位令牌"):
            restore(self.payload)

    def test_int_rejects_bool(self):
        self.payload["columns"]["id"][0]["v"] = True
        with self.assertRaisesRegex(RestoreError, "64 位"):
            restore(self.payload)

    def test_int_rejects_float_and_overflow(self):
        self.payload["columns"]["id"][0]["v"] = 1.5
        with self.assertRaises(RestoreError):
            restore(self.payload)
        self.setUp()
        self.payload["columns"]["id"][0]["v"] = 2 ** 63
        with self.assertRaises(RestoreError):
            restore(self.payload)

    def test_string_rejects_int(self):
        self.payload["columns"]["tags"][0]["v"] = 42
        with self.assertRaises(RestoreError):
            restore(self.payload)

    def test_token_not_object(self):
        self.payload["columns"]["id"][0] = [0, 0, 1]
        with self.assertRaisesRegex(RestoreError, r"令牌\[0\]"):
            restore(self.payload)

    def test_token_wrong_keys(self):
        self.payload["columns"]["id"][0] = {"r": 0, "d": 0}
        with self.assertRaisesRegex(RestoreError, "r/d/v"):
            restore(self.payload)

    def test_extra_and_missing_column(self):
        del self.payload["columns"]["code"]
        with self.assertRaisesRegex(RestoreError, "缺失列"):
            restore(self.payload)
        self.setUp()
        self.payload["columns"]["nope"] = []
        with self.assertRaisesRegex(RestoreError, "多余列"):
            restore(self.payload)

    def test_extra_token_without_element_switch(self):
        # required 叶 id 追加 r=1 令牌：max_rep=0，越级拒绝
        self.payload["columns"]["id"].append(
            {"r": 1, "d": 0, "v": 99}
        )
        with self.assertRaisesRegex(RestoreError, r"越级|大于 d"):
            restore(self.payload)

    def test_mid_row_r_zero_on_repeated_leaf(self):
        # tags 行内第二个元素被改成 r=0：被当成新行，行数不符
        self.payload["columns"]["tags"][1]["r"] = 0
        with self.assertRaisesRegex(RestoreError, "行数不符"):
            restore(self.payload)

    def test_negative_r_d_and_negative_row_count(self):
        self.payload["columns"]["id"][0]["r"] = -1
        with self.assertRaises(RestoreError):
            restore(self.payload)
        self.setUp()
        self.payload["row_count"] = -1
        with self.assertRaisesRegex(RestoreError, "row_count"):
            restore(self.payload)

    def test_int64_boundaries_accepted(self):
        p = self.payload
        p["columns"]["id"][0]["v"] = 2 ** 63 - 1
        p["columns"]["id"][1]["v"] = -(2 ** 63)
        rows = restore(p)
        self.assertEqual(rows[0]["id"], 2 ** 63 - 1)
        self.assertEqual(rows[1]["id"], -(2 ** 63))

    def test_trailing_unclaimable_token_rejected(self):
        # 末尾追加一个无法归属到任何行/元素的 r=2 令牌
        self.payload["columns"]["matrix.vals"].append(
            {"r": 2, "d": 2, "v": 99}
        )
        with self.assertRaisesRegex(RestoreError, r"越级|元素需要"):
            restore(self.payload)

    def test_struct_element_boundary_conflict_across_columns(self):
        # gname 第二元素 r=1 改 r=0：本列多出一个行首，行数先不符
        self.payload["columns"]["groups.gname"][1]["r"] = 0
        with self.assertRaisesRegex(RestoreError, "行数不符"):
            restore(self.payload)

    def test_empty_placeholder_wrong_depth_rejected(self):
        # 空 tags 列表的占位 d 必须为 0，篡改为 1 后 v=null 与深度矛盾
        self.payload["columns"]["tags"][2]["d"] = 1
        with self.assertRaisesRegex(RestoreError, "null|string"):
            restore(self.payload)

    def test_row_count_zero_vs_positive_tokens(self):
        # 正向零行已在 RoundTrip 覆盖；这里验证非零 row_count 配空列被拒
        self.payload["columns"]["id"] = []
        with self.assertRaisesRegex(RestoreError, "行数不符"):
            restore(self.payload)


class SchemaRejectTests(unittest.TestCase):
    def setUp(self):
        self.payload = encode(build_schema(), build_records())

    def reject_schema(self, mutator):
        mutator(self.payload["schema"])
        with self.assertRaises(RestoreError):
            restore(self.payload)

    def test_root_must_be_required_struct(self):
        self.reject_schema(lambda s: s.update(mode="optional"))
        self.setUp()
        self.reject_schema(lambda s: s.update(type="int"))

    def test_bad_field_name(self):
        self.reject_schema(lambda s: s["children"][0].update(name="bad-name"))
        self.setUp()
        self.reject_schema(lambda s: s["children"][0].update(name=""))

    def test_duplicate_sibling(self):
        def dup(s):
            s["children"].append(dict(s["children"][0]))
        self.reject_schema(dup)

    def test_struct_without_children(self):
        self.reject_schema(lambda s: s["children"][3].pop("children"))

    def test_bad_mode_and_type(self):
        self.reject_schema(lambda s: s["children"][0].update(mode="nullable"))
        self.setUp()
        self.reject_schema(lambda s: s["children"][0].update(type="long"))

    def test_extra_node_key(self):
        self.reject_schema(lambda s: s["children"][0].update(extra=1))

    def test_bad_top_level_keys(self):
        self.payload["extra"] = 1
        with self.assertRaises(RestoreError):
            restore(self.payload)


class EncoderGuardTests(unittest.TestCase):
    def test_encode_rejects_wrong_shape(self):
        schema = build_schema()
        with self.assertRaises(EncodeError):
            encode(schema, [{"id": "not-int"}])
        with self.assertRaises(EncodeError):
            encode(schema, [{"id": 1}])  # 根记录缺键
        bad = build_records()
        bad[0]["tags"] = "not-a-list"
        with self.assertRaises(EncodeError):
            encode(schema, bad)


class CliTests(unittest.TestCase):
    def run_cli(self, in_path, out_path):
        proc = subprocess.run(
            [sys.executable, "-I", CLI, in_path, out_path],
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stderr

    def test_success_replaces_and_failure_keeps_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp = os.path.join(tmp, "in.json")
            out = os.path.join(tmp, "out.json")
            good = encode(build_schema(), build_records())
            with open(inp, "w", encoding="utf-8") as f:
                json.dump(good, f)
            with open(out, "w", encoding="utf-8") as f:
                f.write("OLD-RESULT")

            # 先喂一个非法输入，旧结果必须保留
            bad = copy.deepcopy(good)
            bad["columns"]["id"][0]["v"] = True
            with open(inp, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            code, err = self.run_cli(inp, out)
            self.assertNotEqual(code, 0)
            self.assertIn("id", err)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "OLD-RESULT")

            # 合法输入：原子替换为还原结果
            with open(inp, "w", encoding="utf-8") as f:
                json.dump(good, f)
            code, _ = self.run_cli(inp, out)
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(json.load(f), build_records())

    def test_bad_json_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp = os.path.join(tmp, "in.json")
            out = os.path.join(tmp, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write("{not json")
            code, _ = self.run_cli(inp, out)
            self.assertNotEqual(code, 0)
            self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
