# Project Eos 开发说明书

> 面向共同开发者的工程手册。最后更新：2026-11-13，对应当前代码与 324 项测试。
>
> 代码是最终事实来源；当本文与代码不一致时，以代码为准，并请顺手更新本文。

---

## 目录

1. [项目定位与当前状态](#1-项目定位与当前状态)
2. [快速启动](#2-快速启动)
3. [架构总览](#3-架构总览)
4. [代码地图](#4-代码地图)
5. [核心数据模型与不变量](#5-核心数据模型与不变量)
6. [规则引擎与动作协议](#6-规则引擎与动作协议)
7. [模拟子系统](#7-模拟子系统)
8. [城市系统专章](#8-城市系统专章)
9. [AI 系统](#9-ai-系统)
10. [存档、回放与确定性](#10-存档回放与确定性)
11. [HTTP API](#11-http-api)
12. [前端 dashboard.html](#12-前端-dashboardhtml)
13. [配置系统](#13-配置系统)
14. [地图与美术](#14-地图与美术)
15. [测试与验收](#15-测试与验收)
16. [团队协作规范](#16-团队协作规范)
17. [常见故障排查](#17-常见故障排查)
18. [已知限制与路线图](#18-已知限制与路线图)
19. [附录：快速参考](#19-附录快速参考)

---

## 1. 项目定位与当前状态

Project Eos 是一个本地运行的文明模拟沙盘：

- 72×54 pointy-top 六边形世界，默认四国（支持 2–12 国），逐日推进，可暂停/倍速。
- 玩家可以观战，也可以随时接管任意一国；AI 与玩家使用同一套规则。
- 世界地图 + 城市局部地图双层视角；城市有独立人口、分区、粮食、预算、宜居度和 AI 市长。
- 所有动态状态由规则引擎写入，AI 只提交 JSON 动作，便于复盘、回放和调试。
- 前端是单页本地指挥台，通过 `127.0.0.1` HTTP 与 Python 引擎通信。
- 无数据库、无云端依赖、无第三方前端框架；存档是 JSON。

当前已具备的主要闭环：

| 领域 | 已实现 |
| --- | --- |
| 地图 | 固定基线世界、公平随机地图、自定义 2–12 国、六种地图图层 |
| 经济 | 粮食、仓容、财政、税收、矿业、建筑维护、贸易协定、城市地方预算 |
| 人口 | 住房、增长、饥荒、城市人口分账、宜居度 |
| 科技 | 三条科技线、六级、时代门槛、开局年代基线 |
| 城市 | 新城扩张、分区、邻接、方针、粮食物流、地方预算、AI 市长、手动/AI 托管 |
| 外交 | 关系、赠礼、贸易协定、和平提案 |
| 军事 | 招募、行军、宣战、边境/远征战斗、占领、占领贡金、围城与整城易主（含开赴封锁圈）、战争疲劳、和平；按地块看驻军、按地块下令攻打并预报价 |
| 目标 | 综合评分、四条正式胜利路线、自动结算、继续本局 |
| AI | 多国规则 AI、OpenAI 兼容模型、逐国切换、预算/Token 记录、失败回退 |
| 工程 | 存档槽、自动保存、动作日志回放、世界哈希校验、324 项 pytest |

设计原则（来自 `PROJECT_PLAN.md`）：

1. 真实性服务游戏性，但系统必须可解释、可复盘。
2. 新系统必须进入真实结算，不能只显示在日志或 UI。
3. AI 与玩家经过同一规则审核器。
4. 先完成闭环，再做精细表现。

---

## 2. 快速启动

### 2.1 环境要求

- Python **3.10+**（当前开发环境为 3.14）。
- Windows / macOS / Linux 均可。
- 依赖：`numpy`、`scipy`、`matplotlib`；测试另需 `pytest`。
- 建议把虚拟环境和大文件放在 **D 盘**，不要占用空间紧张的 C 盘。

### 2.2 首次安装（Windows / Git Bash）

```bash
cd /d/CCCQoder/project_eos/Project_Eos_Map_Generation_v0.1
python -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.txt pytest
./.venv/Scripts/python -m pytest -q
```

PowerShell / cmd 中使用：

```bat
.venv\Scripts\python -m pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest -q
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt pytest
```

> 当前仓库已有 `.venv/`，但不要把它提交到版本库。换机器时重新创建。

### 2.3 启动实时指挥台

全 Mock、零 API 费用：

```bash
./.venv/Scripts/python live_server.py --days 365 --speed 2 --paused --open
```

浏览器打开 `http://127.0.0.1:8765/`。

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--days N` | 本轮目标日；达到后自动暂停 |
| `--speed X` | 每现实秒推进 X 个游戏日 |
| `--paused` | 启动后暂停，适合先看界面 |
| `--open` | 启动后自动打开浏览器 |
| `--port N` | 换端口，默认 8765 |
| `--models FILE` | 逐国 Mock/LLM 配置 |
| `--api-key-file FILE` | 启动时读取一次密钥到进程环境变量 |
| `--max-calls N` | 付费调用次数硬上限 |
| `--max-cost-usd X` | 已知费用硬上限 |

接入真实模型（示例，不要把密钥写进仓库）：

```bash
./.venv/Scripts/python live_server.py --days 180 --speed 2 --open \
  --models config/models.deepseek.json \
  --api-key-file "/path/to/local-key-file" \
  --max-calls 2 --max-cost-usd 0.10
```

真实付费调用必须同时设置 `--max-calls` 和 `--max-cost-usd`。没有密钥或预算时，世界仍可全 Mock 运行。

### 2.4 命令行模拟

```bash
# 四国 Mock，快进 365 日
./.venv/Scripts/python run_simulation.py run --days 365

# 从存档继续
./.venv/Scripts/python run_simulation.py run --load runs/eos_365.json --days 365 \
  --save runs/eos_730.json

# 交互模式
./.venv/Scripts/python run_simulation.py interactive --load runs/eos_365.json

# 查看存档摘要
./.venv/Scripts/python run_simulation.py status --save runs/eos_365.json
```

输出：

- `--save`：完整动态存档。
- `--log`：JSONL 事件流。
- `--snapshots`：每 30 日及关键节点的国家摘要。
- `--api-usage`：本次 API 调用/Token/估算费用。

交互命令：`pause`、`resume`、`step [日]`、`ff 日`、`run 日`、`speed 每日秒数`、`status`、`save [路径]`、`quit`。

### 2.5 老地图生成管线（可选）

```bash
./.venv/Scripts/python worldgen.py --seed 2000 --output output_regenerated
./.venv/Scripts/python simulate_climate.py \
  --world output_regenerated/world_2000BCE.json \
  --out output_regenerated/first_year_weather.json --seed 2000 --days 365
./.venv/Scripts/python build_viewer.py
```

**重要：** `output/world_2000BCE.json` 是校验过的静态基线，不要覆盖。重新生成请写到 `output_regenerated/`。

### 2.6 开发时最小验证

```bash
./.venv/Scripts/python -m pytest -q
```

提交前建议再跑一轮 365 日 Mock：

```bash
./.venv/Scripts/python run_simulation.py run --days 365 \
  --save runs/_check_365.json --log runs/_check_365.jsonl \
  --snapshots runs/_check_365_snapshots.json
```

---

## 3. 架构总览

### 3.1 分层

```
浏览器 dashboard.html
        │  fetch /api/*
        ▼
live_server.py（LiveSession + ThreadingHTTPServer）
        │  锁定、线程、存档槽、快照
        ▼
core/simulation.py（游戏循环、暂停/倍速、JSONL）
        │
        ▼
core/rules.py — RuleEngine（唯一动态状态写入口）
        │
        ├── core/economy.py        粮食、财政、人口、科研、城市服务
        ├── core/construction.py   建设队列
        ├── core/diplomacy.py      关系、交易、协定、和平
        ├── core/military.py       军队、行军、战斗、占领
        └── core/city_governance.py AI 市长（只改 AI 托管城市）
        │
        ▼
core/state.py — GameState / CountryState / CityState / WorldStatic
        │
        ├── config/rules.json      全部可调参数
        └── output/world_*.json    静态地理世界
```

AI 旁路：

```
AgentScheduler ── deepcopy(observation) ──> HeuristicAgent / OpenAICompatibleAgent
       │                                              │
       └──────────── JSON decision ───────────────────┘
                              │
                     RuleEngine.apply_decision
```

前端永远不直接改状态；它只提交动作，然后重新拉取 `/api/state`。

### 3.2 一次日推进的完整顺序

`RuleEngine.step_day()` 的顺序是硬约束：

1. 过期外交关系与贸易/和平提案。
2. 若到规划日（默认每 90 日）：国家 AI 规划 + 城市 AI 市长规划。
3. AI 边疆开拓（`enable_frontier_ai`）。
4. 采样并记录当日天气。
5. 各国每日经济结算（粮食、地方物流、城市预算、财政、饥荒）——`country.last_economy` 在这里写定。
6. **占领贡金划拨 `military.tribute_daily()`**：必须紧跟在经济结算之后，因为每笔贡金按缴付方当日毛收入定价，账本也在这里倒计时与到期。
7. 行军推进 `military.march_daily()`（抵达当日才结算会战）。
8. 军队维护/疲劳 `military.upkeep_daily()`。
9. 建设推进。
10. 兵营每日募兵 `military.barracks_daily()`。
11. 各国科研推进。
12. 若为月末：天气月度汇总、灾害、城市服务与宜居度、人口增长、稳定度、军队月结算、社会事件。
13. `day_index += 1`。

最后（实时服务层）检查胜利条件，达成则暂停、记录 `victory` 事件并保存。

**新增系统时若要参与每日结算，应插入上述顺序并说明理由。** 不要绕过 `RuleEngine` 在 UI 或 Agent 中直接改状态。

### 3.3 一次动作的完整流程

玩家：

```
dashboard 提交 {actor, action}
  → LiveSession.player_action（校验是否接管该国）
  → RuleEngine.apply_action
  → validate_action（严格 JSON schema）
  → _execute（业务校验 + 状态变更）
  → ActionResult + action_log + event_log + receipts[actor]
  → /api/action 返回 {ok, result}
  → 前端 poll(true) 刷新
```

AI：

```
90 日规划点
  → AgentScheduler.build_observation（只含该国信息，deepcopy）
  → Agent.decide(observation)
  → apply_decision（校验 country_id、world_day、最多 4 个动作）
  → 逐个 apply_action
  → 失败动作记录拒绝原因，不会破坏已接受动作
```

---

## 4. 代码地图

| 文件 | 职责 | 主要改动者 |
| --- | --- | --- |
| `core/state.py` | 数据模型、世界加载/哈希、存档、日历、summary、胜利状态 | 核心/引擎 |
| `core/rules.py` | 唯一写入口、动作执行、日推进顺序、SETTLE | 核心/引擎 |
| `core/actions.py` | 动作白名单、JSON schema 校验、ActionResult | 核心/协议 |
| `core/economy.py` | 粮食、税收、矿业、维护、服务预算、人口、科研 | 经济/城市 |
| `core/society.py` | 城市指标、分区邻接、宜居度、评分、时代、胜利条件 | 城市/平衡 |
| `core/city_governance.py` | AI 市长：方针、分区、预算 | 城市/AI |
| `core/messages.py` | 事件驱动白话短讯、优先级与冷却 | AI/UI |
| `core/frontier.py` | AI 边疆选址与扩张条件 | 城市/AI |
| `core/construction.py` | 建设队列与劳动力/政策进度 | 经济 |
| `core/diplomacy.py` | 关系、赠礼、贸易托管、协定、战争/和平 | 外交 |
| `core/military.py` | 逐兵种招募、行军、克制/地形结算、占领、维护、疲劳 | 军事 |
| `core/content.py` | `rules.json` 内容校验闸门与兵种编成工具 | 核心/策划 |
| `core/weather.py` | 可复现天气 RNG、月统计、灾害基础 | 世界 |
| `core/simulation.py` | 运行/暂停/倍速、JSONL、自动存档 | 引擎 |
| `core/replay.py` | 从动作日志重建最终状态 | 引擎/QA |
| `agents/scheduler.py` | 90 日规划、隔离观察、并发决策、玩家国家跳过 | AI |
| `agents/heuristic.py` | 多国规则 AI 基线 | AI |
| `agents/llm_adapter.py` | OpenAI 兼容适配、预算、Token 记录、失败回退 | AI |
| `live_server.py` | 本地 HTTP、LiveSession、存档槽、API | 后端/UI |
| `dashboard.html` | 分层 UI：一级导航、二级标签、地图、城市视图、通知 | 前端 |
| `config/rules.json` | 所有数值与内容定义 | 平衡/策划 |
| `worldgen.py` | 原始世界生成、气候、水文、灾害、天气函数 | 地图 |
| `mapgen.py` | 公平随机世界、自定义 2–12 国、v1/v2 入口 | 地图 |
| `mapgen_v2.py` | 连续地形、地形纹理、细六边形、河深与道路格 | 地图 |
| `run_simulation.py` | 批处理/交互 CLI | 工具 |
| `build_viewer.py` / `simulate_climate.py` / `plot_elevation.py` | 离线地图/天气/海拔工具 | 地图 |
| `tests/` | 规则、经济、AI、回放、地图、LLM 测试 | 所有人 |
| `assets/` | 设施与建筑图标 | 美术/前端 |

当前规模约 9,000 行 Python/HTML；`dashboard.html` 约 1,850 行，是最大的单文件。

---

## 5. 核心数据模型与不变量

### 5.1 `WorldStatic`

- 只读地理世界：72×54 六边形、气候、河流、灾害、初始国家。
- 加载时计算文件 SHA-256；存档记录该哈希，加载时不一致会拒绝。
- 坐标使用 `(col, row)`；邻居与距离函数来自 `worldgen.neighbors` / `hex_distance`。
- 世界 JSON 里的 `owner_id` 是基线示意；**运行时领土以 `CountryState.tiles` 为准**。

### 5.2 `GameState`

主要字段：

```text
world, config, seed
start_astronomical_year, start_era_index
day_index
countries: dict[str, CountryState]
trades, relations, pacts, diplomatic_proposals
weather: WeatherModel
action_log, event_log
receipts: dict[str, list]
next_action_number, next_project_number, next_trade_number, next_diplomacy_number
```

- `summary()` 生成前端快照，不是存档本身。
- `fingerprint()` 对动态状态做稳定哈希，用于回放校验。
- `receipts` 是每个 actor 最近 `RECEIPT_LIMIT`（12）条命令回执（`action_id/day/action_type/operation/target/accepted/reason_code/reason_text/action`），由 `RuleEngine.apply_action` 在审核后写入、进存档与 `summary()`，但**刻意不参与 `fingerprint()`**：回执只是显示账，同一局面多记一条不算新状态。
- `victory_status()` 计算各国胜利条件与最终赢家。

### 5.3 `CountryState`

关键字段：人口、粮食、财政、领土、科技、税率、劳动力、政策、建筑、在建项目、外交、军队、城市列表、稳定度、缺粮统计、战争疲劳、贡金账本 `levies`。

`levies` 是「本国欠下的占领贡金」列表，每条形如
`{"to": 收缴国, "building": 建筑 id, "share": 0.02, "days_left": 180}`：
被夺一栋带建筑的格子就在**被夺方**名下记一条，`core/state.py::tribute_transfers` 按收缴方分组、
合计封顶 `capture_tribute_cap`，`tribute_base` 用 `last_economy` 的税 + 矿毛收入定价。
账本只由 `Military._tribute`（开战时）与 `Military.tribute_daily`（倒计时与到期）写入，
结算、`summary()`、观测与 `/api/tile_strikes` 都读这同一份账。

国家人口与城市人口必须满足：

```text
country.population == sum(city.population for city in country.cities)
```

破坏后立即调用 `state.sync_city_population(country)`，或按城市权重分配变化。

### 5.4 `CityState`

```text
city_id, name, center, population, focus, tiles, founded_day
zones: dict[(col,row), zone]
managed_by: "ai" | "manual"
livability
food_output / food_need / food_balance / food_loss
budget, local_revenue, local_upkeep, local_net
siege: {"attacker", "armies", "progress", "started_day"} | None
```

- `city_id` 创建后保持稳定（如 `ELAR-C1`、`ELAR-C2`）。
- `zones` 必须覆盖全部城域地块；旧存档加载时会用 `default_zone` 补全。
- 城市中心**不会**被一场会战夺走：中心格只开围城账，账满才整座城（地、人、建筑）一起易主；城域其它地块仍可被会战占领并转移。见 §7.9。
- `siege` 是这座城**唯一**的围城状态：只有被围的那一方存这一条，围方不存副本。面板、AI、结算一律经 `core/state.py::siege_ledger` 派生数字，不许再存第二份进度。旧存档没有这个键 → `None`。
- 城域扩张默认 5 格（`config.expansion.city_tiles`），新增城市继承来源城市的 `managed_by`。

### 5.5 核心不变量

1. **静态世界不可变。** 不要修改 `WorldStatic.data/tiles`；不要无意覆盖 `output/world_2000BCE.json`。
2. **规则引擎是唯一动态写入口。** AI/UI 只提交动作；城市 AI 市长也通过 `engine.apply_action`。
3. **动作原子性。** 校验失败应抛 `ActionRejected`，不能留下半完成状态；涉及资源扣除时先校验后扣款。
4. **人口守恒。** 全国人口等于城市人口之和。
5. **确定性。** 相同种子 + 相同动作日志 + 相同规则，必须得到相同 `fingerprint()`。
6. **观察隔离。** Agent 拿到的是深拷贝；它不能借观察对象修改活状态。
7. **玩家国家跳过 AI。** `scheduler.controlled_by_player` 中的国家不参与自动规划。
8. **手动城市不被 AI 覆盖。** `CityManager` 只处理 `managed_by == "ai"` 的城市。
9. **所有需要复盘的动态变化都要进入动作日志或事件日志。**
10. **前端不持有权威状态。** 每次操作后重新拉取 `/api/state`。

---

## 6. 规则引擎与动作协议

### 6.1 动作集合

公共动作（`core/actions.py`）：

| 动作 | 关键字段 | 说明 |
| --- | --- | --- |
| `BUILD` | `tile:{col,row}`, `building` | 在己方地块开始建造 |
| `ALLOCATE_WORKERS` | `agriculture`, `mining`, `construction` | 三项和为 1 |
| `SET_TAX` | `rate` | 0 到 `maximum_tax_rate` |
| `SET_RESEARCH` | `field` | 三条科技线之一 |
| `SET_POLICY` | `priority` | 六种国家政策 |
| `SETTLE` | `tile:{col,row}` | 相邻无主陆地开拓，建立约 5 格新城 |
| `TRADE` | `operation`, `target`, `offer_food`, `request_treasury` 或 `proposal_id` | `PROPOSE/ACCEPT/REJECT` |
| `DIPLOMACY` | `operation`, ... | `GIFT/PROPOSE_PACT/ACCEPT_PACT/REJECT_PACT/DECLARE_WAR/PROPOSE_PEACE` |
| `MILITARY` | `operation`, `unit`, `strength`, `army_id`, `target`, `tile` | `RECRUIT/MARCH/ATTACK/SET_RECRUITING/SET_RECRUIT_UNIT`；`ATTACK` 的 `tile:{col,row}` 可选，给了就打那一格，省略则自动挑缺口。夺格成功时 `effects.captured` 列出易主格，`effects.tribute` 给这一格建筑立下的贡金字段（`label/share/days/daily/cap/owed`），无建筑或免贡设施则为 `null` |
| `SET_CITY_FOCUS` | `city_id`, `focus` | 城市方针 |
| `SET_CITY_ZONE` | `city_id`, `tile`, `zone` | 城市地块分区 |
| `SET_CITY_GOVERNANCE` | `city_id`, `managed_by` | `ai/manual` |
| `SET_CITY_BUDGET` | `city_id`, `budget` | `austerity/balanced/investment` |
| `NO_OP` | 无 | 内部空操作，LLM 失败回退也用 |

约束：

- 单次 AI 决议最多 `max_actions_per_decision`（默认 4）个动作。
- LLM 决议必须原样带回 `country_id` 与 `world_day`，否则整份决议被拒绝。
- 动作字段是严格白名单，出现未知字段直接 `INVALID_SCHEMA`。
- 数值必须是有限数，布尔值不算数字。
- 所有拒绝都返回 `ActionResult(reason_code, reason_text)`，不会让服务崩溃。
- **每一条命令——成的或败的——都在 `GameState.receipts[actor]` 留下一行**（`action_id` 与 `action_log` 同源，`accepted`/`reason_code`/`reason_text` 照抄，`action` 是规范化后的参数），最多保留 `RECEIPT_LIMIT`（12）条；面板与 `/api/state` 读的就是这一份，前端不再自己猜成败。`PROPOSE_PACT` / `TRADE PROPOSE` 成功时 `effects.proposal_id` 给这份文书一个编号，之后的 `ACCEPT_PACT` / `REJECT_PACT` / `REJECT` 都按这个 id 走。

### 6.2 示例

```json
{"type":"SET_TAX","rate":0.12}
{"type":"BUILD","tile":{"col":13,"row":10},"building":"FARM"}
{"type":"SET_CITY_ZONE","city_id":"ELAR-C1","tile":{"col":14,"row":10},"zone":"farm"}
{"type":"SET_CITY_BUDGET","city_id":"ELAR-C1","budget":"investment"}
{"type":"TRADE","operation":"PROPOSE","target":"MARIS","offer_food":20,"request_treasury":3}
{"type":"MILITARY","operation":"RECRUIT","unit":"ARCHER","strength":2}
```

### 6.3 新增一个动作的检查清单

1. `core/actions.py`：加入 `PUBLIC_ACTIONS`。
2. `validate_action` 顶层 `_keys(...)` 加入新字段名。
3. 增加动作分支，返回规范化后的 dict。
4. `core/rules.py::_execute` 增加业务处理，失败用 `ActionRejected("CODE", "中文原因")`。
5. 需要资源变化时先校验、后扣除；写 `state.record(...)`。
6. `agents/llm_adapter.py` 的 system prompt 加入动作说明。
7. 在 `tests/test_actions.py` 增加成功、非法参数、越权/外部对象、原子性测试。
8. 如前端需要入口，在 `dashboard.html` 增加 UI，并只通过 `/api/action` 提交。
9. 跑完整测试 + 365 日 Mock，确认 AI 不会滥用。

---

## 7. 模拟子系统

### 7.1 日历与天气

- 365 天无闰年；天文纪年无 0 年；显示为“公元前/公元”。
- 开局年份支持 **BC 800 – AD 500**，决定起始时代与三条科技线基线。
- 天气由 `WeatherModel` 基于月气候态 + 随机异常生成；RNG 状态写入存档。
- 月末生成降雨/温度异常，驱动洪水/干旱灾害，灾害会实际扣粮。
- 气象是游戏模型，不是科学天气预报/GCM。

### 7.2 国家经济

每日：

```text
粮食产出 = 地块基础产出 × 天气 × 道路 × 城市方针 × 分区 × 邻接 × 宜居
           × 劳动力 × 经济线科技 × 政策 × 税收压力 × 稳定度 × 粮仓类建筑
矿业收入 = 地块矿业基础 × 道路 × 方针 × 分区 × 邻接 × 宜居
           × 劳动力 × 科技线 × 政策 × 税收 × 稳定度 × 工坊类建筑 × 矿山边际效率
税收     = 人口 × 税率 / divisor × 政策 × 市场 × 贸易协定 × 城市方针/分区 × 经济线税收加成
净收入   = 税收 + 矿业 - 建筑维护 - 城市公共服务支出
```

- 粮食有仓容上限，溢出浪费。
- 缺粮会按缺口比例造成人口损失；损失优先落到缺粮城市。
- 稳定度受税率、粮食安全、住房拥挤、城市化、公共建筑、政策、政治线影响，并反向影响产出。

### 7.3 城市粮食物流

- 每城单独计算 `food_output`、`food_need`、`food_balance`。
- 盈余上缴全国储备、缺口从全国储备调入，双向产生物流损耗。
- 道路降低损耗率（有上限）。
- 依赖调入粮食的城市，人口增长乘 `import_growth_penalty`（默认 0.85）。
- 全国饥荒时，优先扣减粮食缺口城市的人口。

关键配置：`city_logistics`。

### 7.4 城市地方预算

- 城市税收与矿业收入按人口、方针、分区邻接、地块矿业权重拆分。
- 建筑维护和公共服务支出计入城市本地账本。
- 公共服务支出真实计入全国财政净收入，不是免费加成。
- 三档预算：

| 预算 | 服务支出 | 宜居目标 | 增长 |
| --- | --- | --- | --- |
| `austerity` 紧缩 | ×0.6 | -6 | ×0.92 |
| `balanced` 均衡 | ×1.0 | 0 | ×1.0 |
| `investment` 投资 | ×1.5 | +6 | ×1.08 |

关键配置：`city_service_cost_per_1000`、`city_budgets`。

### 7.5 人口、住房、宜居度

- 住房容量来自城域格数、道路、建筑、住宅分区。
- 人口按月增长，受住房余量、宜居度、粮食进口依赖影响。
- 宜居度目标由住宅/商贸/学术、工矿污染、神庙/市场/粮仓、预算、拥挤共同决定。
- 宜居度影响城市产出倍率、人口增长和全国稳定度。
- 分区邻接：农业连片、工矿聚集、商贸靠中心、学术靠住宅。

关键配置：`housing`、`city_zones`、`stability`。

### 7.6 科技与时代

三条线（内部为兼容旧存档保留旧键）：

| 线 | 内部键 | 主题 | 主要效果 |
| --- | --- | --- | --- |
| 科技 | `bronze_metallurgy` | 矿冶、工程、军事装备 | 矿业、军队战力 |
| 政治 | `writing` | 文字、哲学、法典、官僚 | 研究、稳定度 |
| 经济 | `agriculture` | 农业、仓储、货币、贸易 | 粮食、税收 |

- 每条线 6 级，节点名称按时代递进，例如：石器与用火 → 青铜冶炼 → 铁器锻造 → 工程学 → 机械装置 → 军工体系；符号与信仰 → 早期文字与基本哲学 → 城邦法典 → 帝国官僚；定居农业 → 仓储与交换 → 灌溉与市场 → 货币经济。
- 研究点数来自人口/城市方针/分区/书库/政策/稳定度。
- 研究升级到下一级需要 `research_base_threshold × 当前等级`。
- 时代由 `min(三条科技线)` 与开局时代共同决定；不会因开局年代倒退。
- BC800 开局为时代 II 青铜时代盛期，三条线基线 2：青铜冶炼、早期文字与基本哲学、仓储与交换自动点亮；文明类型再给一条线 +1 起始等级。
- 晚期开局不会直接触发知识胜利（知识胜利要求三线满级 6）。
- 科技面板不重复维护解锁清单：节点上的「设施/兵种」标签和「军工解锁」页签都由 `buildings`、`units` 的 `required_technology` 反查生成（`dashboard.html` 的 `techUnlockIndex`/`renderTechIndex`），`tech_tree.levels.unlocks` 只作为建筑侧双向校验字段保留。

关键配置：`tech_tree`、`eras`、`research_*`、`politics_stability_per_level`、`economy_tax_bonus_per_level`。

### 7.7 建设

- 建筑有造价、工期、地形限制、科技前置、全国上限。
- 建设进度受 construction 劳动力与政策影响。
- 完成后进入 `country.buildings`，实际参与经济/住房/稳定度结算。
- 奇观全国唯一，是后期财政出口。

关键配置：`buildings`、`building_upkeep_per_day`、`workers_baseline`。

### 7.8 外交

- 各国两两关系 -100..+100。
- 赠礼提升关系；贸易成交提升关系；拒绝降低关系。
- 关系达到门槛才能提议贸易协定；协定需对方接受，待回应提案会过期。
- 有效贸易协定给双方税收 +5%。
- 战争会取消双方有效协定并把关系降到 -100。
- 和平通过提案与接受完成。
- **外交页读到的是终局，不是半张脸**：`core/state.py::diplomacy_ledger(country_id, proposals, trades, day, limit=LEDGER_LIMIT)` 把 `diplomatic_proposals` 与 `trades` 合成一份账，按 `incoming`（对方来信）/ `outgoing`（本国发文）两列（各取最近 `LEDGER_LIMIT` = 12 行，排序 `(非待回应优先降级, -created_day, proposal_id)`），每行由 `proposal_row` 给 `proposal_id/kind/pact/direction`、`counterparty`、`sender`/`target`、`status` 与中文 `status_label`、`created_day`、`resolved_day`、`days_left`（只在 `PENDING` 时是剩余天数，否则 `null`）、贸易行另带 `offer_food` / `request_treasury`；汇总字段是 `pending` / `decided`。终局共六种：`PENDING` 待回应、`ACCEPTED` 已接受、`ACTIVE` 生效中、`REJECTED` 已拒绝、`EXPIRED` 已过期、`BROKEN` 已作废；被拒与到期的提案**留在账上**，不再只出现在事件日志里。`counterparty` 由收发两端反推、不是读者本国——`test_the_ledger_names_the_other_side_not_the_reader` 钉双向。
- **过期结算在 `step_day` 入口、`day_index` 自增之前**：协定提案由 `Diplomacy.expire_proposals()` 判过期，`resolved_day` 保持 `None`（面板显示「到期未回应」）；贸易行由账本当场推断，因此带 `resolved_day`。到期当日账上仍是 `PENDING` 且 `days_left = 0`，次日才翻成 `EXPIRED`——测试按这两天分别断言，别只测一边。

关键配置：`trade_expiry_days`、`military`、政策参数。

### 7.9 军事

- 地块页先选驻扎、攻打或会师，再在独立弹窗选择军队。`MILITARY/DISPATCH` 是一条可回放的批量动作，内部按选择顺序校验每道军令。局部拒绝附军队和原因。`/api/dispatch_preview` 为选中的进攻列返回逐支到达日数和预计集结系数。
- 已建成兵营每日各贡献 1 点进度和 0.01 财政训练费，同天只训练一支有兵且开募的在营军队。`training_days` 独立于主动招募单价。`barracks_report` 下发 `training_army_id`、`progress_per_day`、`days_left` 和 `unit_rooms`。`last_dispatch` 保存会师抽兵账。成兵的 `upkeep_food` 只扣粮，缺粮依日口粮从高到低遣散。
- 停摆与拒令码：`PAUSED` 关闭募兵，`NO_GARRISON` 无有兵驻军，`INSUFFICIENT_WAGE` 缺当日训练财政，`INSUFFICIENT_FOOD` 缺粮，`UNIT_LIMIT` 该军兵种额度满，`CAPACITY_LIMIT` 驻格容量满，`ALREADY_GARRISONED` 驻格冲突。停摆公告按军队与原因限速 60 日。
- 会师可按兵种拆分，派空时记录墓碑。抵达重算兵种额度和格容量。宿主离开时允许在空闲合法格驻扎，若落点已被占或不可驻扎则撤回并留回执。旧存档同格多军保留最早驻军，其余转移到最近空闲己方格，保留原兵力与番号。
- 攻守双方都吃地形；胜败确定后计算损耗，再按最大余数法分配。首都和普通城的围城仍逐日使用封锁圈实时兵力。单军不能获得满额集结，相邻地块的多支军队仍能协同。

### 7.10 评分与胜利

综合评分四条路线：

- 繁荣：人口、存粮、市场、工坊
- 知识：科技总等级、书库、大书库
- 疆域：领土、前哨、奇观
- 安定：稳定度、住房余量、神庙

正式胜利条件（`config.victory_conditions`）：

| 路线 | 条件 |
| --- | --- |
| 知识 | 三条科技线全部达到 6 级 |
| 繁荣 | 人口 45k、财政 600、存粮达到仓容 80% |
| 疆域 | 至少 26 格，且达到对手平均领土 1.5 倍；至少 3 城 |
| 安定 | 稳定 80、城市平均宜居 62、人口 30k |

- 实时服务每天检查；达成后记录 `victory` 事件、暂停并进入结算。
- 继续本局后不会重复公告同一场胜利。
- 未达成正式胜利时，结算按综合评分显示临时领先。

---

## 8. 城市系统专章

城市是本项目相对普通国家模拟器的核心差异，改动时要特别小心。

### 8.1 城市实体

- 每国初始一座都城；`SETTLE` 建立新城（默认 5 格连通城域，受地形限制）。
- 新城从来源城市迁入人口，默认分区由 `default_zone` 生成。
- 新城继承来源城市的托管状态；AI 国家的新城默认 AI 托管。
- 建城条件同时校验：目标为陆地且无归属、与本国现有领土直接相邻、新建城市数 < `expansion.max_outposts`、人口 ≥ `expansion.minimum_population`、财政 ≥ `expansion.treasury_cost`。地块面板会逐项显示“已满足 / 未满足”，全部满足后“建城”按钮才可用。
- AI 边疆办公室每 120 日检查一次：新城数 <4、人口 ≥10,000、财政 ≥ 开拓费+20 时，选择相邻无主陆地执行 `SETTLE`；玩家接管国家不自动开拓。
- 玩家点击己方领土相邻的无主地块，在右侧“建立新城”；国家侧栏显示可开拓边疆格数和已建新城数量。
- 双击大地图上的城市标记，会直接打开该城市的二级规划地图；也可从一级“城市”页进入。
- 城市人口独立，但全国人口始终等于城市人口之和。

### 8.2 城市方针

`balanced/growth/industry/commerce/scholar`，影响粮食、矿业、税收、研究乘数。配置在 `city_focuses`。

### 8.3 分区与邻接

`unzoned/residential/farm/industry/commerce/scholar`，配置在 `city_zones`。

邻接规则在 `core/society.py::zone_adjacency`：

- 农业：相邻农业 +食物
- 工矿：相邻工矿 +矿业
- 商贸：靠近中心、相邻商贸 +税收
- 学术：相邻住宅 +研究

城市局部地图上的金色小点表示该地块当前享有邻接加成。

### 8.4 宜居度

`city_livability_target` 计算目标值，月度向目标靠拢。影响：

- 地块产出倍率
- 人口增长
- 全国稳定度

### 8.5 粮食账本

城市详情显示：本地产出、需求、余缺、调配损耗、自给率。相关字段：

```text
food_output, food_need, food_balance, food_loss
```

AI 市长在自给率低于 95% 时转“人口与农业”方针并扩建农业区。

### 8.6 地方预算

城市详情显示本地收入、服务与维护、结余，并提供三档预算按钮。

- 观察模式只读。
- AI 托管城市由市长自动选择，玩家不能直接改。
- LLM 托管城市由模型在规划周期选择，玩家需收回手动治理后直接改。
- 手动城市才能由玩家调整。
- 预算变化真实影响全国财政、宜居和增长。

### 8.7 城市治理模式

每座城市有 `managed_by`，可取 `ai`、`manual`、`llm`：

- `ai`：规则 AI 市长托管，每个规划周期自动调整方针、预算和分区。
- `manual`：玩家手动治理；玩家可直接改方针、预算、分区，并在城市二级地图中选择地块建造设施。
- `llm`：交给该国已配置的 LLM 国家控制器。城市 AI 市长跳过该城市；LLM 在规划周期读取 `country.cities` 中 `managed_by=llm` 的城市指标，用 `SET_CITY_FOCUS/SET_CITY_ZONE/SET_CITY_BUDGET` 直接规划。
- 切到 `llm` 前必须先把该国 AI 设为模型控制；玩家接管期间 LLM 不会行动，回到观战模式后生效。
- 玩家把城市从 `manual` 切到 `ai` 或从 `ai` 收回 `manual`，都通过 `SET_CITY_GOVERNANCE` 动作完成，并写入事件日志。
- 城市二级地图的“城市建造”与大地图共用 `BUILD` 规则、财政和全国上限；只有玩家 + 手动城市可直接建造。AI/LLM 托管城市可查看，但需先收回手动治理。

### 8.8 AI 市长

`core/city_governance.py`：

- 只处理 `managed_by == "ai"`；`manual` 与 `llm` 都跳过。
- 每 90 日规划一次：方针、预算、目标分区。
- 粮食自给低 → 农业优先。
- 国库紧张且本地亏损 → 紧缩。
- 宜居低或治安/卫生偏低且国库允许 → 投资。
- 卫生紧张 → 优先把工矿地块转为住宅。
- 手动 / LLM 城市完全跳过，不覆盖玩家或模型操作。

### 8.9 城市服务

每座城市维护独立 `security`（治安）和 `health`（卫生），目标值由 `city_service_targets` 计算：

- 治安：神庙、市场、道路提升；工矿分区和住房拥挤压低。
- 卫生：粮仓、灌溉、神庙、市场提升；工矿污染、住房拥挤、缺粮压低。
- 预算档位也会直接修正治安/卫生目标。

阈值与后果：

- `< 45`：紧张；`< 35`：危机。
- 低治安降低税收和人口增长；低卫生降低人口增长。
- 每月危机检查：治安事件损失财政与稳定度；疫病损失人口与稳定度。
- 服务值、目标值和状态显示在城市卡片与城市详情。

字段：`security`、`health`；目标与状态由 `city_static_metrics` 派生为
`security_target`、`health_target`、`security_status`、`health_status`。

### 8.10 新增城市功能的检查清单

1. `CityState` 加字段。
2. `to_dict/from_dict` 加持久化与默认值（旧存档兼容）。
3. `city_static_metrics` 加到前端字段。
4. 若参与结算，更新 `core/economy.py` 对应位置。
5. 若影响 AI，更新 `core/city_governance.py`。
6. UI 城市卡片/详情增加显示或操作。
7. 增加测试：真实结算、存档往返、回放指纹、手动/AI 边界。

---

## 9. AI 系统

### 9.1 规划节奏

- 默认每 90 日一次，由 `RuleEngine.step_day` 在规划日调用。
- `AgentScheduler.build_observation(state, country_id)` 只提供该国可见信息。
- 观测里的军事与外交字段与面板同源：`country.barracks` = `barracks_reports`、`country.recruit_options` = `recruit_options`、`diplomacy.ledger` = `diplomacy_ledger`，AI 读的停摆码、`batch` 报价与提案终局就是结算用的那一份，不再自己推。`receipts` 是给人看的显示账，**不下发给 Agent**（模型要什么自己调用动作，不需要读自己的回执历史）。
- 每个国家的观察是深拷贝；Agent 不能通过观察对象修改活状态。
- 各国决策通过 `asyncio.gather` 并发执行。
- 玩家接管的国家不参与自动规划。
- 单个 Agent 异常会回退为 `NO_OP`，不会中断世界。

### 9.2 规则 AI

`agents/heuristic.py`：

- 各国各有政策/研究/建筑偏好。
- 会接受贸易、评估公平性、赠礼、提议/接受协定。
- 会扩张、建设、研究最低科技线以推进时代、建军和开战/求和。
- 是零成本、确定性的基线，也是 LLM 失败时的安全网。

### 9.3 AI 性格预设

`agents/personality.py` 定义三套共享性格，Mock 与 LLM 都读取国家状态里的
`personality` 字段：

| 值 | 中文 | Mock 倾向 | LLM 倾向 |
| --- | --- | --- | --- |
| `aggressive` | 激进 | 更早建军、更多战争和领土；军队与财政规模最大、稳定度最低；城市优先工矿/投资 | 扩张、军备与威慑优先，少赠礼 |
| `neutral` | 中立 | 均衡扩张、有限军备、协定最多；默认值 | 粮食、财政、军备、外交均衡 |
| `conservative` | 保守 | 不主动宣战、最低军备、赠礼最多；扩张与中立接近，城市按常规基建发展 | 粮食、稳定、防御与外交优先，避免远征 |

- 性格写入 `CountryState` 与动态存档；旧存档默认中立。
- 观察数据 `country.personality` 同时提供给 Mock 与 LLM。
- `mock.units` 是兵种招募优先序：规则 AI 只在已解锁、未满编、买得起的前提下按该顺序选兵种，全军团规模受观测里的 `country.army_cap`（=`field_cap`，随已建成兵营数增长）约束，缺这个键才退回 `military.maximum_strength`；三套性格都拿不到可用兵种时退回 `military.default_unit`。
- `mock.barracks` 是本国**主场**想持有的兵营数：`_building_choice()` 的目标是 `mock.barracks + max(0, 城市数 - 1)`，每多一座城市就多要一座营，因为每营只练 `garrison_capacity` 支固定编制、全国上限又只按营数增长。内容表不再给 `BARRACKS` 设 `max_per_country`，所以这里没有硬顶；开工还要过 `bronze_metallurgy` 前置，再过一道**流量闸门**（v0.43）：`(已建成 + 在建 + 1) 座营的日饷 ≤ (last_economy.treasury_income − military_upkeep) × 0.4`，外加国库仍付得起这一栋的造价。旧口径是 `treasury ≥ 造价 + 日饷 × 30` 的存量闸门——它只保证买得起砖，于是 AI 会一堵一堵地立起养不活的营，正是 §7.9 那批天天停摆的兵营的来路。`run_march_balance.py` 用 `兵营当饭吃 / 无人修兵营` 两档场景调这个旋钮。
- 兵营募兵开关由 `mock` 的钱包与上限驱动：某支驻军营练满 `garrison_capacity`、或全国兵力已达 `army_cap` 时，规则 AI 主动 `SET_RECRUITING False` 停饷，不留着空转（`agents/heuristic.py::_barracks_orders`）。
- `mock.roads` 是筑路欲望（本国想持有的道路格数）：`_building_choice()` 先满足它再排工矿，设 0 即完全不为此筑路。因为行军定价不分道路归属，这一格旋钮直接决定本国军队能在一条命令内打到哪里；立国偏好为 `building: ROAD` 的商贸国家仍会按经济偏好修到 2 条，与性格无关。
- `mock.pact_relation` 必须不低于 `core.diplomacy.MINIMUM_PACT_RELATION`（15）：低于闸门只会在每个规划周期换来一条 `RELATION_TOO_LOW` 拒绝，`tests/test_march_balance.py` 逐档检查。
- `mock.war_relation` 是开战闸门（`relation <= war_relation` 才宣战），所以 `military.peace_relation_reset` 事实上充当停战期：定在 -20 时三档性格在下一个 90 天规划点都仍满足开战条件，想让和平顶得住得把这个值抬到中立档阈值之上。
- 议和闸门不只看疲劳：`agents/heuristic.py::_peace_gate(profile, country, rules)` 在 `mock.peace_exhaustion` 上减去「净贡金 ÷ 本方毛收入 × `capture_tribute_resolve`」并夹在 ±1 比例内，所以被抽贡的国家会更早求和、收贡的会更晚。观测里没有 `tribute` 或净贡为零时闸门退回原疲劳值，`tests/test_capture_tribute.py` 逐档锁住这三条。
- 规则 AI 在 `agents/heuristic.py` 中把性格参数用于建军、扩张、赠礼、协定、宣战、贸易、建筑和劳动力；激进城市在 `core/city_governance.py` 中优先工矿与投资预算；中立/保守按常规城市规则发展。
- LLM 提示词把性格说明放在系统消息最前面，但动作协议和 JSON 结构不变。
- AI 席位页可逐国切换，调用 `POST /api/agents/personality`；不需要重填 API Key。
- 命令行/AI 席位重新配置时，若未显式传性格，保留国家当前性格。

### 9.4 LLM 适配层

`agents/llm_adapter.py`：

- 只支持 OpenAI 兼容的 `/chat/completions` 协议。
- 每家可单独配置 provider、model、base_url、超时、重试、输出上限、输入/输出单价。
- 密钥只从环境变量读取；配置里只写环境变量名。
- 默认要求同时设置调用次数和费用上限，否则拒绝付费调用。
- 记录 `llm_call` / `llm_decision` 事件；Usage 缺失记为 unknown，不当作 0 费用。
- 非法 JSON、超时、429/5xx 会按配置重试；最终失败回退 `NO_OP`。
- 提示词要求只输出 JSON，动作最多 4 个，并额外输出 `diary`：`title`、`mood`、第一人称 `text`。
- 系统提示词把新字段讲清楚，模型才会用它们：`country.barracks`（逐营 `army_id`/`unit`/`progress`/`days_left`/`room`/`raised`/`stall`，含七种停摆码的中文解释）、`country.recruit_options`（`reason` 与引擎同一套闸门）、`diplomacy.ledger`（`PENDING` / `ACCEPTED` / `REJECTED` / `EXPIRED` 与剩余天数）。这四段文案由 `tests/test_recruit_receipts.py` 逐条 needle 断言，改了字段名就要同时改提示词与测试。
- `text` 写公开决策叙事（局势、动机、取舍、下一步），不要求模型暴露逐步推理链。

模型配置示例见 `config/models.example.json`。真实配置复制为本地文件（如 `config/models.local.json`），不要提交密钥。

### 9.5 运行时切换

**简单模式**：只提交厂商预设和 API Key，服务端解析官方默认值：

```json
{
  "country_id": "ELAR",
  "settings": {
    "mode": "openai_compatible",
    "provider_id": "qwen",
    "api_key": "玩家粘贴的本地密钥"
  }
}
```

服务端把 Key 写入该进程的环境变量 `EOS_AGENT_<COUNTRY>_API_KEY`，不会写回响应、存档、日志或页面。重启服务后内存密钥清空。

当前预设（`config/llm_providers.json`）：

| ID | 厂商 | OpenAI 兼容 Base URL | 默认模型 |
| --- | --- | --- | --- |
| `deepseek` | DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| `doubao` | 字节豆包 / 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seed-2-1-pro-260915` |
| `qwen` | 阿里千问 / 百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| `kimi` | 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-auto` |
| `glm` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3` |
| `minimax` | MiniMax | `https://api.minimax.cn/v1` | `MiniMax-M3` |

简单模式同时提供厂商主流模型下拉：DeepSeek Chat/Reasoner；豆包 Seed 2.1 Pro/Turbo；千问 Plus/3.8 Max/3.8 Flash/Turbo；Kimi Moonshot v1 Auto/K3/K2.6；GLM 5.3/5.3 Flash/5.3 FlashX；MiniMax M3/M2.7/M2.7 Highspeed/M2.5。列表来自 `config/llm_providers.json`，简单模式只接受列表内模型；自定义模型走高级模式。

厂商切换但 Key 留空时，服务端拒绝，避免把旧厂商的 Key 发给新厂商。预设模型以各厂商账号实际开通情况为准，可用高级模式覆盖。

**高级模式**：完整自定义：

```json
{
  "country_id": "ELAR",
  "settings": {
    "mode": "openai_compatible",
    "provider": "custom-gateway",
    "model": "custom-model",
    "base_url": "https://gateway.example.com/v1",
    "api_key_env": "EOS_AGENT_ELAR_API_KEY",
    "timeout_seconds": 45,
    "retries": 0,
    "max_output_tokens": 4000
  }
}
```

`GET /api/agents` 同时返回安全的 `providers` 预设列表；页面只显示“是否已配置”，不会返回密钥。Mock 席位不会因为存在全局 `EOS_API_KEY` 而误显示为已配置。

**连接测试**：

```json
POST /api/agents/test
{"country_id": "A"}
```

服务端向当前 Provider 的 `/chat/completions` 发起一次 `max_tokens=8` 的最小请求，返回：

```json
{
  "ok": true,
  "provider": "阿里千问",
  "model": "qwen-plus",
  "status_code": 200,
  "latency_ms": 412.5,
  "message": "连接成功，Key、模型与端点可用"
}
```

失败时返回 HTTP 状态、延迟和截断后的错误信息；不会包含 Key 或请求头。测试结果保存在内存中的 `agent_meta.last_test`，并记录 `agent_test` 事件；Mock 席位忽略历史 LLM 错误。

**调用预算**：正式 90 日规划也受 `CallBudget` 约束。通过 UI 首次保存 LLM 席位时，如果服务没有 CLI 预算，会自动启用 40 次 / $1.00 的本次运行预算；否则会出现“连接测试成功但规划调用 BudgetExceeded”。AI 席位顶部的预算栏可修改：

```json
POST /api/agents/budget
{"max_calls": 40, "max_cost_usd": 1.0}
```

调用上限不能低于已用次数；修改预算会清除旧调用错误的展示。

### 9.6 LLM 统治日记与事件短讯

每次 LLM 规划成功后，`AgentScheduler` 把决策叙事记录为 `llm_diary` 事件，保存在“AI 统治档案”中：

```json
{
  "type": "llm_diary",
  "country_id": "ELAR",
  "day": 180,
  "date": "公元前1999年7月1日",
  "title": "粮食与边境",
  "mood": "谨慎",
  "text": "今年的收成足够……我决定先把税率维持……",
  "actions": [ { "type": "SET_TAX", "rate": 0.12 } ],
  "provider": "deepseek",
  "model": "deepseek-flash",
  "synthetic": false
}
```

- 只对 `OpenAICompatibleAgent` 记录；HeuristicAgent 没有日记。
- 模型没有返回 `diary` 时，用 `reason` 合成并标记 `synthetic: true`。
- 提示词要求现代白话文，禁止文言文、骈文、四字排比和古风腔。
- 日记是观察/审计元数据，不进入 `fingerprint()`；回放不要求重建模型输出。
- **地图气泡不再使用 90 日 LLM 日记**，避免规划日四国同时刷长篇；日记只在档案中回看。
- 地图短讯由 `core/messages.py` 在事件发生时生成 `ruler_message`：战争、战斗、停战、贸易/协定、赠礼、建城、工程、科研、缺粮、动荡、疫病、招兵、灾害等。
- 短讯使用现代白话文模板，40–80 字；每国默认 12 游戏日冷却，危机事件可突破冷却；玩家国家不生成 AI 短讯。
- 气泡状态机：`highlight` 2.2s → `steady` 5.2s → `fade` 0.9s → 归档；鼠标移入立即转常亮，移出 1.2s 后淡出；淡出中触碰会恢复常亮。
- 单击气泡展开完整正文、动作标签和模型信息；关闭后气泡移除，但内容仍保留在事件日志/档案。
- 顶部“AI 日记”打开“AI 统治档案”，可回看日记和事件短讯；载入存档只归档历史，不补弹旧气泡。
- `GET /api/diary?country=ELAR&limit=80` 返回日记/短讯、各国控制器和模型信息。
- 载入存档会保留当前 LLM 席位，不会把已配置模型重置为 Mock。

---

## 10. 存档、回放与确定性

### 10.1 存档格式

动态存档格式：

```json
{
  "format": "project-eos-dynamic-save",
  "format_version": 1,
  "rules_version": "...",
  "world_file": "output/world_2000BCE.json",
  "world_sha256": "...",
  "seed": 2000,
  "day_index": 0,
  "start_astronomical_year": -1999,
  "start_era_index": 1,
  "rules": { ... },
  "countries": { ... },
  "trades": { ... },
  "relations": { ... },
  "pacts": { ... },
  "diplomatic_proposals": { ... },
  "weather": { "rng_state": ..., "anomaly": ..., "month_accumulators": ... },
  "counters": { ... },
  "action_log": [ ... ],
  "event_log": [ ... ],
  "receipts": { "ELAR": [ ... ] }
}
```

- `receipts` 是每个 actor 最近 `RECEIPT_LIMIT`（12）条命令回执，由 `GameState.add_receipt(result, action)` 写入、随存档往返，**不进 `fingerprint()`**：`test_receipts_survive_the_save_and_do_not_move_the_fingerprint` 钉住「载入后回执逐字段一致、指纹逐字不变」。被协议层拒绝的动作只存 `{"type": action_type}`（缺参数字段不写进日志），`accepted/reason_code/reason_text` 照原样存。

存档槽：

- `runs/saves/<id>.json`：状态。
- 每个国家条目带 `levies`（占领贡金账本，`[{"to","building","share","days_left"}]`）；旧存档缺这个键按空表加载，因此 v0.36 之前的 timeline 载入后不会欠任何贡金，也不会因为多了一个字段而改变指纹以外的任何结算。
- 每座城市条目带 `siege`（`{attacker, armies, progress, started_day}` 或 `null`），围城进度就存在被围方这座城市上，随存档往返并进 `fingerprint()`；旧存档缺这个键按 `null` 加载（`CityState.from_dict` 走 `data.get("siege")`），载入后没有任何城莫名被围。`test_the_blockade_survives_a_save_round_trip` 钉住「存档 → 载入 → 再结算一天」与不存档直接跑逐字段一致，`test_an_older_save_without_a_siege_column_reads_as_no_blockade` 钉住旧存档载入后没有任何城莫名被围。
- `runs/saves/<id>.meta.json`：名称、日期、种子、开局年、时代、国家名、玩家国、保存时间。
- 每 30 游戏日自动保存，返回菜单和关闭服务也会保存。
- 载入存档后默认暂停，目标日重置为当前日 +365。
- 载入存档会保留当前 LLM 席位和模型配置；不会静默降级为 Mock。

### 10.2 回放

`core/replay.py`：

1. 读取最终存档与动作日志。
2. 从同一静态世界和种子新建状态。
3. 恢复自定义开局年代。
4. 按 `applied_day` 重放动作。
5. 每日推进，**禁用 CityManager**，因为市长的动作已经记录在动作日志中。
6. 对比 `fingerprint()`。

### 10.3 确定性红线

以下行为会破坏回放/测试：

- 使用未写入存档的随机数。
- 在 `CityManager` 或 UI 中直接修改城市状态，而不经过动作日志。
- 把派生字段写进指纹，但这些字段在回放时无法重现。
- 修改世界静态文件但沿用旧哈希。
- 在 config 合并时重命名已有键却没有迁移。

新增状态时问自己：**回放时能否从动作日志 + 种子 + 规则重新得到它？**

### 10.4 存档兼容

- `GameState.load` 会用默认 `rules.json` 合并旧存档的规则，新增配置键自动补默认值。
- `CityState.from_dict` / `CountryState.from_dict` 对新字段使用默认值。
- 兵种编成是附加式变更：`format_version` 仍为 1。存档没有 `army_units` 时，`core/content.migrate_roster` 用 `military.default_unit` 承接旧的标量 `army_strength`；`max_count` 只约束之后的招募，不会裁剪迁移来的旧编制。
- 破坏性格式变更必须提升 `format_version` 并写迁移或明确拒绝旧档。
- 不要静默删除旧字段。

---

## 11. HTTP API

服务只绑定 `127.0.0.1/localhost`，没有认证，禁止绑定公网。

### 11.1 GET

| 路径 | 作用 |
| --- | --- |
| `/` | 返回 `dashboard.html` |
| `/api/world` | 静态世界压缩载荷（metadata/polities/tiles） |
| `/api/state` | 实时完整快照：日期、国家、城市、事件、建筑、项目、AI、预算、胜利、接管国的逐军队行军报价（含 `mass` / `mass_share`，交战另带 `siege` = `siege_opportunities` 的原样切片与 `siege_marches` = 可开赴城墙的行军报价）、`army_cap` 全国编制上限，每国的 `tribute` 账本（`paid` / `collected` / `daily_paid` / `daily_collected` / `base_income`）、`barracks`（每座在营兵营一行 `barracks_report`）、`recruit_options`（逐兵种的 `reason` / `can_recruit` / `batch`）、`receipts`（本国最近 12 条命令回执）与 `diplomacy.ledger`（提案与贸易的终局账本）；每座城市行带 `siege` = `siege_ledger`（没被围则 `null`） |
| `/api/agents` | 各国控制器、模型、预算、厂商预设、连接测试状态（不含密钥） |
| `POST /api/agents/test` | 对当前 LLM 席位发起一次最小连接测试 |
| `POST /api/agents/budget` | 设置本次运行调用次数与费用上限 |
| `/api/diary` | LLM 国家统治日记；`country`、`limit` 可选 |
| `/api/setup` | 新建世界的默认国家、文明类型、核心格数，以及手绘地图的画笔表与行军成本常数 |
| `/api/saves` | 存档列表与当前存档 ID |
| `/api/tile_strikes?col=&row=` | 单个地块按需结算：这一格上的驻军（不分归属）+ 接管国每支待命军队为攻打这一格付出的天数/成本/集结系数；另带 `building` 与 `tribute`（`label/share/days/daily/cap/owed`），免贡设施或空格则 `tribute=null`；点的是城池中心格时 `city_center=true` 并多带 `siege`（本国真账，没开账则是 `_siege_projection` 的预告账）与 `siege_taken`（这面墙已被别国开着的账，此时 `siege=null`）；自家中心格也在 `OWN_TERRITORY` 那一行带回 `siege`，被围的城在点它的那一格就能看见。带 `reason` 说明为什么不能打。刻意不进 `/api/state`，定价要跑一次最短路 |
| `/assets/*` | 图标/图片静态资源 |

### 11.2 POST

#### `/api/control`

```json
{"action":"pause"}
{"action":"resume"}
{"action":"step"}
{"action":"speed","value":4}
{"action":"take_control","value":"ELAR"}
{"action":"observe"}
{"action":"extend_round"}
{"action":"reset"}
```

用途：暂停/继续/单步/倍速、接管国家、回到观战、延长本轮、重置。

#### `/api/action`

```json
{
  "actor": "ELAR",
  "action": {"type":"SET_TAX","rate":0.12}
}
```

返回：

```json
{"ok": true, "result": {"action_id":"A00000001","accepted":true,...}}
```

只有在已接管该国时才能提交；只能提交给自己接管的国家。

#### `/api/agents`

见 9.4。

#### `/api/new_game`

```json
{
  "countries": [
    {"name":"赤河国","color":"#cc5544","archetype":"river_valley","population":30000},
    {"name":"蓝港国","color":"#4488cc","archetype":"maritime","population":24000},
    {"name":"云山国","color":"#88aa66","archetype":"highland","population":18000},
    {"name":"沙丘国","color":"#ccaa55","archetype":"semiarid","population":26000}
  ],
  "seed": "",
  "start_year": -799,
  "save_name": "我的世界"
}
```

`countries` 可省略，系统自动填默认四国；支持 2–12 个，名称 1–24 字符、颜色唯一 `#RRGGBB`、人口 8000–60000。

带 `draft` 时走手绘地图分支，忽略 `countries`（国家来自 `draft.polities`）：

```json
{
  "draft": {
    "grid": ["oooo....", "...72 列 x 54 行..."],
    "rivers": [[30, 12]],
    "roads": [[42, 20]],
    "polities": [{"name":"赤河国","color":"#cc5544","archetype":"river_valley","population":30000,"capital":[8,20]}],
    "seed": 4242,
    "name": "我的图"
  },
  "start_year": -799,
  "save_name": "手绘世界"
}
```

#### `/api/map_preview`

```json
{"draft": { ... }}
```

返回 `{"ok":true,"report":{...}}`，即绘制面板上的开局校验：陆地/海洋占比、河流与道路格数、地形构成、每国核心格数与 `openable`、`warnings`、`digest`。只做地形与领土推导，不生成气候与水文，所以每画一笔都能立刻回价。

#### `/api/march_preview`

```json
{"draft": { ... }, "from": [8, 20], "to": [40, 20]}
```

用 `core.movement.Movement` 在草稿格上算最短路，返回 `days`、`cost`、`route`、`terrain_mix`、`road_share`、`crossings`、`straight_km`。三种"走不通"分开表达：端点是海洋或两侧被海隔断时 `ok=false` 并给 `reason`；陆路存在但超过 `maximum_order_cost` 时 `ok=true`、`single_order=false`，另给 `segments` 与 `hint`。

#### `/api/saves`

```json
{"operation":"save","name":"手动存档"}
{"operation":"load","id":"save_..."}
{"operation":"delete","id":"save_..."}
```

### 11.3 本地安全边界

- 服务没有登录，默认只监听 loopback；`main()` 会拒绝非 `127.0.0.1/localhost` 的 host。
- 静态资源做了路径逃逸检查。
- 页面会显示模型是否配置，但永远不会返回密钥。
- 若要局域网共享，请使用系统级远程访问/反向代理并自行加认证，不要直接改 host 暴露公网。

---

## 12. 前端 dashboard.html

### 12.1 结构

单文件 HTML + CSS + 原生 JS，无构建步骤。

### 12.1 分层结构

一级导航是 `#primaryRail`，固定在最左侧，所有功能页从这里进入：

| 一级 | 二级标签 / 内容 |
| --- | --- |
| 世界 | 全幅地图；浮动侧栏：列国、详情（国家/地块/排名） |
| 国家 | 概览与政策、城市、世界排名 |
| 城市 | 城市列表（被围标记）、城市规划（局部六边形地图、粮食、预算、服务、分区、建造） |
| 科技 | 总览、科技线、政治线、经济线、军工解锁（兵种/设施解锁阶梯与兵营、战争、围城、行军四张常量卡） |
| 外交 | 邦交关系、待回应提案、外交回执（命令成败 + 提案终局账本） |
| 军事 | 军队、兵营（逐营募兵卡）、战争（「围城战线」逐面墙 + 「可开赴的城墙」逐路开赴）、募兵与军令回执 |
| AI | 席位配置、统治档案 |
| 系统 | 暂停菜单：继续、保存、载入、AI 席位、AI 日记、返回主菜单 |

- 每个功能页都是全屏 deck，使用统一 `.modal` 页面样式，不再是叠在消息台上的浮窗；`modal-head` 带面包屑和“返回世界”。
- 一级切换统一走 `openDeck(name)`；每个 deck 的关闭函数会恢复进入前的暂停状态，并回到世界页。
- 世界页的列国/详情侧栏可收起，工具栏“列国 / 详情”按钮同步状态；选择国家或地块会自动展开详情。
- 通知不再常驻世界页：世界事件生成右下角气泡（最多 3 条，7 秒淡出），顶部“动态”显示未读角标，点击进入通知中心（世界动态 / 在建工程）。
- AI 统治气泡仍锚定首都，与普通世界事件气泡分开。
- AI 席位默认显示简单配置（厂商 + API Key），高级配置折叠在下方。
- 城市详情：局部六边形地图、粮食账本、地方预算、分区编辑、城内地块建造。
- 世界页点任意地块，详情侧栏的 `panel-tile` 先给「驻军」卡（`tileGarrison`）：不分归属列出站在这格的军队，本国那几支带「选为下令对象」，点了就把它设为当前下令军队。敌方地块再多一张「攻打这一格」卡（`tileStrikeCard`）：逐行列出本国每支待命军队的天数/成本/落脚格/到场系数，按钮按 `can_attack` 在「用这支军队攻打」与「开赴并攻打」之间切换，`NOT_AT_WAR` 行换成「向 国家X 宣战」。
- 地块报价走点击而不是轮询：`renderRight` 用 `tileStrikeKey = "col,row,day,接管国"` 判断是否换格/换天/换国，`loadTileStrikes` 收到回价时若 key 已经过期就直接丢弃（和绘制器的 `PAINTER.marchSeq` 同一套思路），下单或宣战后调 `repriceTile` 强制重新报价。文案与数字都来自 `/api/tile_strikes`，前端不自己算路径也不自己估系数。
- 贡金只有四个显示出口，全部读服务端字段：`tileTributeNote`（攻打卡里的预告行）、`tributeLedger`（军事→「占领贡金」卡的账目与净额）、`tributeNetNote`（国家概览的净值行）、`tributeCap`（规则文案里的封顶百分比，取 `STATE.military_specs.capture_tribute_cap`）；份额、天数、每日额都来自 `summary()` 的 `tribute` 与 `/api/tile_strikes` 的 `tribute`，前端一处都不自己算。
- `tileStrikeCard` 只在 `price.tribute` 非空时追加预告行（份额/天数/每日额/对该国已计比例与上限）；免贡建筑换成一句「这类设施不分归属，攻占它不会带来贡金」，格上无建筑或已易主则 `tribute` 为 `null`，整行不出现。军事 deck 的「占领贡金」卡按 `data-mil-sect="war"` 挂在战争区，账目为空时整卡不渲染。
- 战争事件文案在 `formatEvent` 里拼后缀「夺其粮仓，此后 180 日抽其收入 2%」，数据来自战斗事件的 `e.tribute`；规则页军事表新增「占领贡金」「免贡设施」「贡金对战意」三行。
- **围城数字同样只有一个来源**：前端七个切片函数都不自己算账——`siegeTable()`（`STATE.military_specs.siege`）、`siegeRing()`（`blockade_radius`）、`siegeDate(ledger)`（`days_left` → 「N 日城破 / 即日城破 / 圈内没有军队：账不推进」）、`siegeBadge(city)`（城市卡与详情里那颗 `.pill.siege`）、`siegeBar(ledger)`（进度条 + 每日额）、`siegeArmies(ledger)`（圈内军队 chips）、`tileSiegeCard(price)`（地块详情的「围城账」卡，读 `price.siege_taken || price.siege`）。`test_the_panel_prints_the_blockade_straight_from_the_ledger` 用字符串契约钉住这些出口都还在读服务端字段。
- **四个显示出口**：① 世界页点中心格 → `tileStrikeCard` 头部改说「围这一座城」，按钮在「用这支军队开围 / 开赴封锁圈」之间按 `can_attack` 切换，卡里直接印 `tileSiegeCard`；② 军事页「战争状态」卡按每个交战国挂一段 `siegeRows(id)`，它印**两张**表：「围城战线」（数据 `quotes[id].siege`，五列表：被围城池 / 封锁圈军队 / 围城进度 / 预计城破 / 下令）`taken_by` 那一行整行灰掉、写「被 谁 抢先围住」且不给按钮；「可开赴的城墙」（数据 `quotes[id].siege_marches`，五列表：城墙 / 开赴的军队 / 行军报价 / 抵达之后 / 下令）把 `days_left` 明写为**抵达之后**几天城破，按钮 `data-siege-march` 下的仍是同一条 `ATTACK + army_id + tile=中心格`；两张表都空时才合印一条 hint；③ 城市列表与城市详情读 `city.siege`（`siegeBadge` + `.siege-strip`，详情的 `#citySiegeNote` 多一句解围办法）；④ 科技页「军工解锁」的 `siegeStats` 卡把七个参数与城墙的造价/工期/科技门槛原样抄自 `STATE.military_specs.siege` 与 `STATE.building_specs.WALL`，`test_the_rules_page_prints_the_siege_table_from_config` 逐键断言这里没有写死的数字。围城令一律 `data-siege-city` / `data-siege-march` → `submitPlayerAction({type:'MILITARY',operation:'ATTACK',target,army_id,tile:中心格})`，坐标与天数都来自服务端那一行。
- 地图上的围城：`render()` 给每座 `city.siege` 的中心格画两圈——虚线圈用围方国家色表示封锁圈，亮黄实弧按 `progress / threshold` 表示已经啃掉的墙，图例随之多出「围城封锁圈（亮弧=已啃的墙）」一行（只有真在被围时才加）。
- **边警的三个出口，一份账**：`STATE.incoming_threats`（快照里按受害者分组的行）同时喂三处——① 地图 `drawThreatMarkers(scale)`：`threatColumn(row)` 用 `army_id` 找回那支军队的 `march.route`，红色 `#ef6b5e` 虚线只画**还没走完**的那一段，目标格套 `hexPath(..., 1.04)` 红圈，右上角小圆徽章印 `days_left`，`capital` 为真再叠一个 `⚠`；屏幕上那个国家满不透明，别国的警报降到 `.3/.55`，因为「邻国被入侵」是背景而「我们被入侵」是警铃。② 左侧滚动栏 `#worldThreats`（`renderThreats()`）逐行 `threatCard(row)`，写「还剩 N 天行军 · 抵达于 <日历日>」——天数与日期都直接印引擎字段，浏览器不重新计时。③ 军事按钮右上角的 `#militaryDot`（`updateMilitaryDot()`）计数并在 >9 时写 `9+`，`title` 给最快一支的倒计时与抵达日。三处都过 `threatRealm()`，它**优先跟着屏幕上选中的国家**，所以小红点、左栏与军事页的「边警」卡不可能一个说 0 一路、另一个说 1 路。
- **同一张卡的其余落点**：列国列表每行挂 `.tag.threat`「警 N」（`renderCountries()` 读 `STATE.incoming_threats[p.id]`）；世界页点被盯上的那一格，详情侧栏在驻军卡之前插 `tileIncomingCard(tile)`（`tileIncoming` 按格过滤全棋盘的行）；军事页在「战争状态」之前是一张 `data-mil-sect="war"` 的边警卡，两行汇总（在途敌军/最快一支、打都城/落在无守军缺口）加逐行 `threatCard`。卡片本身是按钮，`bindThreats(root)` → `focusTile(col,row)` 选中那一格并打开详情。`threatSignature()` 进 `render()` 与 `renderIfChanged()` 的缓存键，所以「刚刚下了一道入侵令」当帧就重画，不用等下一次轮询。
- **被吞并的国家从面板上消失，但留名字**：所有以世界名单为底的列表（列国、地图图例、外交行、都城星标循环）都走 `livePolities()` = `WORLD.polities ∩ STATE.countries`；`renderCountries()` 末尾用 `STATE.eliminated` 追加一行「已被吞并 N 国 · 国名（覆灭于第 D 日）」；`healSelection()` 在选中国被吞掉的当帧把视角迁回接管国，`renderRight/renderMilitary/renderDiplomacy` 首行 `if(!c)return;`。灭国当日不会出现「面板还列着一支已经不存在的军队」，也不会抛 `undefined` 异常。
- **围城事件文案**：`formatEvent` 认 `siege_started` / `siege_joined`（圈中几支、有没有城墙、约几日城破）、`siege_refused`（一座城同时只有一方围得）、`siege_lifted`（`peace` / `ring_empty` / `captured` 三种中文标签 + 进度停在哪儿）、`city_fallen`（格城域 / 人口 / 缴到的建筑 / 贡金 / 败方稳定度）；`receiptDetail` 在 `ATTACK` 的 `tile` 是中心格时把「围 城名」写进回执那一行。
- **点兵营那一格就能选兵**：世界页点本国已建成的 `BARRACKS` 格，详情侧栏由 `tileBarracksCard(tile, owned)` 渲染「兵营募兵」卡，内容是 `barracksPanel(report, options, isPlayer)` 那一套：这一营由哪支军队领饷（`army_id`）、正在练的兵种（`unit` / `label`，`preferred` 未解锁时回落默认兵种）、`progress/price` 进度、按当日军饷算出的「约 N 日练成」（`days_left`）、`strength/capacity` 与剩余名额 `room`、`raised`，以及 `stall` + `stall_label`（中文文案由服务端 `RECRUIT_STALL_LABELS` 给，前端不自己拼）。三颗按钮都只下单、不改状态：`data-camp-target` → `SET_RECRUIT_UNIT`、`data-camp-recruit`（`batch` 由服务端算好）→ `RECRUIT`、`data-camp-toggle` → `SET_RECRUITING`。军事页 `data-mil-sect="barracks"` 的「兵营募兵」卡把 `barracks_reports` 逐营渲染成同一张卡，并汇总「N 座停摆」。
- **成不成功读回执**：`receiptFilter(rows, types)` 取本国账目切片（军事页只要 `MILITARY`，外交页要 `DIPLOMACY` / `TRADE`），`receiptCard(rows, emptyText)` 逐行显示 `accepted` / `reason_code` / `reason_text`，`receiptDetail(action)` 展开规范化后的参数。面板不猜成败与原因：这份文本就是 `RuleEngine.apply_action` 记下的那一条（`GameState.receipts`，最近 12 条）。
- **外交页读到终局**：`data-diplo-section="receipts"` 那一节把 `dip.ledger`（即 `diplomacy_ledger`）的 `outgoing` / `incoming` 用 `ledgerRow(row)` 逐行列出，`pending` / `decided` 计数直接印在 `.sect` 头部；`status_label` 也是服务端给的中文，前端只切片、不自算状态。
- 主菜单的「手绘地图」进入 `#painterScreen`（不在一级导航里）：左列画笔/工具/国家，中间六边形画布，右列行军耗时与开局校验。宽屏是三列，`max-width:1180px` 以下自动把校验卡换成整行。
- 绘制器不自己算行军：每笔之后 180 ms 防抖请求 `/api/map_preview`，悬停/测距 70 ms 防抖请求 `/api/march_preview`，并用 `PAINTER.marchSeq` 丢弃已过期的回价。
- 绘制器复用主地图的 `hexPath(x,y,r,c)`、`cx/cy` 与投影，六边形几何只有一份；`resize()` 会重新 `painterFit()`。
- 「用这张地图开局」按钮在校验未通过时禁用，按钮文案直接说明原因（未落都 / 地图问题 / 等待回价）。

### 12.2 状态流

- `STATE` 保存 `/api/state` 快照，`WORLD` 保存 `/api/world`。
- `poll(force)` 每 500ms 拉取一次；`renderIfChanged()` 用关键字段避免重复重绘；`syncDiaryBubbles()` 与 `syncNoticeBubbles()` 分别处理 AI 气泡和世界通知。
- 所有玩家操作调用 `submitPlayerAction()` → `/api/action` → 再 `poll(true)`。
- `showOnlyScreen(name)` 切换主菜单/载入/新世界/游戏壳。
- URL 参数便于调试：`?play=latest`、`?play=<save_id>`、`?tech=1`、`?diplo=1`、`?mil=1`、`?city=1`、`?citymap=1`、`?layer=political`。

### 12.3 地图

- Canvas 绘制六边形，支持拖动、缩放、选中地块。
- 图层：卫星、政治、海拔、降水、资源、交通。
- 卫星图层叠加所有国家的领土色与边界；v2 世界会先绘制 576×432 连续地形纹理，再叠加 144×108 六边形网格。政治图层显示更强的国家染色。
- 河流由 `buildRiverChains()` 按下游关系构建从河源到河口的连续链，`sampleRiver()` 用 Catmull-Rom 采样并加入低频横向摆动，`drawRiverNetwork()` 分层绘制河岸、水体和细高光；河宽从源头向下游渐宽。
- v2 河流按 `river_cells.depth_m` 填充河格，颜色随水深变深；道路按 `road_cells` 填充占格道路/桥梁，不再绘制中心线。v1 世界仍回退旧的弯曲河道和道路线稿。
- `shortestLandPath` 必须使用 `landNeighbors`（只返回陆地邻格）；不要与全局 `hexNeighbors(col,row)` 混用，否则道路寻路会全部失败。
- 国家颜色来自世界 polities；城市、设施图标、道路、施工进度环叠加。
- 设施图标通过 `BUILD_ICON` 映射到 `/assets/icons/*.png`；图标为透明背景 320×320。
- v2 地形纹理由 `/api/terrain?seed=<seed>` 提供，文件默认位于 `runs/worlds/`，不进入 Git。
- 道路在卫星/政治/交通图上始终画成连续线稿，不使用设施图片。

### 12.4 城市视图

- 城市卡片：人口、住房、预算、粮食自给、余缺、宜居度。
- 点击进入局部地图，点击地块查看分区/邻接，手动城市可改分区。
- 右侧粮食账本、地方预算、AI 市长说明。
- AI/LLM 托管城市只读；玩家需先“收回手动治理”。

### 12.5 美术资源

`tools/generate_transparent_icons.py` 读取本机 GPT Image Studio 的 `settings.json`（必要时从其 `keySource` 的 `api_providers.json` 取 OfoxAI key），调用 `gpt-image-2.5-flare` 生成 1024×1024、low quality 的单体图标：

```bash
.venv/Scripts/python tools/generate_transparent_icons.py --only farm --force
.venv/Scripts/python tools/generate_transparent_icons.py --force
```

- 请求使用 `background: transparent`；若供应商返回白底，脚本会从边缘泛洪去背、羽化 alpha、按可见像素裁切并居中到 320×320。
- 原始图保存到 `assets/raw/transparent/`，成品保存到 `assets/icons/`。
- 密钥只存在于脚本进程内存，不打印、不写入仓库。
- 道路例外：`road.png` 不参与地图和建造入口，统一使用 CSS 线稿/Canvas 道路。

### 12.6 前端改动规范

1. 不要在前端计算权威资源；只显示服务端字段。
2. 提交动作必须走 `submitPlayerAction`，不要直接改 `STATE`。
3. 新字段先加进 `summary()`/`city_static_metrics()`，再在 UI 读取。
4. 文本使用中文，保持现有视觉密度；图标沿用透明 `assets/icons`。
5. 大改后至少做一次浏览器冒烟：主菜单 → 进游戏 → 八个一级页 → 各二级标签 → 城市视图 → Esc → 回菜单。
6. 新增一级页时，同时补 `rail` 按钮、`openDeck()` 分支、关闭函数和 `setRail()` 状态。
7. `.build-option` 是 `38px 1fr auto` 三列网格，只放一个纯文本子元素时必须加 `solo`（`.build-option.solo{grid-template-columns:1fr}`），否则整段文案会被压进 38px 图标列，中文逐字竖排。

---

## 13. 配置系统

`config/rules.json` 是主要平衡入口。修改后重启服务；旧存档会合并新默认配置。

内容表在进入运行时前先过 `core/content.py::validate_content(config, world_biomes)`：`GameState.new` 和 `GameState.load` 都会调用，并把当前世界文件里真实出现的群系集合作为第二个参数传入——`movement.biome_cost` 必须覆盖这些陆地群系，否则一张"配置能加载、行军到那格就 KeyError"的表会在建局时直接被拒。任何不一致（未知克制、悬空建筑解锁、缺键、越界概率）直接抛 `ContentError`。`tech_tree`、`eras`、`research_fields`、`buildings`、`units` 是内容键，读档时一律用当前 `rules.json` 覆盖存档副本。

主要分区：

| 键 | 作用 |
| --- | --- |
| `rules_version` | 规则版本标识 |
| `planning_interval_days` | AI 规划间隔（90 日）；被 `Military.alarm_watch()` 打断——自家城墙正在被啃、或有一支军队的命令抵达时会开围的国家，当天就议事一次 |
| `max_actions_per_decision` | 单次 AI 动作上限 |
| `expansion` | 前哨上限、费用、人口门槛、新城格数、AI 扩张间隔/财政储备 |
| `food_per_1000_people_per_day` | 人均粮食消耗 |
| `biome_food_yield` | 各生态粮食潜力 |
| `base_food_per_tile` / `farm_food_bonus` | 农业产出 |
| `base_mine_per_tile` / `mine_*` | 矿业产出与边际递减 |
| `road_production_bonus` | 道路产出加成 |
| `treasury_population_tax_divisor` | 人口税基 |
| `high_tax_*` | 高税惩罚 |
| `monthly_population_growth_rate` | 月人口增长 |
| `food_storage` | 仓容 |
| `housing` | 住房 |
| `stability` | 稳定度 |
| `social_events` | 月度社会事件 |
| `weather` | 天气/灾害 |
| `buildings` | 建筑定义 |
| `units` | 兵种定义：战力/防御/编制上限/造价/日维护/科技前置/克制/地形 |
| `city_zones` | 分区 |
| `city_focuses` | 方针 |
| `city_logistics` | 城市粮食物流 |
| `city_service_cost_per_1000` | 城市服务基础成本 |
| `city_budgets` | 城市预算档位 |
| `city_services` | 治安/卫生目标、阈值、事件概率与损失 |
| `victory_conditions` | 胜利条件 |
| `tech_tree` / `eras` / `research_*` | 科技与时代 |
| `policies` | 国家政策 |
| `military` | 编制下限 `maximum_strength`、`recruit_batch_max`、`default_unit`、战损率、`defense_bonus_flat`、`expedition_factor`、冶金加成、随机区间、占领与疲劳参数、`peace_relation_reset`、`strike_columns`（一场战争同时报价几路）、`mass_radius`（缺口几格内算到场）、`minimum_mass_share`（单独一支军队的战力保底）、`barracks` 子表（造价/工期/日饷/营内固定编制 `garrison_capacity`/默认兵种/进度封顶；**没有** `max_per_country`） |
| `movement` | 真行军定价：`daily_budget`（单日可花的成本）、`road_cost`（道路格成本）、`hostile_cost`（踏入别国一格的减速附加，v0.43 起真的进结算）、`maximum_order_cost`（单条命令上限，60 点；超了回 `ORDER_LIMIT` 并报价实需）、`biome_cost`（逐陆地群系成本） |
| `trade_expiry_days` | 提案过期 |
| `workers_baseline` | 劳动力基准 |

### 13.1 新增建筑

1. `config/rules.json` 的 `buildings` 加定义（造价、工期、地形、科技前置、上限、维护）。
2. `core/economy.py` / `core/society.py` 加实际效果。
3. `dashboard.html` 的 `BUILD_NAMES`、`BUILD_ICON` 加显示。
4. `assets/icons/` 放入图标，或先用文字占位。
5. 增加测试：建造、前置、上限、完成后的真实效果。

### 13.2 新增分区

1. `city_zones` 加定义（food/mining/housing/tax_bonus/research_bonus/color）。
2. `zone_adjacency` 加邻接规则。
3. `default_zone` 决定自动生成。
4. UI 分区调色板自动读取配置。
5. 测试邻接/宜居/产出。

### 13.3 新增科技/时代

1. `tech_tree`、`research_fields`、`eras` 同步调整。
2. 所有读取科技效果的代码（经济、军事、UI）加新线。
3. 开局时代基线、知识胜利条件、LLM 观察字段检查。
4. 回放测试与晚期开局测试。

### 13.4 新增胜利路线

1. `victory_conditions` 加阈值。
2. `core/society.py::victory_progress` 加进度与详情。
3. `dashboard.html` 的侧栏自动遍历 conditions，通常无需改结构。
4. 测试进度、达成、赢家选择。

### 13.5 新增兵种

1. `config/rules.json` 的 `units` 加一条：`label`、`color`、`power`、`defense`、`max_count`、`cost{treasury,food}`、`upkeep_per_day`、`required_technology{field,level}`、`counters{敌兵种ID:±修正}`、`terrain{生物群系:倍率}`。ID 用大写，克制只能指向表内 ID，地形键必须出现在 `biome_food_yield`。
2. 不写任何引擎分支：`core/military.py` 逐个兵种合成战力、按 `counters` 加权、对进攻方套用 `terrain`，战损自动按比例拆分。
3. `agents/personality.py` 的三种 `mock.units` 各插入该兵种 ID，否则 AI 永远不会招募它。
4. 需要科技门控时改 `required_technology`，建筑还要在对应 `tech_tree` 等级的 `unlocks` 里补上 `label`（校验字段，界面标签已从 `required_technology` 反查）；`core/content.py` 会在开局前双向校验。
5. 前端不需要改：军事面板遍历 `/api/state` 的 `unit_specs`，`military_specs` 提供批量与总规模；科技面板的解锁标签与「军工解锁」页签同样从这两张表派生。
6. 测试补在 `tests/test_military_units.py`：招募门控、克制是否真的改变 `attack_power`、战损拆分是否确定。
7. 改完先跑 `python -m pytest -q`：`validate_content` 会在建局时就拒绝坏表，不等结算炸。

---

## 14. 地图与美术

### 14.1 静态基线

- `output/world_2000BCE.json`：3,888 格（72×54），2,703 陆地，1,185 海洋，289 河流格。
- 世界哈希被存档引用；文件字节变化会导致旧存档拒绝加载。
- 重新生成写到 `output_regenerated/`。

### 14.2 地图生成

- `worldgen.py`：海拔/气候/水文/灾害/初始国家/天气函数。
- `mapgen.py`：v1 公平随机地图与自定义 2–12 国；旧测试和基线保持此路径。
- `mapgen_v2.py`：新世界默认路径。先在高分辨率栅格上生成连续地形、水系、道路，再聚合到 144×108 细六边形。输出 `river_cells`、`road_cells`、连续地形纹理和河深色阶。
- v2 地图半径 17 km，单元面积约 750 km²；旧地图半径 34 km、单元约 3,003 km²。
- v2 河流阈值按真实汇水面积校准，不直接复用旧 17 格阈值；河格由细网格水文生成，同一 `network_id` 内必须六邻接连通；河深是汇水面积代理，不是水力测量值。
- v2 首都选址禁止河格；`SETTLE` 在河格上返回 `RIVER_TILE`，AI 边疆候选也会跳过河格。
- 连续气候噪声与火山核按物理尺度缩放，避免高分辨率栅格把气候切成高频噪点。
- 旧 72×54 世界文件不能覆盖；v2 使用 `world_v2_custom_<seed>.json`。动态存档仍只保存 `world_file` 与 SHA-256。
- 新建世界使用 A/B/C/D… 的通用 ID；固定基线仍使用 ELAR/MARIS/VAYR/SAHAR；颜色、公平度、AI 偏好和 UI 均按国家列表动态生成。
- 新世界通过 UI（可增减国家）或 `POST /api/new_game` 创建。
- 自动边疆扩张在 `core/frontier.py`，规则参数在 `config.expansion.ai_interval_days`、`ai_treasury_reserve`；回放时关闭自动扩张，只重放动作日志。

### 14.3 手绘地图（`mapgen_custom.py`）

玩家只画四样东西：地形字符、河流格、道路格、都城。其余全部由草稿派生，派生过程不引入新随机源。

- 画布固定 72×54（`DRAFT_WIDTH/DRAFT_HEIGHT` 取自 `mapgen.W/H`），地形用单个字符表示，词表 `BIOME_CHARS` 同时驱动画笔调色板、派生格表和图例，三处不可能对不上。
- `normalize_draft()` 是唯一的入口校验：行宽、未知字符、全海、无入海口（全陆）、河流/道路落在海上、都城在海上/重叠/相距不足 4 格、国家数量与颜色人口合法性（复用 `mapgen.normalize_countries`）。开局校验面板、行军预览和真正开局都走它，所以面板里报出的错误就是开局会报的错误。
- `_derive_terrain()` 按纬度带 + 生物群系表 `TERRAIN` 派生海拔、年均温、年降水、月序列、坡度与六类灾害；`_derive_drainage()` 用优先洪水填洼算出单跳 `downstream` 与子树汇水面积。同一张图 + 同一颗种子每次算出的结果一致（噪声用 `zlib.crc32(seed:col:row:tag)`，不用 Python 的加盐 `hash`）。
- 核心领土与公平度直接复用 `mapgen._grow_equal_territories` 和 `mapgen._fairness`，手绘图不另设一套平衡口径；`fairness.mode` 标记为 `hand_drawn`。
- 世界文件写到 `runs/worlds/world_draft_<seed>_<digest10>.json`。文件名带草稿摘要是硬要求：存档用 `world_sha256` 钉住静态世界，两张不同的图若撞同一个文件名，旧存档会拒绝加载。
- `march_preview()` 与 `draft_report()` 只做地形/道路/领土推导，不跑气候与水文，悬停一次约 2–10 ms。
- 行军定价只有 `core.movement.Movement` 一处实现：绘制器、引擎结算、AI 共用同一张 `movement` 成本表。`tests/test_mapgen_custom.py::test_painted_days_survive_into_the_running_engine` 断言"画图上显示的天数 == 开局后引擎算出的天数"，改成本表就会在这里露馅。

### 14.4 图标

- 图标在 `assets/icons/*.png`，来源是本地 GPT Image Studio 的写实低质量图。
- 原始拼图在 `assets/raw/`。
- 前端按 `BUILD_ICON` 映射；新增建筑需要新增同名 PNG。
- 当前背景为深蓝灰摄影棚底，通过圆角遮罩融入；替换透明图不需要改逻辑。

---

## 15. 测试与验收

### 15.1 测试矩阵

| 文件 | 覆盖 |
| --- | --- |
| `test_generated_world.py` | 世界完整性、气候、连通性、天气样本 |
| `test_mapgen.py` | 自定义国家、16 格连通核心、公平度元数据 |
| `test_mapgen_v2.py` | 连续地形、144×108 网格、河深/道路实体、存档往返 |
| `test_mapgen_custom.py` | 手绘草稿校验、派生世界确定性与合法性、开局校验面板、行军预览与引擎定价一致 |
| `test_military_units.py` | 兵种内容表、编成存档兼容、逐兵种招募与克制、兵营募兵开关 |
| `test_march_movement.py` | 加权最短路、每日推进、道路加成、命令上限分段、抵达开战、远征报价与 `march_cancelled` 的已知矛盾（`test_an_expedition_is_priced_but_never_walked`） |
| `test_march_balance.py` | 多年战役不变量：命令不超上限、议和条款取自内容表、筑路欲望进性格、逐军队报价与列上限、会战战力系数落在集结区间内、兵营随城市增长、接管国报价 |
| `test_mass_battle.py` | 兵营规模与集结：`field_cap` 随营数增长、兵营数量不封顶、每营固定编制与停饷、集结系数算给攻守两侧、战报记录到场军队、AI 读同一份上限、面板契约 |
| `test_tile_strikes.py` | 按格驻军与按格下令：地块驻军报告（本国/无主/敌国/观战）、各 `reason` 拒绝码、落脚格报价与 `_mass` 一致、按格价 == 引擎列价、点了别的缺口就真的打别的缺口、不可达不编天数、当日会战与抵达会战的事件序列、`ATTACK.tile` 协议校验、`MarchOrder.objective` 存档往返、端点与面板契约 |
| `test_capture_tribute.py` | 占领贡金：只有带建筑且非免贡的格子才立账、比例/天数/逐笔叠加/8% 封顶取最长账单、每日划拨额与守恒、国库见底不产生新钱、到期停收、存档往返、`step_day` 挂钩、按格预告与会战结算逐字段一致、双方宫廷短讯、观测账本与参数键、`_peace_gate` 算术与 `decide()` 议和翻转、内容闸门拒绝坏配置 |
| `test_recruit_receipts.py` | 募兵与外交回执：兵营真的练出那名士兵（`raised`/编成/`room`/事件逐字段）、`stall` 是实时算的不是上次结算的答案、非自愿停工只播报一次而 `PAUSED` 保持安静、本营定额只挡驻军不挡野战军队、`recruit_options` 的 `reason` 与引擎 `reason_code` 全表对齐（含科技门槛与闸门顺序、整批定价）、回执有界且与 `action_log` 同序、存档往返且指纹不动、提案全生命周期（PENDING → ACCEPTED/ACTIVE/REJECTED/EXPIRED）、`counterparty` 双向、贸易与协定同账、三营三种停摆、观测与 LLM 提示词读同一批字段 |
| `test_siege.py` | 围城与整城易主（38 项）：中心格开围不打会战、`NOT_IN_RING` / `SIEGE_TAKEN`、封锁圈才算兵力（不是会战那一格）、账满日 == 预告日、成数逐日推进、城墙与守军按同一算术减速、解围三种码 `peace` / `ring_empty` / `captured`、一座城同时只归一方围、同国第二支加入同一本账、城破整座连地带人换手且建筑立贡金、**都城破则整国并入并留下墓碑（`test_a_realm_that_lost_its_capital_never_comes_back_on`）**、`on_city_center` 只落中心格、城墙先过科技闸门、存档往返与无 `siege` 键的旧存档、AI 读的行就是结算的账、`_siege_orders` 不把人挪上别人的墙、`_peace_gate` 在城破前翻求和（外省城只吃 `siege.peace_pressure`，都城行再吃 `annexation.peace_pressure`）、LLM 提示词教同一批键、面板契约与宫廷短讯、内容闸门拒绝坏 `siege` 表，以及**开赴通道**：`siege_marches` 用 `at=` 给在途军队按落脚格算抵达当日的账、还在家的军队不算围（`share 0 / days_left null`）、六格全敌土时一行不报且同一道命令被 `NO_PATH` 回绝、报出的行真的抵达并在抵达当日 `siege_started`、`_siege_orders` 先开围后开赴且一支军队只下一道令、提示词与面板都印这张表、规则页那张围城常量卡七个键全从 `military.siege` 抄且不写死数字 |
| `test_border_alerts.py` | 入侵边警与无守军缺口（18 项）：在途军队按**落地那格的主人**归档、`arrival_day` 就是会战/城破事件所在的那一日、没有进攻 `purpose` 的军队只是走路不是入侵、落在无人格当日只折 `unopposed_loss_rate` 且**不保底 1 人**、走向都城封锁圈的行标 `capital` 而城边一格农田不冒充都城、`_relief` 只答提问国且只报真能赶到的那支、`relief.days <= days_left` 才值得回防、都城行排在城门行之前、一支军队不被两条边警下两道令、防守方在自己观测里同时读到警与答、快照按受害者分组为面板备齐整张棋盘、存档往返后边警照旧，以及**警报叫醒议事**：落向城墙的行军只惊动本城主人、打城缘的不惊动、城墙正被啃的国家在非议事日也坐下议事（`Scribe` 记下面对谁议事）、同一支军队重复确认同一本围城账不再写第二条 `siege_started` |
| `test_dashboard_ui.py` | 面板结构、军事/行军/绘制器的前后端契约字符串 |
| `test_actions.py` | 建造、科研、政策、交易、外交、军事、城市、预算、动作原子性 |
| `test_simulation.py` | 日历、暂停/倍速、存档、世界哈希、经济、评分、胜利、灾害 |
| `test_agents_replay.py` | 观察隔离、AI 贸易、研究、玩家跳过、365 日、回放、断点续跑 |
| `test_llm_adapter.py` | OpenAI 兼容协议、重试、预算、Usage、混用、失败回退 |

当前：**324 passed**（`python -m pytest -q --basetemp=.tmp_pytest`）。

### 15.2 测试原则

- 新机制必须测试“真实结算”，不能只断言事件出现。
- 资源扣除/转移要测非法输入和失败原子性。
- 城市改动要测人口守恒、存档往返、回放指纹。
- AI 改动要测观察隔离、玩家国家跳过、失败回退。
- 平衡改动跑 365 日；结构性改动跑 1,825 日。

### 15.3 验收清单

提交前逐项确认：

- [ ] `pytest -q` 全绿。
- [ ] 365 日 Mock 无崩溃、无异常饥荒/财政失控。
- [ ] 新动作有 schema、执行、拒绝原因、测试。
- [ ] 新字段进存档并能旧档兼容。
- [ ] 回放指纹一致。
- [ ] UI 实际打开验证，不只看接口。
- [ ] 控制台无 JS 错误、无 404。
- [ ] 没有提交密钥、`.venv`、`runs/`、临时截图。
- [ ] 平衡改动记录实测数据。

---

## 16. 团队协作规范

### 16.1 当前版本控制状态

项目已初始化 Git，并推送到：

```text
https://github.com/zsmb07/neirong
默认分支：main
```

同事首次获取：

```bash
git clone https://github.com/zsmb07/neirong.git
cd neirong
python -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.txt pytest
./.venv/Scripts/python -m pytest -q
```

日常提交：

```bash
git switch -c feat/<topic>
# 修改、测试
git add <具体文件>
git commit -m "feat(scope): ..."
git push -u origin feat/<topic>
```

**基线文件：** `.gitignore` 使用 `output/*` 排除生成物，但显式保留 `output/world_2000BCE.json`、`world_summary.json`、`first_year_weather.json`、`sha256_manifest.json`、`viewer.html`。不要用 `git add -f` 绕过规则，也不要把整个 `output/` 重新加回来。

**换行保护：** `.gitattributes` 已固定 `output/world_2000BCE.json -text`，避免 Git 转换换行后导致世界 SHA-256 校验失败；图片按二进制处理。

不要提交：

- `.venv/`
- `runs/`（存档、实验输出、截图）
- `output_regenerated/`
- `.idea/`、`Thumbs.db`
- 任何 API 密钥文件（本机 `api.txt` 在仓库目录之外）
- `__pycache__/`、`.pytest_cache/`

### 16.2 分支与提交

建议：

- `main`：始终可运行、测试全绿。
- `feat/<module>-<topic>`：功能分支。
- `fix/<topic>`：修复分支。
- 每个 PR 只做一件事；动作协议、存档格式、配置键的改动要单独说明。

提交信息示例：

```text
feat(city): add local service budget tiers
fix(replay): exclude UI-only city note from fingerprint
balance(economy): lower city service cost to 0.0015
docs(manual): document action protocol and save format
```

### 16.3 建议分工

| 角色 | 主要文件 | 备注 |
| --- | --- | --- |
| 引擎/规则 | `core/state.py`, `core/rules.py`, `core/actions.py`, `core/simulation.py` | 动作协议与确定性优先 |
| 经济/城市 | `core/economy.py`, `core/society.py`, `core/city_governance.py` | 强耦合，改动需同步测试 |
| AI | `agents/*` | 观察隔离、预算、失败回退 |
| 后端/API | `live_server.py`, `core/replay.py` | 本地安全边界、存档槽 |
| 前端 | `dashboard.html` | 单文件，改动前先沟通，避免冲突 |
| 地图/美术 | `worldgen.py`, `mapgen.py`, `mapgen_v2.py`, `assets/` | 不覆盖基线世界 |
| QA/平衡 | `tests/`, `run_simulation.py`, `runs/` | 365/1,825 日实验 |

`dashboard.html` 与 `core/actions.py` 是冲突高发区；多人同时改时先约定接口字段。

### 16.4 代码风格

- Python 3.10+ 类型标注；模块顶部 `from __future__ import annotations`。
- 业务拒绝用 `ActionRejected("CODE", "中文原因")`，不要抛裸异常。
- 事件类型用稳定 snake_case；payload 保持 JSON 可序列化。
- 不写无意义注释；只在约束/坑点处写一行说明。
- 中文 UI 文本；代码标识符英文。
- 新增数值优先进 `config/rules.json`，不要散落魔法数。

### 16.5 评审重点

- 是否绕过了规则引擎？
- 是否破坏人口守恒/世界哈希/确定性？
- 新字段是否进入存档与回放？
- AI 是否能通过该机制获得无限资源或绕过校验？
- UI 是否只读服务端权威状态？
- 是否泄露密钥或预算失控？
- 是否跑过长期模拟？

### 16.6 GitHub 协作

- 所有人通过 GitHub 仓库协作，不再使用 SMB/SSH/Radmin 远程接入。
- `main` 保持可运行；功能在 `feat/<topic>` 分支开发，通过 Pull Request 合并。
- PR 描述写清：改动目的、影响的存档/协议/配置、测试与长期模拟结果。
- 世界基线 `output/world_2000BCE.json` 是二进制等价输入，任何修改都必须单独评审并同步更新哈希/存档兼容说明。
- 大存档、实验输出和截图不提交；需要分享时使用 Release 附件或外部存储。
- 本机 `live_server.py` 仍然只监听 `127.0.0.1`，没有认证；不要把它部署到公网。

---

## 17. 常见故障排查

### 17.1 端口被占用

Windows：

```bat
netstat -ano | findstr 8765
taskkill /PID <PID> /F
```

或换端口：

```bash
./.venv/Scripts/python live_server.py --port 8766
```

### 17.2 页面一直“正在载入”

- 确认服务进程还活着，`/api/state` 返回 200。
- 浏览器控制台看 JS 错误。
- 如果使用隐藏/后台浏览器自动化，`requestAnimationFrame` 可能被节流；手动调用 `poll(true)` 或用可见的 headless 页面。

### 17.3 控制台中文乱码

- 存档/接口使用 UTF-8；Windows 控制台可能按 GBK 显示。
- 临时设置 `PYTHONIOENCODING=utf-8`，或改用支持 UTF-8 的终端。

### 17.4 存档加载失败

- `static world SHA-256 differs`：世界基线被改动或换行被转换。恢复原文件；不要手改 `output/world_2000BCE.json`。
- `unsupported save format`：格式版本不匹配，检查是否改了 `format_version`。
- 存档引用的 `world_file` 不存在：确认相对路径和目录结构。

### 17.5 回放指纹不一致

- 是否有未记录的随机数？
- 是否新增了派生字段并写进指纹，但回放无法重现？
- 是否绕过了动作日志直接改城市？
- 是否在回放时错误启用了 CityManager？
- 对比 `tests/test_agents_replay.py` 的两个回放测试。

### 17.6 LLM 返回空动作/失败

- 检查密钥环境变量是否配置。
- 推理模型可能把输出预算耗在 reasoning 上；提高 `max_output_tokens`（上限 4000）。
- 确认返回是纯 JSON，不要 Markdown 围栏。
- 检查 `country_id`、`world_day` 是否与观察一致。
- 查看事件日志里的 `llm_call` / `agent_fallback`。

### 17.7 测试找不到 pytest / scipy

使用项目 `.venv`，不要用全局 Python：

```bash
./.venv/Scripts/python -m pip install pytest
./.venv/Scripts/python -m pytest -q
```

---

## 18. 已知限制与路线图

当前是垂直切片，不是完整商业游戏。

已知限制：

- 六家 LLM 厂商的预设已完成接入与本地安全测试，但尚未用五家真实账号逐一验收；模型 ID 以各账号控制台实际开通情况为准。
- Mock 规则 AI 仍是基线强度，尚未进行“无 Key 玩家也能获得完整体验”的深度策略优化。
- 军事是战略抽象：已有兵营、多支军队实体、真行军与围城（封锁圈 → 每日成数 → 整城易主），但无海军、补给线、军团编制；围城也不模拟援军解围事件、饥荒围城内的粮价与城墙耐久分级——一座城同时只有一本账、只有一方围得。
- **远征行军在 v0.43 已经打通**（旧文档曾把它列为「报价能出、结算走不完」的已知缺陷）：`march_daily` 不再因为「下一格已属他国」而中止，按下单时冻结的路线走完，抵达当日照常有守军就开战；`Movement` 在己方/无主走廊走不通时放开成过境敌土再算一次价（`expedition=True`，每格多付 `hostile_cost`，会战再按 `expedition_factor` 惩罚），报价与结算读的是同一条路。围城开赴通道 `siege_marches` 也随之放开——它现在会报过境路线，代价写在同一行里，「面板报得出 = 结算走得到」靠的是同源定价而不是把路线藏起来。这一改动等于**抬高战争强度**，任何后续平衡结论都要以多 seed 长程实测为准（行军令次数、到场率、远征占比、夺地率、陷落与吞并数），不要凭「应该更活跃了」下结论。
- 默认开局世界（`output/world_2000BCE.json`，四国）在 4160 游戏日内一次宣战都没有：ELAR 与 SAHAR 全程零兵营零军队，VAYR 只攒到 3 支，唯一成规模的 MARIS（23 支、6 座营）离最近的对手还有 37 格以上——远超当时 24 点的 `movement.maximum_order_cost`，报价一律 `reachable=false`，而 AI 在宣战之前就先看可达性，于是和平一直挂着。军事内容的平衡证据因此全部来自 `mapgen.generate_fair_world` 生成的公平世界，实时观察台的默认局与 `run_march_balance.py` 不是同一个世界。**这条结论测于「24 点上限 + 走廊不可穿」的旧口径**：v0.43 把上限提到 60 点并放开敌土通行，默认局是否仍然长和平需要重测（37 格草原在 60 点内已经够得着）。
- 城市没有完整物资库存与城市间商品网络；目前只有粮食账本与地方预算。
- 城市服务已有治安/卫生指标，但还没有独立的卫生所、治安所、消防等设施与事件链。
- 无正式胜利动画、音效、过场；结算为 UI 面板。
- 世界气象是游戏模型，不是科学天气预报。
- 前端是单文件原生 JS，无组件化/构建系统；规模继续增长时需要拆分。
- 实时服务无认证，只适合本机；远程访问需额外认证层。
- 存档格式版本 1；破坏性变更尚无迁移框架。
- 事件日志会随长期运行增长，尚未做归档。

路线图（详见 `PROJECT_PLAN.md`）：

1. 城市物资库存、完整分区供需、城市服务设施。
2. 更强的 AI 市长诊断与预算解释。
3. 多军团、海军、补给线、战争目标。
4. 时代专属建筑/单位/政策，更多剧本与目标模式。
5. 正式美术、动画、音效、胜利演出。
6. UI 组件化与可访问性。

---

## 19. 附录：快速参考

### 19.1 常用命令

```bash
# 测试
./.venv/Scripts/python -m pytest -q

# 实时观察台（Mock）
./.venv/Scripts/python live_server.py --days 365 --speed 2 --paused --open

# 带 LLM（需本机密钥文件与预算）
./.venv/Scripts/python live_server.py --days 180 --speed 2 --open \
  --models config/models.deepseek.json --api-key-file "/path/to/key" \
  --max-calls 2 --max-cost-usd 0.10

# 批处理 365 日
./.venv/Scripts/python run_simulation.py run --days 365

# 从存档继续
./.venv/Scripts/python run_simulation.py run --load runs/eos_365.json --days 365 \
  --save runs/eos_730.json

# 地图重建（不要覆盖 output/）
./.venv/Scripts/python worldgen.py --seed 2000 --output output_regenerated
```

手测集结闭环（默认世界不会开战，见第 18 节）：

1. `live_server.py --port 8899 --paused --days 3650` 起服务，`POST /api/new_game` 传
   `{"countries":[{},{},{},{},{},{}],"base_seed":77777,"start_astronomical_year":-799}` 换成公平世界。
2. 反复 `POST /api/control {"action":"step"}` 直到某国 `at_war_with` 非空且有两支以上待命军队，
   再 `{"action":"take_control","value":"<该国>"}`。
3. 读 `GET /api/state` 的 `campaign_quotes`——**键是敌国，不是自己**；面板「军事 → 战争」点
   「其余向此集结」，`march_ordered` 事件应带着到集结格的路线出现。
4. 继续 step 到军队抵达：同一行的 `mass` 应从 1 变成 2、`mass_share` 抬到 1.0，
   随后的 `battle` 事件里 `attacker_mass` 列出两支军队。

手测按格闭环（在浏览器里点，不靠 API）：

1. `live_server.py --port 8899 --days 365 --speed 2 --paused`，主菜单「新建世界」填 seed
   `77777` 生成公平世界，接管国家 A，募 4 支民兵并向 D 宣战，然后暂停。
2. 在地图上点一格**敌国边缘缺口**：详情侧栏应同时出现「驻军」与「攻打这一格」。
   实测 (67,26) 给「行军 6 天 · 17.50 成本 → 落脚 (66,26)，抵达当日开战 · 到场 1 支 · 战力系数 1.00」。
3. 点「开赴并攻打」，`GET /api/state` 的该军队 `march` 应是
   `purpose=ATTACK / target=D / objective=[67,26] / destination=[66,26] / cost=17.5 / days_left=6`
   ——即 `objective` 就是你点的那一格，不是引擎自动挑的最近缺口。
4. 单步 6 天：先 `army_arrived {fought:true}`，同日 `battle`，胜利则 `captured=true` 且
   `attacker_share` 等于面板印出来的系数（实测 1.0 对 0.5）。
5. 点一格**本国或无主地**：只有「驻军」卡，本国军队那行有「选为下令对象」；
   点一格**已经在军队周围的敌格**（实测 (66,27)）：文案变「已在这一格的周围六格：下令当日直接会战」，
   按钮变「用这支军队攻打」，下令当日开战且军队位置不动。
6. 点**城池中心格**：不再被拒，卡头改说「围这一座城」并直接印出围城账（成数 / 进度 / 几日城破）；
   圈里已有本国军队时按钮是「用这支军队开围」，下令当日开账且 `siege_started` 事件与账上数字一致，
   还没进圈时是「开赴封锁圈」。点**未宣战国的地块**仍给 `NOT_AT_WAR` 与「向 国家X 宣战」按钮，
   点完面板立刻重新报价（不用换天）。
7. 报价核对：`GET /api/tile_strikes?col=67&row=26` 与同军队在 `campaign_quotes` 里的行
   应给出同一份价；两者不同就说明有人在前端另算了一套。
8. **开赴并开围**（围城的第二条通道，实测 seed `31001`）：把时钟推到第 1806 日、接管国家 B，
   「军事 → 战争 → 与 国家D 交战中」下面除「可下令军队」外应再多一张「可开赴的城墙 · 国家D 1 路」：
   青原城3 中心 (82,29)、落脚 (83,30)、「1 天 / 3.00 成本 · 只走己方与无主地」、
   「抵达后围 17 日 · 全程约 18 日城破」。点「开赴并开围」→ `march_ordered` 带
   `purpose=ATTACK / objective=[82,29] / destination=[83,30] / expedition=false`；
   单步 1 天 → `army_arrived` 落在 (83,30)、`siege_started` 当日开账，
   `GET /api/state` 里 `countries.D.cities[..].siege` 给 `open=true / armies=["B-A2"] / progress=0.06 / days_left=16`
   （= 报价的 17 天减掉已经走完的那一天），面板那一行同时从「可开赴的城墙」搬进「围城战线」。
   这张表**只报结算送得到的路**：若城缘六格全是敌土，表就空下来并给 hint，此时必须先夺一格城缘。

### 19.2 关键文件

```text
core/state.py                 数据模型、存档、summary、胜利
core/rules.py                 唯一写入口、日推进
core/actions.py               动作 schema
core/economy.py               经济、粮食物流、预算
core/society.py               城市指标、分区、评分、胜利条件
core/city_governance.py       AI 市长
live_server.py                HTTP + 会话 + 存档槽
dashboard.html                单页 UI
config/rules.json             全部平衡参数
config/llm_providers.json     LLM 厂商简单模式预设（端点/默认模型/文档）
output/world_2000BCE.json     不可覆盖的静态世界基线
runs/saves/                   存档槽（不入库）
runs/worlds/                  自定义随机世界（不入库）
```

### 19.3 状态字段速查

国家快照主要字段：

```text
population, food, treasury, tax_rate, workers, policy
technology, research_field, research_progress
tiles, outposts, buildings, projects, cities
stability, housing, food_capacity, last_economy
army_strength, army_cap, army_units, army_power, military_upkeep, army_position, armies, at_war_with, war_exhaustion
barracks, recruit_options, receipts
tribute(paid/collected/daily_paid/daily_collected/base_income), diplomacy(relations/active_pacts/incoming_proposals/outgoing_proposals/ledger), scores(total/breakdown), victory
```

`paid`/`collected` 是按对手聚合后的转账行（`to`/`from`、`share`、`capped`、`days_left`、`buildings`、`daily`）；账本本身存在 `CountryState.levies` 里（逐建筑一条：`to`/`building`/`share`/`days_left`），只进存档不进快照，快照一律给派生后的 `tribute`。

`barracks` 是 `barracks_reports` 的逐营一行（字段见 §7.9），`recruit_options` 是逐兵种一行的 `unit/label/batch/reason/can_recruit`，`receipts` 是本国最近 12 条命令回执；`diplomacy.ledger` 是 `diplomacy_ledger` 的原样输出。这三样都只由 `core/state.py` 单源派生，前端与 AI 不再自己算，`receipts` 也不参与 `fingerprint()`。

城市快照主要字段：

```text
city_id, name, center, population, focus, tiles
housing, food_potential, food_output, food_need, food_balance
food_self_sufficiency, food_status, food_loss
budget, budget_label, local_revenue, local_upkeep, local_net
security, health, security_target, health_target, security_status, health_status
livability, managed_by, zones, buildings, tiles_detail
siege(city_id/name/center/attacker/defender/open/armies/garrison/walls/attack_power/defense_power/share/progress/threshold/daily/days_left/started_day/exhaustion) | null
```

`siege` 是 `siege_ledger` 的原样输出（没被围就是 `null`），军事页的 `campaign_quotes[敌国].siege`、`.siege_marches` 与 `/api/tile_strikes` 的 `siege` / `siege_taken` 用的是同一个函数，所以城市卡、地块卡、战争卡三处印出的城破日是同一个数字；`siege_marches` 那一行的 `share / days_left` 走的是同一本账加 `at={army_id: 落脚格}`，即「抵达那天」的账。

### 19.4 验证用数值

- 每日行军预算 0.75，单令 60 点，满额 80 日。敌土加 1.25 点。边警回防直接比较两侧引擎报价；围城开赴分开显示旅程与围城天数。
- 每座兵营每天贡献 1 训练点、扣 0.01 财政。民兵/弓手/战车/重步/骑兵/攻城单营分别为 260/320/390/470/560/700 日，财政首付 2/2.5/3/3.5/4/5。首付另计粮，养护全用粮，每兵每天 0.0288/0.032/0.0448/0.04/0.0528/0.0576。
- 一年 365 日，普通格容量为营数 × floor(10 × 365 ÷ 260) = 营数 × 14；都城固定再加 14，等于该档的一营基准。单营兵种额度 8/6/4/5/4/3，民兵比率 8/14。每格一支本国驻军。全国与每营总兵力的旧定额停用，保留每单最多招募 4 支。
- 有守军会战的胜方 4%、败方 42%；无守军 2%，胜方和无守军向下取整。城域溃退生还率 40%。最低战力系数 0.5，单军最高 0.75，集结半径 1 格。民兵地形中性，其他兵种按地形表对攻守双方定价。
- 围城进度每天 0.06 × 实际成数，阈值 1，封锁半径 1 格，城墙分量 1.6。无墙无守军最快 17 日；按 3 点成本开赴需要 4 日，最快全程 21 日。抵达当天用实际圈内攻守兵力重算。城墙仍需青铜冶金 3 级、60 财政、90 日。
- 运行平衡对照：python run_march_balance.py --seeds 2000 2001 --scenarios appetite open_camps no_camps rendezvous_control rendezvous --days 730 --output <目录>。
- 双种子五组各 730 日：225 道行军令、171 场会战、58 次宣战、50 次议和，超程 0。预置驻营军的组练成 46 支；普通开局组未练成，慢练兵档下首批兵仍主要来自主动招募。
