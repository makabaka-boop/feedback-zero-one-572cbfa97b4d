# 嵌套列还原作业（Nested Column Restore）

把按叶列拆开的 Dremel / Parquet 风格 `{r, d, v}` 令牌流还原成原序嵌套记录。
层级解释与记录组装全部从零自行实现，**不调用任何现成的嵌套列解码器**。

## 目录结构

```
nested_restore/
├── nested_restore.py   # 核心：schema 校验、单列 r/d 解码、跨列合并、主入口 restore()
├── encoder.py          # 独立正向编码器（验收用，层级算法与解码器相互独立）
├── cli.py              # 交付入口：读 JSON → 还原 → 原子写输出；失败保留旧结果
└── tests/
    └── test_restore.py # 40 个验收用例
```

仅依赖 Python 3.8+ 标准库。

## 输入 / 输出契约

输入 JSON：

```json
{
  "schema":    { "name": "root", "mode": "required", "type": "struct",
                 "children": [ ... ] },
  "row_count": 3,
  "columns": {
     "a.b": [ {"r": 0, "d": 1, "v": 10}, ... ]
  }
}
```

* **schema**：根为 `required` struct。节点含 `name` / `mode` / `type`；
  `mode ∈ required | optional | repeated`，`type ∈ struct | int | string`；
  struct 另含非空 `children`。字段名为非空 ASCII 字母数字下划线，兄弟间唯一。
* **columns**：以点分叶路径为键，恰好覆盖 schema 的全部叶；每列是 `{r,d,v}`
  令牌数组。
* 输出：原序记录数组（`List[dict]`）。

### 层级语义

* **定义深度 d**：路径每经过一个 `optional` 或 `repeated` 节点加一。存在的
  叶值 `d` 取该叶最大定义深度，`v` 类型匹配（int 为有符号 64 位且非布尔；
  string 为字符串；`0` 与 `""` 均合法）。
* **占位令牌**：遇到首个缺失 optional（值为 null）或空 repeated 时停止展开，
  给每个后代叶发一个 `d = 进入该节点前深度`、`v = null` 的占位。
* **重复深度 r**：repeated 节点从 1 起沿路径累计。令牌按行、按列表顺序排列；
  每个新行首令牌 `r = 0`；行内 r 是“首次换到新元素”的那个 repeated 祖先的
  重复深度；没有元素切换不得再发令牌。
* **零行**：`row_count = 0` 时每列必须为空数组，输出 `[]`。

## 使用方式

### 库入口

```python
from nested_restore import restore, RestoreError

try:
    records = restore(payload)      # payload 为已解析的 dict
except RestoreError as e:
    print(e)                        # 列错误带叶路径与令牌下标，如 列 'a.b' 令牌[3]
```

### 命令行入口

```bash
python3 cli.py input.json output.json
```

* 成功：先写同目录临时文件，再 `os.replace` **原子替换**输出。
* 失败：进程非零退出，错误写 stderr，**输出文件原样保留**（成功才替换）。

### 正向编码（生成测试数据）

```python
from encoder import encode
payload = encode(schema, records)   # 产出可直接喂给 restore() 的 payload
```

## 跨列一致性（拒绝补值 / 截短对齐）

每列独立解码后，按子树路径合并。共享同一祖先的兄弟列必须在以下三点完全一致，
否则整批拒绝：

1. **存在性**：同一 optional 节点，一列判 null、另一列判非 null → 拒绝；
2. **列表长度**：共享 repeated 祖先的元素个数不同 → 拒绝；
3. **元素边界**：两层列表中 r 切换点错位导致元素归属不同 → 拒绝。

所有合并冲突的报错都带两侧的**列路径与令牌下标**，便于定位。系统不会为任何
列补造值，也不会截短较长的列去对齐。

## 验收覆盖

```bash
python3 tests/test_restore.py
```

40 个用例，覆盖：

* 独立正向编码 → 反向还原完整对拍：两层列表、可选 struct、空列表、缺失叶、
  零值 `0`、空字符串 `""`、零行、单叶 schema、深层 optional/repeated 组合；
* 逐列 `r/d` 令牌序列的人工断言；
* 篡改共享祖先（列表长度、null 存在性、两层列表元素边界）验证拒绝与定位；
* 首 r 非零、行数不符、越级 r/d、`r>d`、占位 `v` 非 null、布尔/浮点/越界
  整数、字符串类型错误、令牌非对象/缺键、多余与缺失列、行内 r=0、
  无法归属的尾部令牌、负 r/d、负 row_count；
* schema 非法（根非 required struct、字段名非法、兄弟重名、struct 缺
  children、mode/type 非法、多余节点键、顶层多余键）；
* CLI：失败保留旧输出、成功原子替换、坏 JSON 输入。
