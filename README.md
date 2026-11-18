# OSM 洪水分析水域面 API

按 OSM 关系号提交**逐次递增修订**的完整节点经纬度与无序 way（含 `outer` /
`inner` 角色），服务按节点引用拼接 way（允许线段反向），生成一个外环、至多
一个内环的 GeoJSON Polygon，并管理"当前供数面"的状态机。

当多条 OSM 水域关系圈住同一片水面时，下游按关系分别取面会**重复计入**重叠区。
服务另提供**跨关系重叠质检**接口 `POST /overlaps`：输入 2～8 个不同关系号，
从同一已提交快照固定各关系的当前修订，返回按"实际覆盖关系号集合"切分、互不
重叠的 GeoJSON 面片，并报告每个关系实际使用的修订号。

技术栈：**FastAPI + Shapely + SQLite（WAL 模式）**，可 `docker compose` 启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档（可直接上传、取面）
open http://localhost:8000/docs
```

本地无 Docker 时：

```bash
pip install -r requirements.txt
FLOOD_DB_PATH=./flood.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/relations/{relation_id}/revisions` | 上传一个完整修订（修订号必须严格递增） |
| GET  | `/relations/{relation_id}/revisions/{revision}` | 按修订号读取**原始输入 + 几何 + 状态** |
| GET  | `/relations/{relation_id}/revisions` | 列出该关系全部修订号与状态 |
| GET  | `/relations/{relation_id}/current` | 取当前供数面（最新有效修订） |
| POST | `/overlaps` | 跨关系重叠质检：按实际覆盖关系集合切分互不重叠面片 |
| GET  | `/health` | 健康检查 |

### 上传请求体

```json
{
  "revision": 1,
  "nodes": [
    {"id": 1, "lat": 0.0, "lon": 0.0},
    {"id": 2, "lat": 0.0, "lon": 1.0},
    {"id": 3, "lat": 1.0, "lon": 1.0},
    {"id": 4, "lat": 1.0, "lon": 0.0}
  ],
  "ways": [
    {"id": 10, "role": "outer", "node_refs": [1, 2, 3, 4, 1]}
  ]
}
```

约束：每次提交为该修订的**完整快照**；最多 **8 条 way、32 个节点**；way
无序，拼接时可反向遍历；`node_refs` 闭合方式沿用 OSM 惯例（首尾节点相同）。

## 校验与状态机

以下情况修订标记为 `invalid`，**原始输入完整保留**，响应中返回错误码及相关
way id，当前面**停止供数**（`/current` 返回 409）：

- `missing_nodes`：way 引用了未提交的节点
- `broken_ring`：单 way 未闭合、way 间断环、外环拆成多个连通分量
- `fork`：三条以上 way 端点汇聚，或 way 端点落在另一条 way 内部（T 型分叉）
- `self_intersection`：环自交 / 节点重复访问（8 字形等）
- `inner_ring_violation`：内环触碰外环边界、越过外环或位于外环之外
- `missing_outer`：没有 `outer` 角色的 way

停止供数期间，**上一个有效面仍可按修订号读取**，响应里的
`last_valid_revision` 指向它；后续出现有效修订后 `/current` 自动恢复供数。

输入约束违例（超 8/32 上限、角色非法、修订号非递增等）返回 422 / 409，
**不落库**。

## 跨关系重叠质检 `POST /overlaps`

请求体（2～8 个**互不相同**的关系号，示例见 `examples/overlap_request.json`）：

```json
{ "relation_ids": [1001, 1002, 1003] }
```

响应为 GeoJSON `FeatureCollection`，并在 `relation_revisions` 中给出每个关系
本次实际固定使用的修订号：

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {"relations": [1001, 1002, 1003]},
      "geometry": {"type": "Polygon", "coordinates": [...]}
    }
  ],
  "relation_revisions": {"1001": 7, "1002": 3, "1003": 12}
}
```

切分与计面规则：

- 每个面片对应一个**确切覆盖关系号集合**（`properties.relations`，至少含两个
  关系）：该面片内每一点都恰好被这组关系、且不被组外任何输入关系覆盖；面片
  之间**互不重叠**，可直接按面片面积汇总而不会重复计入。
- **三重覆盖只归入三关系集合**，不会再出现在任何两两集合中；N 重覆盖同理。
- 只保留**正面积**区域：仅沿边界（边/点）接触的关系不产生面片。
- **内环（洞）保持为洞**：洞内没有任何关系的面积，自然不计入重叠。
- 没有任何 ≥2 关系覆盖的正面积区域时，`features` 为空数组（HTTP 仍为 200）。

整组报错规则（不返回部分结果，也**不回退到旧的有效修订**）：

| 场景 | 状态码 | `detail.code` |
| --- | --- | --- |
| 某关系从未上传过任何修订 | 404 | `relation_not_found` |
| 某关系当前最新修订为 `invalid` | 409 | `current_revision_invalid`（附当前修订号与错误明细） |
| 关系号数量不在 2～8、或有重复 | 422 | 请求体校验错误 |

**快照固定与交错上传**：所有关系的最新修订在**同一个只读事务（一条 SQL）**
内读取。WAL 模式下只读事务在首条读取语句即固定数据库快照，因此请求处理期间
即使有新修订提交，响应也只会使用同一快照里的修订，**绝不会混用上传前后的
修订**；`relation_revisions` 可供下游核对所用版本。原单关系供数接口
（`/current`、按修订号读取等）行为保持不变。

## 一致性、原子性与持久化

- SQLite 启用 **WAL + `synchronous=FULL`**：提交即 fsync，重启后数据可查；
  读不阻塞写，读者只能看到完整提交的修订。
- 每次写入在单个 `BEGIN IMMEDIATE` 事务内完成"递增检查 + 插入"，进程内
  再以锁串行化；存储失败整体回滚，**不会留下半份修订**。
- 原始输入、几何、状态同事务写入，因此并发上传与读取看到的同一修订三者
  必然一致。
- 数据库位于挂载卷 `/data/flood.db`（环境变量 `FLOOD_DB_PATH` 可改）。

## 自测

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

覆盖：单/多 way 拼接与反向、内环挖洞、缺节点、断环、分叉、T 型分叉、自交、
内环触碰/越界、状态机恢复、修订号冲突、上下限校验、重启恢复、事务原子性、
并发上传/读取、`/docs` 可用性，以及跨关系重叠的确切覆盖切分（三重覆盖不重复
计入两两集合、内环洞不计、仅沿边接触不算、整组 404/409 报错、单快照修订固定
的并发竞态）。
