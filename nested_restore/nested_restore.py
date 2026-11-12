"""
nested_restore.py
=================

从零实现的嵌套列（Dremel / Parquet 风格 repetition / definition level）
还原作业。不依赖、不调用任何现成的嵌套列解码器，层级解释与记录组装全部
在本模块内完成。

输入 JSON 结构：

    {
      "schema":    <根 required struct 节点>,
      "row_count": <非负整数>,
      "columns":   { "<点分叶路径>": [ {"r": int, "d": int, "v": 值}, ... ] }
    }

层级契约（与需求逐条对应）：

* schema 根为 required struct；节点含 name / mode / type；
  mode ∈ {required, optional, repeated}，type ∈ {struct, int, string}；
  struct 必须带非空 children；字段名非空、仅 ASCII 字母数字下划线、兄弟唯一。
* 叶路径上每经过一个 optional 或 repeated 节点，定义深度加一。
* 存在的叶值 d = 最大定义深度，v 类型匹配（int 为有符号 64 位且不是 bool；
  string 为 str；空字符串 "" 合法；整数 0 合法）。
* 遇首个缺失 optional 或空 repeated 即停止展开，给每个后代叶发一个占位
  令牌：d = 进入该节点之前的定义深度，v = null。
* 令牌按行、按列表顺序排列；每个新行首令牌 r = 0；行内 r 是“首次换到新
  元素”的那个 repeated 祖先的重复深度（repeated 节点从 1 计）；没有元素
  切换不得再发令牌。
* 零行时每列为空数组。

内部解码值统一表示为 (raw, provenance) 二元组：
  raw 为标量 / None / list / dict；provenance 为 (列名, 令牌下标)，
指向生成该节点的第一个令牌，保证跨列矛盾能定位到具体列与令牌下标。

非法输入整批抛出 RestoreError；库函数不产生部分结果，CLI 成功才原子替换
输出文件，失败保留旧结果。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
MODES = ("required", "optional", "repeated")
TYPES = ("struct", "int", "string")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MISSING = object()  # 合并时表示“该子树位置尚无列数据”的内部哨兵

# 解码值：(raw, (列名, 令牌下标))
Decoded = Tuple[Any, Tuple[str, int]]


class RestoreError(ValueError):
    """所有可预期的输入非法情况统一抛出本异常。"""


def _is_int64(x: Any) -> bool:
    # bool 是 int 的子类；契约要求整数非布尔。
    return isinstance(x, int) and not isinstance(x, bool) and INT64_MIN <= x <= INT64_MAX


# --------------------------------------------------------------------------- #
# schema 校验与叶信息收集
# --------------------------------------------------------------------------- #

def validate_schema(root: Any) -> Dict[str, dict]:
    """校验 schema，返回 {点分叶路径: leaf_info}。"""
    if not isinstance(root, dict):
        raise RestoreError("schema: 根节点必须是 struct 对象")
    if set(root.keys()) != {"name", "mode", "type", "children"}:
        raise RestoreError(
            "schema: 根节点必须恰好含 name/mode/type/children 四个字段"
        )
    if root.get("type") != "struct":
        raise RestoreError("schema: 根节点 type 必须为 struct")
    if root.get("mode") != "required":
        raise RestoreError("schema: 根节点 mode 必须为 required")
    if not isinstance(root.get("name"), str) or not _NAME_RE.fullmatch(root["name"]):
        raise RestoreError("schema: 根 name 必须为非空 ASCII 字母数字下划线")

    leaves: Dict[str, dict] = {}

    def walk(node: Any, depth_in: int, rep_in: int,
             parts: List[str], defs: List[int], reps: List[int],
             nodes: List[dict], is_root: bool) -> None:
        if not isinstance(node, dict):
            raise RestoreError("schema: 节点必须是对象")
        base_keys = {"name", "mode", "type"}
        allowed = base_keys | ({"children"} if node.get("type") == "struct" else set())
        if set(node.keys()) != allowed:
            raise RestoreError(
                "schema: 节点必须恰好含 name/mode/type，struct 另含 children"
            )
        name = node.get("name")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise RestoreError(
                "schema: 字段名必须为非空 ASCII 字母数字下划线"
            )
        mode = node.get("mode")
        if mode not in MODES:
            raise RestoreError(f"schema: 字段 {name!r} 的 mode 非法: {mode!r}")
        ntype = node.get("type")
        if ntype not in TYPES:
            raise RestoreError(f"schema: 字段 {name!r} 的 type 非法: {ntype!r}")

        if not is_root:
            depth_out = depth_in + (1 if mode in ("optional", "repeated") else 0)
            rep_out = rep_in + (1 if mode == "repeated" else 0)
            parts = parts + [name]
            defs = defs + [depth_out]
            reps = reps + [rep_out]
            nodes = nodes + [node]
        else:
            depth_out, rep_out = depth_in, rep_in

        if ntype == "struct":
            children = node.get("children")
            if not isinstance(children, list) or len(children) == 0:
                raise RestoreError(
                    f"schema: struct 字段 {name!r} 必须含非空 children 数组"
                )
            seen = set()
            for child in children:
                if not isinstance(child, dict):
                    raise RestoreError("schema: children 的元素必须是节点对象")
                cname = child.get("name")
                if not isinstance(cname, str) or not _NAME_RE.fullmatch(cname):
                    raise RestoreError(
                        "schema: 字段名必须为非空 ASCII 字母数字下划线"
                    )
                if cname in seen:
                    raise RestoreError(
                        f"schema: 字段 {name!r} 下兄弟字段名重复: {cname!r}"
                    )
                seen.add(cname)
                walk(child, depth_out, rep_out, parts, defs, reps, nodes, False)
        else:
            path = ".".join(parts)
            if path in leaves:  # 兄弟唯一时不会发生，防御性检查
                raise RestoreError(f"schema: 叶路径重复: {path!r}")
            leaves[path] = {
                "path_parts": parts,
                "def_counts": defs,
                "rep_counts": reps,
                "max_def": depth_out,
                "max_rep": rep_out,
                "value_type": ntype,
                "nodes": nodes,
            }

    walk(root, 0, 0, [], [], [], [], True)
    if not leaves:
        raise RestoreError("schema: 根 struct 必须至少含一个叶字段")
    return leaves


# --------------------------------------------------------------------------- #
# 令牌级校验
# --------------------------------------------------------------------------- #

def _check_token(tok: Any, col: str, idx: int,
                 max_def: int, max_rep: int, value_type: str) -> None:
    if not isinstance(tok, dict):
        raise RestoreError(f"列 {col!r} 令牌[{idx}]: 必须是 {{r,d,v}} 对象")
    if set(tok.keys()) != {"r", "d", "v"}:
        raise RestoreError(
            f"列 {col!r} 令牌[{idx}]: 必须恰好含 r/d/v 三个键"
        )
    r, d, v = tok["r"], tok["d"], tok["v"]
    if not isinstance(r, int) or isinstance(r, bool) or r < 0:
        raise RestoreError(f"列 {col!r} 令牌[{idx}]: r 必须为非负整数")
    if not isinstance(d, int) or isinstance(d, bool) or d < 0:
        raise RestoreError(f"列 {col!r} 令牌[{idx}]: d 必须为非负整数")
    if r > d:
        raise RestoreError(
            f"列 {col!r} 令牌[{idx}]: r={r} 大于 d={d}，层级非法"
        )
    if r > max_rep:
        raise RestoreError(
            f"列 {col!r} 令牌[{idx}]: r={r} 越级，本列最大重复深度为 {max_rep}"
        )
    if d > max_def:
        raise RestoreError(
            f"列 {col!r} 令牌[{idx}]: d={d} 越级，本列最大定义深度为 {max_def}"
        )
    if d == max_def:
        if value_type == "int":
            if not _is_int64(v):
                raise RestoreError(
                    f"列 {col!r} 令牌[{idx}]: 最大定义深度的 int 值必须是"
                    f"有符号 64 位非布尔整数，收到 {v!r}"
                )
        elif not isinstance(v, str):
            raise RestoreError(
                f"列 {col!r} 令牌[{idx}]: 最大定义深度的 string 值必须是"
                f"字符串，收到 {v!r}"
            )
    elif v is not None:
        raise RestoreError(
            f"列 {col!r} 令牌[{idx}]: 占位令牌(d={d} < max_def={max_def})"
            f"的 v 必须为 null，收到 {v!r}"
        )


# --------------------------------------------------------------------------- #
# 单列解码：层级解释与组装自行实现
# --------------------------------------------------------------------------- #

class _Cursor:
    """单列令牌流上的只读游标。"""

    def __init__(self, tokens: List[dict], col: str):
        self.tokens = tokens
        self.col = col
        self.i = 0

    def peek(self) -> Optional[dict]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> dict:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def prov(self) -> Tuple[str, int]:
        return (self.col, self.i - 1)


def _decode_node(cur: _Cursor, nodes: List[dict], def_counts: List[int],
                 rep_counts: List[int], max_def: int, max_rep: int,
                 value_type: str, i: int, entry_d: int) -> Decoded:
    """解码 nodes[i:] 子树上的“一个实例”，返回 (raw, provenance)。

    raw 形态：叶 -> 标量或 None；struct -> {子名: Decoded}；
    repeated -> list[Decoded]。provenance 指向本实例消费的第一个令牌。
    """
    node = nodes[i]
    mode, ntype = node["mode"], node["type"]
    is_leaf = ntype != "struct"
    node_rep = rep_counts[i]

    tok = cur.peek()
    if tok is None:
        raise RestoreError(
            f"列 {cur.col!r}: 行内令牌不足，等待字段 {node['name']!r}"
        )
    if tok["d"] < entry_d:
        raise RestoreError(
            f"列 {cur.col!r} 令牌[{cur.i}]: d={tok['d']} 越级，"
            f"进入字段 {node['name']!r} 前定义深度至少为 {entry_d}"
        )

    # ---------------- 叶节点 ---------------- #
    if is_leaf:
        if mode == "repeated":
            # 空列表占位
            if tok["d"] == entry_d:
                cur.take()
                return [], cur.prov()
            if tok["r"] > node_rep:
                raise RestoreError(
                    f"列 {cur.col!r} 令牌[{cur.i}]: r={tok['r']} 越级，"
                    f"repeated 叶 {node['name']!r} 内无更深 repeated 祖先"
                )
            first = cur.take()
            if first["d"] != max_def:
                raise RestoreError(
                    f"列 {cur.col!r} 令牌[{cur.i - 1}]: repeated 叶元素必须"
                    f"处于最大定义深度 {max_def}"
                )
            values: List[Decoded] = [(first["v"], cur.prov())]
            while True:
                nxt = cur.peek()
                if nxt is None or nxt["r"] < node_rep:
                    break
                if nxt["r"] != node_rep:
                    raise RestoreError(
                        f"列 {cur.col!r} 令牌[{cur.i}]: r={nxt['r']} 越级，"
                        f"叶 {node['name']!r} 内无更深 repeated 祖先"
                    )
                if nxt["d"] != max_def:
                    raise RestoreError(
                        f"列 {cur.col!r} 令牌[{cur.i}]: repeated 叶元素必须"
                        f"处于最大定义深度 {max_def}"
                    )
                cur.take()
                values.append((nxt["v"], cur.prov()))
            return values, values[0][1]

        if mode == "optional" and tok["d"] == entry_d:
            # 缺失 optional 叶：null 占位
            cur.take()
            return None, cur.prov()

        full = cur.take()
        if full["d"] != max_def:
            raise RestoreError(
                f"列 {cur.col!r} 令牌[{cur.i - 1}]: 叶 {node['name']!r} "
                f"出现非预期占位深度 d={full['d']}（max_def={max_def}）"
            )
        return full["v"], cur.prov()

    # ---------------- repeated struct ---------------- #
    if mode == "repeated":
        if tok["d"] == entry_d:
            # 空 repeated struct：本叶列只看到一个占位令牌
            cur.take()
            return [], cur.prov()
        if tok["r"] > node_rep:
            raise RestoreError(
                f"列 {cur.col!r} 令牌[{cur.i}]: r={tok['r']} 越级，"
                f"字段 {node['name']!r} 的重复深度为 {node_rep}"
            )
        elements: List[Decoded] = []
        while True:
            nxt = cur.peek()
            if nxt is None:
                break
            if elements:
                # 首个之后的元素：必须由本级 repeated 切换进入
                if nxt["r"] < node_rep:
                    break
                if nxt["r"] != node_rep:
                    raise RestoreError(
                        f"列 {cur.col!r} 令牌[{cur.i}]: 切换 "
                        f"{node['name']!r} 元素需要 r={node_rep}，"
                        f"收到 r={nxt['r']}（越级）"
                    )
            elif nxt["r"] > node_rep:
                raise RestoreError(
                    f"列 {cur.col!r} 令牌[{cur.i}]: r={nxt['r']} 越级，"
                    f"字段 {node['name']!r} 的重复深度为 {node_rep}"
                )
            child_decoded = _decode_node(
                cur, nodes, def_counts, rep_counts, max_def, max_rep,
                value_type, i + 1, def_counts[i]
            )
            # 每个元素是 nodes[i] 这个 struct 的一个实例：其直属孩子（通向
            # 本叶的下一跳 nodes[i+1]）映射到递归解出的孩子子树。
            # 约定：dict 的值统一是 Decoded 元组。
            elem_struct = {nodes[i + 1]["name"]: child_decoded}
            elements.append((elem_struct, child_decoded[1]))
        if not elements:
            raise RestoreError(
                f"列 {cur.col!r}: repeated 字段 {node['name']!r} 声明非空"
                f"（d>{entry_d}）但未解码出任何元素"
            )
        return elements, elements[0][1]

    # ---------------- optional / required struct ---------------- #
    if mode == "optional" and tok["d"] == entry_d:
        # 缺失 optional struct：停止展开，消费占位
        cur.take()
        return None, cur.prov()

    child_decoded = _decode_node(
        cur, nodes, def_counts, rep_counts, max_def, max_rep,
        value_type, i + 1, def_counts[i]
    )
    # struct 节点：把孩子子树包在直属孩子名下（本列只经过一条子链）。
    # 约定：dict 的值统一是 Decoded 元组。
    return {nodes[i + 1]["name"]: child_decoded}, child_decoded[1]


def decode_column(col: str, tokens: List[dict], leaf: dict) -> List[Decoded]:
    """把单列令牌解码成按行的子树片段（每行一个 Decoded）。"""
    max_def, max_rep = leaf["max_def"], leaf["max_rep"]
    value_type = leaf["value_type"]
    for idx, tok in enumerate(tokens):
        _check_token(tok, col, idx, max_def, max_rep, value_type)

    if not tokens:
        return []

    if tokens[0]["r"] != 0:
        raise RestoreError(f"列 {col!r}: 首个令牌 r 必须为 0（新行首令牌）")

    cur = _Cursor(tokens, col)
    rows: List[Decoded] = []
    while cur.i < len(tokens):
        head = tokens[cur.i]
        if head["r"] != 0:
            raise RestoreError(
                f"列 {col!r} 令牌[{cur.i}]: 行首 r 必须为 0，"
                f"收到 r={head['r']}；行内无元素切换不得再发令牌"
            )
        fragment = _decode_node(
            cur, leaf["nodes"], leaf["def_counts"], leaf["rep_counts"],
            max_def, max_rep, value_type, 0, 0
        )
        rows.append(fragment)
    return rows


# --------------------------------------------------------------------------- #
# 跨列合并：统一共享祖先的存在性、列表长度与元素边界
# --------------------------------------------------------------------------- #

def _merge(existing: Any, incoming: Decoded, path: str) -> Decoded:
    """把 incoming 合并进 existing（_MISSING 表示空位），矛盾即拒绝。"""
    if existing is _MISSING:
        return incoming

    a_raw, a_prov = existing
    b_raw, b_prov = incoming

    def conflict(detail: str) -> RestoreError:
        return RestoreError(
            f"跨列矛盾于 {path or '<根>'}: {detail}；"
            f"列 {a_prov[0]!r} 令牌[{a_prov[1]}] 与 "
            f"列 {b_prov[0]!r} 令牌[{b_prov[1]}]"
        )

    if a_raw is None or b_raw is None:
        if a_raw is not b_raw:
            raise conflict("同一 optional 节点存在性不一致（null 对非 null）")
        return existing

    if isinstance(a_raw, dict):
        if not isinstance(b_raw, dict):
            raise conflict("结构类型不一致（期望 struct）")
        # dict 的值统一是 Decoded 元组（标量/None/list/dict 均带 provenance）。
        merged = dict(a_raw)
        for key, val in b_raw.items():
            slot = merged.get(key, _MISSING)
            if slot is _MISSING:
                merged[key] = val  # val 本身即 Decoded
            else:
                merged[key] = _merge(
                    slot, val, f"{path}.{key}" if path else key
                )
        return merged, a_prov

    if isinstance(a_raw, list):
        if not isinstance(b_raw, list):
            raise conflict("结构类型不一致（期望 list）")
        if len(a_raw) != len(b_raw):
            raise conflict(
                f"共享 repeated 祖先的列表长度不一致 "
                f"{len(a_raw)} != {len(b_raw)}"
            )
        out = [
            _merge(a, b, f"{path}[{j}]")
            for j, (a, b) in enumerate(zip(a_raw, b_raw))
        ]
        return out, a_prov

    # 标量叶被两个列重复定义（schema 正常时不会发生）
    raise conflict("叶值被重复定义")


def _strip(v: Any) -> Any:
    """去掉 provenance，输出纯 JSON 值。"""
    if isinstance(v, tuple):
        return _strip(v[0])
    if isinstance(v, dict):
        return {k: _strip(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def restore(payload: Any) -> List[dict]:
    """校验 -> 逐列解码 -> 跨列合并 -> 原序记录。出错整体拒绝。"""
    if not isinstance(payload, dict):
        raise RestoreError("输入必须是 JSON 对象")
    if set(payload.keys()) != {"schema", "row_count", "columns"}:
        raise RestoreError("输入必须恰好含 schema/row_count/columns 三个键")

    row_count = payload["row_count"]
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise RestoreError("row_count 必须为非负整数")

    columns = payload["columns"]
    if not isinstance(columns, dict) or not all(isinstance(k, str) for k in columns):
        raise RestoreError("columns 必须是以点分叶路径为键的对象")
    for key, toks in columns.items():
        if not isinstance(toks, list):
            raise RestoreError(f"列 {key!r}: 令牌必须是数组")

    leaves = validate_schema(payload["schema"])
    expected, actual = set(leaves), set(columns)
    if actual != expected:
        detail = []
        if expected - actual:
            detail.append(f"缺失列 {sorted(expected - actual)}")
        if actual - expected:
            detail.append(f"多余列 {sorted(actual - expected)}")
        raise RestoreError("columns 必须恰好覆盖所有叶: " + "; ".join(detail))

    if row_count == 0:
        for col, toks in columns.items():
            if toks:
                raise RestoreError(
                    f"列 {col!r}: row_count=0 时令牌数组必须为空，"
                    f"实际含 {len(toks)} 个令牌"
                )
        return []

    ordered = sorted(leaves, key=lambda c: leaves[c]["path_parts"])
    decoded: Dict[str, List[Decoded]] = {}
    for col in ordered:
        rows = decode_column(col, columns[col], leaves[col])
        if len(rows) != row_count:
            raise RestoreError(
                f"列 {col!r}: 行数不符，r=0 令牌数 {len(rows)} "
                f"!= row_count {row_count}"
            )
        decoded[col] = rows

    result: List[Any] = [_MISSING] * row_count
    for col in ordered:
        leaf = leaves[col]
        top_name = leaf["path_parts"][0]
        for row_idx, fragment in enumerate(decoded[col]):
            # _decode_node 已返回从根的直属孩子起的完整子树；
            # 容器内统一 Decoded 元组，根级 dict 的值即 fragment 本身。
            wrapped = ({top_name: fragment}, fragment[1])
            result[row_idx] = _merge(result[row_idx], wrapped, "")

    return [_strip(r) for r in result]
