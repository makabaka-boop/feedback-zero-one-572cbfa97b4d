"""
encoder.py
==========

独立正向编码器：把原序嵌套记录按 schema 编码成 {r, d, v} 叶列令牌。

本模块是验收用的参考实现，层级深度自行根据 schema 计算，不导入解码器的
任何内部函数，保证编码 / 解码两侧逻辑相互独立，对拍才有意义。

两个深度的语义（独立实现，与还原契约同源）：

* d 定义深度：路径每经过一个 optional / repeated 节点加一；存在的叶值以
  最大定义深度发出，v 为真实值；optional=None 或空 repeated 时停止展开，
  给每个后代叶在“进入该节点前的 d”发占位令牌，v=null。
* r 重复深度：repeated 节点从 1 起沿路径累计。每个节点持有自己的全局
  重复深度 rep；一段上下文中的“首元素”首令牌沿用上一层的 first_r，
  其后每次元素切换的 r 取被切换 repeated 节点的全局 rep。新行首 r=0。
"""

from __future__ import annotations

from typing import Any, Dict, List

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


class EncodeError(ValueError):
    pass


def _is_int64(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and INT64_MIN <= x <= INT64_MAX


def _all_leaf_paths(node: dict, prefix: str = "") -> List[str]:
    name = node["name"]
    path = f"{prefix}.{name}" if prefix else name
    if node["type"] != "struct":
        return [path]
    out: List[str] = []
    for child in node["children"]:
        out.extend(_all_leaf_paths(child, path))
    return out


def _emit_placeholders(node: dict, d: int, first_r: int,
                       columns: Dict[str, List[dict]], prefix: str) -> None:
    """节点缺失 / 空列表时，给每个后代叶在深度 d 发占位令牌。"""
    path = f"{prefix}.{node['name']}" if prefix else node["name"]
    if node["type"] != "struct":
        columns[path].append({"r": first_r, "d": d, "v": None})
        return
    for child in node["children"]:
        _emit_placeholders(child, d, first_r, columns, path)


def _emit(node: dict, value: Any, d: int, rep: int, first_r: int,
          columns: Dict[str, List[dict]], prefix: str) -> None:
    """把 value 编码进 columns。

    d       进入本节点前的定义深度；
    rep     本节点所在的全局重复深度（repeated 祖先计数，根级为 0）；
    first_r 本节点在当前上下文中首个令牌应使用的 r（新行/首元素沿用上一层）。
    """
    path = f"{prefix}.{node['name']}" if prefix else node["name"]
    mode, ntype = node["mode"], node["type"]

    if ntype == "struct":
        if mode == "repeated":
            if not isinstance(value, list):
                raise EncodeError(f"字段 {path!r} 为 repeated，值必须是列表")
            if len(value) == 0:
                _emit_placeholders(node, d, first_r, columns, prefix)
                return
            elem_d = d + 1
            elem_rep = rep + 1  # 本级 repeated 的全局重复深度
            for idx, elem in enumerate(value):
                if not isinstance(elem, dict):
                    raise EncodeError(f"字段 {path!r} 的元素必须是 struct 对象")
                _check_struct_keys(node, elem, path)
                # 首元素首令牌沿用上一层 first_r；之后元素切换 r=elem_rep。
                child_first_r = first_r if idx == 0 else elem_rep
                for child in node["children"]:
                    _emit(child, elem[child["name"]], elem_d, elem_rep,
                          child_first_r, columns, path)
            return

        if mode == "optional":
            if value is None:
                _emit_placeholders(node, d, first_r, columns, prefix)
                return
            if not isinstance(value, dict):
                raise EncodeError(f"字段 {path!r} 必须是 struct 对象或 null")
            d = d + 1
        else:  # required struct
            if not isinstance(value, dict):
                raise EncodeError(f"字段 {path!r} 为 required struct，不能为 null")

        _check_struct_keys(node, value, path)
        for child in node["children"]:
            _emit(child, value[child["name"]], d, rep, first_r,
                  columns, path)
        return

    # 叶节点
    if mode == "repeated":
        if not isinstance(value, list):
            raise EncodeError(f"字段 {path!r} 为 repeated，值必须是列表")
        if len(value) == 0:
            columns[path].append({"r": first_r, "d": d, "v": None})
            return
        elem_rep = rep + 1
        for idx, elem in enumerate(value):
            _check_scalar(node, elem, path)
            tok_r = first_r if idx == 0 else elem_rep
            columns[path].append({"r": tok_r, "d": d + 1, "v": elem})
        return

    if mode == "optional":
        if value is None:
            columns[path].append({"r": first_r, "d": d, "v": None})
            return
        _check_scalar(node, value, path)
        columns[path].append({"r": first_r, "d": d + 1, "v": value})
        return

    # required 叶
    _check_scalar(node, value, path)
    columns[path].append({"r": first_r, "d": d, "v": value})


def _check_struct_keys(node: dict, value: dict, path: str) -> None:
    names = {c["name"] for c in node["children"]}
    if set(value.keys()) != names:
        raise EncodeError(
            f"struct {path!r} 的键必须恰好为 {sorted(names)}，"
            f"收到 {sorted(value.keys())}"
        )


def _check_scalar(node: dict, value: Any, path: str) -> None:
    if value is None:
        raise EncodeError(f"字段 {path!r} 不允许 null（检查 mode 声明）")
    if node["type"] == "int":
        if not _is_int64(value):
            raise EncodeError(f"字段 {path!r} 需要有符号 64 位非布尔整数")
    elif not isinstance(value, str):
        raise EncodeError(f"字段 {path!r} 需要字符串")


def _validate_schema_shape(node: Any, is_root: bool = True) -> None:
    if not isinstance(node, dict):
        raise EncodeError("schema 节点必须是对象")
    if not isinstance(node.get("name"), str) or not node["name"]:
        raise EncodeError("schema 字段名非法")
    if node.get("mode") not in ("required", "optional", "repeated"):
        raise EncodeError(f"schema 字段 {node.get('name')!r} mode 非法")
    if node.get("type") not in ("struct", "int", "string"):
        raise EncodeError(f"schema 字段 {node.get('name')!r} type 非法")
    if is_root and (node["type"] != "struct" or node["mode"] != "required"):
        raise EncodeError("schema 根必须是 required struct")
    if node["type"] == "struct":
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise EncodeError(f"schema struct {node['name']!r} 需非空 children")
        names = [c.get("name") for c in children]
        if len(set(names)) != len(names):
            raise EncodeError(f"schema struct {node['name']!r} 兄弟字段名重复")
        for child in children:
            _validate_schema_shape(child, False)


def encode(schema: dict, records: List[dict]) -> Dict[str, Any]:
    """正向编码入口，返回可直接喂给 restore() 的 payload。"""
    _validate_schema_shape(schema)
    if not isinstance(records, list):
        raise EncodeError("records 必须是记录列表")
    for rec in records:
        if not isinstance(rec, dict):
            raise EncodeError("每条记录必须是 struct 对象")

    # 根名不出现在点分叶路径中：列键从根的孩子开始、prefix 为空。
    columns: Dict[str, List[dict]] = {}
    for child in schema["children"]:
        for leaf_path in _all_leaf_paths(child, ""):
            columns[leaf_path] = []

    root_names = {c["name"] for c in schema["children"]}
    for rec in records:
        if set(rec.keys()) != root_names:
            raise EncodeError(
                f"根记录键必须恰好为 {sorted(root_names)}，收到 {sorted(rec.keys())}"
            )
        # 每新行：rep=0、首令牌 r=0。
        for child in schema["children"]:
            _emit(child, rec[child["name"]], 0, 0, 0, columns, "")
    return {"schema": schema, "row_count": len(records), "columns": columns}
