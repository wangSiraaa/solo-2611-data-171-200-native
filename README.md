# 题 173 输入材料

在独立PostgreSQL 16测试库执行fixtures/seed.sql。queries.json中的query_id分别独立分析；非唯一ROWS排序是应诊断的输入，不允许静默加event_id改变SQL。

所有输入均为本地合成材料；此目录不提供题目实现、参考产物或预期答案。路径相对题目仓库根。使用 Python 3.11；无需 Docker。

---

## 实现

窗口帧解释工具见 [`README_TOOL.md`](README_TOOL.md)，方法说明见 [`docs/method.md`](docs/method.md)。

```sh
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
bash scripts/init_test_db.sh
export WINDOW_LAB_DSN='host=/tmp port=5433 dbname=window_lab'
.venv/bin/python -m window_lab all --out evidence
```
