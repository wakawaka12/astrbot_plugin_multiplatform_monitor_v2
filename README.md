# steam_status_monitor_V3 · 全平台游戏监控与查价插件

![MIT](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/Python-3.10+-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.24.2-purple)
[![Repo](https://img.shields.io/badge/GitHub-wakawaka12%2Fastrbot__plugin__multiplatform__monitor__v2-181717?logo=github)](https://github.com/wakawaka12/astrbot_plugin_multiplatform_monitor_v2)

面向 **AstrBot** 的 QQ 机器人插件：监控 Steam / PSN / Xbox 玩家状态，推送上下线与进出游戏；并提供 **查价、愿望单折扣、排行榜、新购游戏通知** 与本地资料库。

> **开源声明**  
> 本仓库是在开源项目  
> **[Maoer233/astrbot_plugin_steam_status_monitor](https://github.com/Maoer233/astrbot_plugin_steam_status_monitor)**（MIT，作者 Maoer）  
> **v4.5.5** 基础上的 **移植 / 二次开发**，用于群场景线上化与增强。  
> 原始著作权归上游作者所有；本仓库修改与新增部分见 Git 历史。License 仍为 **MIT**。

---

## 功能一览

### 1. 状态监控（核心）
- 多 SteamID 分群监控；智能轮询（游戏中约 1 分钟，离线更长）或固定间隔
- **上线 / 下线 / 进游戏 / 切游戏 / 退游戏** 推送；游戏会话状态机（假退出合并、时长统计）
- 状态专用连接池，**批量 GetPlayerSummaries**，降低限流风险
- PSN / Xbox 多平台前缀（`psn:` / `xbox:`）；NS 因上游 API 不可用暂停

### 2. 查价 `/price` `/px` `/steam`
- ITAD 候选检索 + 多区现价（默认国区 + 比价区）+ **Steam 史低**
- **本地库 DLC 摘要**：本体只展示「共 N 个 + 少量示例」；查 DLC 显示所属本体
- 商店 403 冷却时降级 ITAD；查价结果写入本地库（现价 / 观测史低）

### 3. 愿望单折扣 `/game wish_sale`
- **列表**与**价格**分离：默认凌晨自动拉愿望单列表（SSR）
- **ITAD** 判断是否折扣；商店仅对「有折扣且未知截止」补 `sale_end`
- 有折扣且截止未到 **跳过**，降低重复请求
- 夜间扫描写本地入队，**白天固定时间**合并推送，并 **@ 已绑定 QQ**
- 手动：`update` / `check` / `test`；`update` 带进度提示

### 4. 本地资料库（SQLite）
- `local_store.db`：游戏元数据、DLC→本体、价格与史低、愿望单缓存、库存快照等
- 适合后续数据分析与「是否新史低」判断

### 5. 其它
- 游戏时长排行榜（今日 / 昨日，昵称优先，封面多级回退）
- **新购游戏通知**（对比已购库快照）
- 成就相关能力、WebUI 管理页、`/status` 接口探测

---

## 指令速查

| 指令 | 说明 |
|------|------|
| `/status` | 接口与状态池探测 |
| `/price` `/px` `/steam <名或链接>` | 查价 |
| `/steam rank` · `/steam rank 昨天` | 时长排行 |
| `/game wish_sale on\|off\|status\|update\|check\|test\|cache\|list` | 愿望单折扣 |
| 绑定 / 监控管理 | 见 AstrBot WebUI 与 `/game`、`/steam` 子指令 |

---

## 架构（简图）

```text
QQ 群指令 / 后台定时
  ├─ 状态轮询（状态池）→ 上下线/进游戏 → 群推送
  ├─ 查价（ITAD + 商店/ITAD 价 + 本地 DLC 摘要）
  ├─ 愿望单（凌晨 SSR 列表 + ITAD 折扣 + 少量商店截止 → 白天汇总 @ 推送）
  ├─ 排行 / 新购通知
  └─ SQLite local_store.db
```

**稳定性策略（摘要）**
- Steam 商店全局 403 冷却；愿望单/查价与状态请求隔离
- ITAD 连续失败熔断，避免拖垮主流程
- 愿望单：跳过「未截止折扣」、夜间错峰、请求配额

---

## 环境要求

- Python 3.10+
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) ≥ 4.24.2
- Steam Web API Key；查价/愿望单建议配置 **ITAD API Key**
- 可选 HTTP(S) 代理（配置 `enable_proxy` / `proxy_url`）

依赖见 `requirements.txt`。

---

## 安装

1. 将本仓库放到 AstrBot 插件目录，例如：  
   `data/plugins/steam_status_monitor_V3`
2. 安装依赖：`pip install -r requirements.txt`
3. 在 AstrBot 插件配置中填写 `steam_api_key`、`itad_api_key` 等（勿提交真实密钥到 Git）
4. 重启 AstrBot / 重载插件

字体：CJK 字体可按上游说明在启动后下载，或放入 `assets/fonts/`。

---

## 配置说明（部分）

| 键 | 默认/建议 | 说明 |
|----|-----------|------|
| `fixed_poll_interval` | 0 | 0=智能轮询；>0=固定秒 |
| `smart_poll_intervals` | 1,2,3,5,8,15 | 智能间隔（分钟） |
| `price_region` / `price_compare_regions` | CN / IN,UA,PK | 查价区 |
| `wish_sale_night_hour` | 1 | 愿望单夜间任务小时 |
| `wish_sale_notify_hour` | 9 | 折扣白天统一通知小时 |
| `wish_sale_min_cut` | 10 | 折扣推送门槛 % |
| `wish_sale_night_store_limit` | 60 | 夜间愿望单商店请求上限 |
| `enable_owned_games_notify` | true | 新购游戏通知 |
| `enable_proxy` / `proxy_url` | 按环境 | 代理 |

新配置项须登记在 `_conf_schema.json`，否则 AstrBot 启动时可能丢弃未知键。

---

## 数据目录（运行时）

约：`data/steam_status_monitor/`

- `local_store.db` — 资料库  
- `wishlist_cache/`、`wish_sale_price_cache.json` — 愿望单与折扣缓存  
- `group_*_states.json` 等 — 轮询基线与会话状态  

**请勿**将含 API Key 的配置或用户数据提交到公开仓库。

---

## 开源协议

- **License:** [MIT](./LICENSE)
- **上游:** [Maoer233/astrbot_plugin_steam_status_monitor](https://github.com/Maoer233/astrbot_plugin_steam_status_monitor)（MIT）
- 二次开发请保留本文件中的上游致谢与 LICENSE 版权声明。

---

## 致谢

- [Maoer233](https://github.com/Maoer233) / 上游插件作者与贡献者  
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)  
- Steam Web API、IsThereAnyDeal 等第三方服务

## 免责声明

本插件仅用于学习与个人/小群自动化；请遵守 Steam / PSN / Xbox / ITAD 服务条款，控制请求频率，勿用于滥用或商业爬取。
