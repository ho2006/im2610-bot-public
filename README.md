# IM2610 模拟交易 Bot

只交易 **IM2610** 的自动化模拟交易程序：Python 后端、SQLite 数据库、网页控制台和 Linux Docker 容器。策略结合盘口失衡、Microprice、库存偏移和被动限价做市。

公开仓库：[ho2006/im2610-bot-public](https://github.com/ho2006/im2610-bot-public)，采用 [MIT 许可证](LICENSE)。公开版本的 `main` 包含最新环境过滤策略、研究工具、测试和部署文档。

默认在本机 Docker 的 Linux 容器中运行，使用 `paper` 本地模拟模式，策略暂停，手续费为 **0**。`competition` 模式连接中金所杯比赛模拟账户。没有真实资金交易通道，不自动换月；合约到期上限为 2026-10-16。

## 1. 项目结构

```text
app.py                      网页、鉴权和控制接口
core.py                     行情解析、交易日历、策略和比赛接口
engine.py                   SQLite、循环执行、对账、风控和订单恢复
test_bot.py                 策略、交易恢复和网页离线测试
test_robustness.py          故障、状态冲突和性能回归
test_collector.py           采集、退避、去重和退出检查
test_data_quality.py        完整日覆盖和样本外分割检查
web/                        原生 HTML / CSS / JavaScript 控制台
scripts/init_env.py          生成本机环境配置和随机令牌
scripts/check_debug.py       只读检查本机 paper 服务
scripts/check_connections.py 只读检查比赛账户连接
scripts/collect_l1.py        无密钥的独立真实 L1 采集与健康检查
scripts/backtest.py          真实数据质量、固定方案回放和合成压力测试
scripts/preflight.py         本机工程预检与上线证据缺口
scripts/benchmark.py         离线 SQLite 与历史页面查询基准
scripts/probe_feed.py        检查公开行情
scripts/probe_platform.py    检查比赛接口结构
scripts/package.py          打包源码，不包含密钥和数据库
Dockerfile                  Python 3.13 Linux 镜像
compose.yaml                bot + collector 部署和持久化数据卷
.env.example                配置模板
LICENSE                     MIT 开源许可证
```

运行仅依赖 Flask 和 Waitress，无需前端构建、Redis 或外部数据库。一个账户只运行一个实例。

## 2. 默认方案：本机 Docker

安装 Git、Python 3 和 Docker；Windows 使用 Docker Desktop 的 Linux 容器模式。Docker 需支持 `docker compose` 和 `--wait`。

```bash
git clone https://github.com/ho2006/im2610-bot-public.git
cd im2610-bot-public
python scripts/init_env.py
docker compose -p im2610-debug up -d --build --wait
docker compose -p im2610-debug ps
```

Linux 若没有 `python` 命令，用 `python3 scripts/init_env.py`。公开仓库可直接克隆；控制台令牌和比赛密钥在本机生成或填写，不要提交到 Git。

打开 [本机控制台](http://localhost:8000)（也可访问 `http://127.0.0.1:8000`），从本地 `.env` 复制 `BOT_TOKEN` 输入页面。初次运行默认监控行情但不交易，点击“启动策略”才开始本地模拟。页面刷新后需要重新输入令牌。

Compose 同时启动 `bot` 和 `collector`：后者每 5 秒只读公开新浪 IM2610，把真实、源时间递增且新鲜的交易时段盘口保存到独立 `l1.sqlite3`。采集器不加载 `.env`、不接收比赛密钥，也不操作账户；策略暂停不影响采集。休市期间等待开市，不把重复收盘快照当成新样本。

`init_env.py` 不覆盖已有 `.env`；生成随机控制令牌，默认 `paper` 和 `BOT_AUTOSTART=false`。如果当前进程已设置 `CFAPIKEY`，会将其复制到配置文件，但不会打印密钥。

常用操作：

```bash
docker compose -p im2610-debug logs --tail=100 bot
docker compose -p im2610-debug exec -T collector python scripts/collect_l1.py --db /app/data/l1.sqlite3 --health
docker compose -p im2610-debug logs -f bot
docker compose -p im2610-debug restart bot
docker compose -p im2610-debug stop
docker compose -p im2610-debug up -d --build --wait
```

保持同一个项目名以复用数据卷。当前项目名对应卷 `im2610-debug_bot-data`；更换项目名会创建另一套数据。不要同时启动多个项目争用 8000 端口或操作同一比赛账户。

## 3. 可选：Linux 服务器部署

服务器需安装 Git、Docker Engine、Compose 插件和 Python 3。宿主机 Python 只用于生成配置，不需要安装后端依赖。允许出站 HTTPS 访问新浪行情及比赛接口，保持服务器时钟同步。

```bash
git clone https://github.com/ho2006/im2610-bot.git
cd im2610-bot
python3 scripts/init_env.py
chmod 600 .env
docker compose -p im2610-debug up -d --build --wait
```

默认只监听宿主机 `127.0.0.1:8000`。在自己的电脑建立 SSH 隧道：

```bash
ssh -N -L 8000:127.0.0.1:8000 user@your-server
```

然后访问本机 `http://127.0.0.1:8000`。本机 8000 被占用时改用：

```bash
ssh -N -L 18000:127.0.0.1:8000 user@your-server
```

访问 `http://127.0.0.1:18000`。默认方案不需要开放服务器 8000 入站端口。

两个容器使用 UID 10001 的非 root 用户、只读根文件系统、临时 `/tmp` 和同一命名卷；bot 内存上限 256 MiB，collector 为 128 MiB。不要直接把普通用户不可写的宿主机目录替换成数据卷。

如需要公网域名，需另行配置 HTTPS 和受信代理。写接口严格校验 Origin；当前 Waitress 不信任代理头，简单套一层 HTTPS 反向代理可能因应用看到 HTTP 而返回 403。应配置并验证代理的协议传递和信任范围后再使用；现成部署路径是 SSH 隧道。

## 4. 不使用 Docker

建议 Python 3.13；项目也已在 Python 3.14 验证。命令在项目根目录执行。

Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/init_env.py
.venv/bin/python app.py
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts/init_env.py
.venv\Scripts\python.exe app.py
```

本机服务仍在 `http://127.0.0.1:8000`。默认数据库位于 `data/paper.sqlite3`。不要与 Docker 服务同时占用该端口。

不用 Docker 时，在第二个终端单独运行采集器；停止网页进程不会停止这个终端中的采集器：

```bash
python scripts/collect_l1.py --db data/l1.sqlite3 --interval 5
```

Linux 使用 `.venv/bin/python`，Windows 使用 `.venv\Scripts\python.exe`。`--interval` 允许 2–60 秒，默认 5；`--once` 完成一次只读观察，查询失败退出 1、休市等候退出 0；`--health` 只读数据库健康状态，不发起行情请求。SIGINT / SIGTERM 会打断等待并记录停止状态。

## 5. 环境配置

编辑根目录 `.env`，不要修改镜像来放入密钥。生成的文件示例：

```dotenv
BOT_MODE=paper
BOT_AUTOSTART=false
BOT_TOKEN=替换为生成的随机令牌
CFAPIKEY=
```

| 变量 | 默认 / 要求 | 用途 |
| --- | --- | --- |
| `BOT_MODE` | `paper` | `paper` 本地撮合；`competition` 比赛模拟账户 |
| `BOT_AUTOSTART` | `false` | 是否允许启动时恢复已启用策略 |
| `BOT_TOKEN` | 必填，至少 24 字符 | 网页及 API 控制令牌，与比赛密钥不同 |
| `CFAPIKEY` | competition 必填 | 比赛 Skills 页面提供的账户密钥 |
| `BOT_DB` | 本机 `data/{模式}.sqlite3` | SQLite 路径；Compose 固定为 `/app/data/{模式}.sqlite3` |
| `BOT_HOST` | 本机 `127.0.0.1` | 容器镜像设置为 `0.0.0.0` |
| `PORT` | `8000` | 容器内监听端口；修改时也要修改映射和健康检查 |

本机 `.env` 加载器仅处理 `KEY=VALUE` 和整行注释。使用 UTF-8 无 BOM，不加引号，不使用变量插值或行尾注释。已有进程环境变量优先于 `.env`；Compose 的显式 `environment` 配置优先于 `env_file`。

修改 `.env` 后重建容器配置，单纯 `restart` 不会刷新容器环境变量：

```bash
docker compose -p im2610-debug up -d --force-recreate --wait
```

`paper` 与 `competition` 使用独立数据库。数据库绑定运行模式和比赛密钥指纹，不能用同一数据库切换账户。轮换比赛密钥也需要处理指纹不一致；先核对旧账户委托并保留备份，不要清库绕过未决订单。

## 6. 连接比赛账户

1. 先在 paper 模式确认网页、行情、参数和日志正常。
2. 在 `.env` 设置 `BOT_MODE=competition`，填入本人比赛账户的 `CFAPIKEY`，保留 `BOT_AUTOSTART=false`。
3. 使用上一节的 `up --force-recreate --wait` 重新创建容器。
4. 在网页核对账户、IM2610 持仓、比赛阶段、可交易合约、委托及成交；完成第 18 节的交易验收、没有锁定或未明委托后再点击“启动策略”。
5. 需要无人值守恢复时，将 `BOT_AUTOSTART=true` 并重新创建容器。

自动启动不会绕过持久化的手动暂停或故障锁定：首次启动且允许自动启动时可以启用；已有数据库只有上次仍启用且未锁定才恢复。点击“核查恢复”只对账并解除可解除的锁定，之后仍需点击“启动策略”。

程序只管理自身记录的订单。已有 IM2610 持仓纳入库存；其他合约不交易，但账户总权益和保证金会影响风控。避免用其他工具同时交易该账户的 IM2610，否则订单归属可能无法确认。

## 7. 网页操作

| 操作 | 行为 |
| --- | --- |
| 启动策略 | 通过校验后进入自动循环 |
| 暂停并撤单 | 停止新增报价并撤本程序挂单，保留持仓 |
| 只减仓至空仓 | 持续被动平仓，空仓后暂停 |
| 核查恢复 | 核对订单，解除有证据可解除的锁定；不自动启动 |
| 保存参数 | 仅暂停且自身未决订单全部终结时可保存 |
| 锁定页面 | 清除浏览器内存中的令牌；后台继续运行 |

页面显示盘口、Microprice、净库存、权益、保证金占比、监控盈亏、委托、成交和日志。关闭浏览器不停止策略。操作命令异步排队，接口返回 202 仅表示接收，执行结果看状态及日志。

收盘减仓时间之后仍有持仓，页面持续显示残仓告警并写入事件；超限库存和保证金占用也会提示。用户已选择继续仅被动平仓：所有买单最多报买一、卖单至少报卖一，不新增主动减仓模式。告警不会自动清仓，也不代表已完成平仓。

停机前先“暂停并撤单”，确认挂单已撤后再停止容器。正常 SIGTERM 会尽力撤单；断电或强制终止不能保证撤单。

## 8. 策略参数

参数存储在 SQLite，经网页保存后重启仍保留，不通过 `.env` 配置。比例填写小数，例如 `0.65` 表示 65%。

| 参数 | 默认 | 范围 | 说明 |
| --- | ---: | --- | --- |
| `poll_seconds` | 10 | 5–300 | 每轮完成后的等待秒数，实际循环还包含请求耗时 |
| `max_quote_age` | 8 | 1–30 | 行情最大允许年龄，秒 |
| `order_lifetime` | 40 | 10–600 | 普通报价最长存续秒数 |
| `reprice_ticks` | 3 | 整数 1–100 | 改价触发跳数 |
| `order_size` | 1 | 整数 1–10 | 单笔手数，不能大于仓位上限 |
| `max_position` | 1 | 整数 1–20 | 总持仓及未成交开仓预留上限 |
| `max_daily_loss` | 3000 | 1–100000 | 当日监控亏损上限，元 |
| `max_use_rate` | 0.65 | 0.01–0.8 | 保证金占权益比例上限 |
| `margin_rate` | 0.20 | 0.08–1 | 新开仓保证金估算比例 |
| `half_spread_ticks` | 2 | 1–100 | 基础半价差，跳 |
| `edge_ticks` | 1 | 0–100 | 最低额外报价优势，跳 |
| `fee_per_side_bps` | **0** | 0–100 | 单边手续费 bps；比赛无手续费时保持 0 |
| `inventory_skew_ticks` | 3 | 0–100 | 每手净库存引起的报价偏移，跳 |
| `imbalance_cutoff` | 0.65 | 0.05–1 | 阻止不利方向新开仓的失衡阈值 |
| `ema_alpha` | 0.5 | 0.01–1 | Microprice EMA 权重 |
| `max_spread_ticks` | 30 | 整数 1–200 | 允许盘口价差上限，跳 |
| `max_orders_per_day` | 100 | 整数 1–1000 | 当日 IM2610 开仓委托数上限，含外部委托 |
| `flatten_time` | `14:50` | `14:00`–`14:59` | 开始只减仓的北京时间 |
| `entry_filter` | `true` | 布尔值 | 启用开仓环境过滤；网页使用复选框 |

首次运行零手续费版本会一次性将旧数据库的费率改为 0，保留订单与成交，不退回历史模拟手续费。非零费率仅用于主动设置成本实验；本地撮合也按该参数计费。

2026-10-02 压力测试后，新部署默认仓位上限由 2 手降为 1 手，减仓时间由 14:55 提前到 14:50。这是降低暴露的调整，未获得收益提升证据；已有数据库参数不自动覆盖，升级后需暂停撤单并在网页保存这两个参数。详见 [回测与上线检查报告](BACKTEST.md)。

当日亏损基准是当天第一次成功采样的权益，重启保留；不是交易所结算盈亏，监控开始前的损益不追溯。当日触发亏损上限后保持当日限制，增大参数不会自动解除。此阈值触发只减仓，**不是保证最大亏损金额**：被动平仓可能不成交，行情跳变和延迟仍会造成更大亏损。

## 9. 策略公式与执行

设买一/卖一价格 `b,a`，数量 `B,A`，净库存 `q=多仓-空仓`，最小跳价 `tick=0.2`：

```text
imbalance = (B - A) / (B + A)
mid       = (b + a) / 2
micro     = (a * B + b * A) / (B + A)
fair      = EMA(micro) - q * inventory_skew_ticks * tick
half      = max(half_spread_ticks * tick,
                mid * fee_per_side_bps / 10000 + edge_ticks * tick)
buy       = floor_tick(min(b, fair - half))
sell      = ceil_tick(max(a, fair + half))
```

Microprice 等于 `mid + imbalance * spread / 2`，不重复叠加同一个失衡信号。失衡阈值只阻止不利方向新开仓，平仓不受该过滤阻止。每点价值按 200 元计算。

默认启用环境过滤：最近 300 秒内至少 6 个独立源时间并覆盖 120 秒，趋势效率（净移动绝对值 / 逐步移动绝对值之和）不超过 0.30，最大单步跳价不超过 20 tick，盘口价差不超过 6 tick，双方一档数量至少 10 手。只通过环境检查仍须满足报价与账户限制。该过滤仅阻止新开仓和撤销不再合适的旧开仓单，原被动平仓计划继续执行；网页显示阻止原因及样本覆盖时间。

启动、暂停、核查恢复或保存参数后重新采样；跨日、午休及超过 90 秒的源时间断档重新暖机。行情错误也清空采样。重复源时间不增加样本，同秒价格、数量和价差变化会立即重查。参数保存还会重置公允价 EMA。固定阈值来自研究假设，不是经真实成交数据校准的收益参数；轮询及请求耗时使独立观察间隔大于 60 秒时，300 秒窗口可能无法满足 6 样本要求，程序持续显示采样不足，不自动放宽门槛。

每侧最多一笔，优先平掉对向库存。总持仓及挂单预留计入风险；达到库存、保证金、亏损限制或收盘减仓时间后限制新开仓。改价先撤旧单，确认撤销后下一轮才下新单，每轮最多一个提交或撤单动作。价格对齐跳价并限制在涨跌停范围。

北京时间 09:30–11:30、13:00–15:00 是时间窗口，程序另外排除 2026 年节假日与所有周末，调休工作周末仍休市；还需比赛阶段、合约和新鲜行情校验通过。只支持 2026 年至 IM2610 到期日 2026-10-16，不自动换月或沿用日历到下一年。

休市日期已与[上交所官方 2026 日历](https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml)核对。中金所《关于2026年部分节假日休市安排的通知》〔2025〕55号作为公告号参考；本次中金所官网原文未成功访问，不能把上交所页面当成中金所原文已核验的证据。若交易所另有临时通知，需更新日历并重验。

比赛接口没有已知 post-only 选项。被动性只约束提交时的报价，行情到达撮合器前可能变化；被动止损和收盘减仓不能保证及时成交或清仓。

## 10. 数据和交易接口

公开行情：

```text
GET https://hq.sinajs.cn/list=nf_IM2610
Referer: https://finance.sina.com.cn/
```

使用中金所 50 字段格式，零基索引：最新价 3、累计成交量 4、涨跌停 9/10、买一价量 16/17、卖一价量 26/27、日期时间 36/37。缺字段、异常盘口、非合法跳价或陈旧时间戳均停止报价，不合成盘口兜底。

2026-09-30 实测收盘快照为买一 `7290.6 / 3 手`、卖一 `7291.2 / 26 手`、北京时间 `15:00:00`。这是一次可用性验证，不代表持续稳定或低延迟保证。默认轮询 10 秒，比赛请求间隔至少 1 秒；实际循环包括多次对账请求，属于轮询程序。

比赛适配接口为 `https://qhsim.eastmoney.com/simCustomCompeteApi/api/skills/action`，支持账户、持仓、委托、挂单、成交、合约查询及模拟下单撤单。公开行情与比赛撮合器不同，可能存在价格和时间差。

参考：[新浪 IM2610](https://finance.sina.com.cn/futures/quotes/IM2610.shtml)、[AKShare 新浪接口实现](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_zh_sina.py)、[中金所 IM 合约规格](https://www.cffex.com.cn/u/cms/www/202207/18190308vxpv.pdf)。

## 11. 订单恢复与故障锁定

```text
PREPARED → ACK → OPEN / PARTIAL → FILLED
                    └→ CANCELLING → CANCELLED / FILLED
提交结果不明 → UNKNOWN → 锁定并对账
撤单结果不明 → UNKNOWN_CANCEL → 锁定并对账
明确拒绝 → REJECTED；明确未发送 → NOT_SENT
```

发送前先提交 SQLite 订单意图。接口没有已知客户端幂等键，也不保证成功回执有稳定订单 ID；程序保存提交前委托计数，以交易日、时间、方向、价格和数量关联唯一新增记录，再关联可撤挂单号。部分成交更新剩余量，跨日重启查询未终结意图所属日期。

不明回执不会自动重发。委托与挂单两次查询间发生状态变化时，只重新读取一次；持续冲突、重复签名、外部挂单或无法确认的重启意图会阻止新委托。已有证据能完成关联时，“核查恢复”可以处理；始终缺证据时需在比赛平台人工核对，程序保持锁定。不要删除数据库或换实例来重试未知订单。

比赛只读查询出现网络故障时停止报价、对已知自身挂单作尽力撤单，并以最长 60 秒退避重连；撤单结果不明会持久化锁定，不能因重连而再次发送。发单在接口限速等待结束后再次检查新鲜行情、交易时段、风险和暂停 / 退出命令，未发送的意图标记 `NOT_SENT`。身份校验失败时不会先修改旧账户参数；进程锁保持到交易线程停止，避免退出期间另一进程接管同一日志。

账户、持仓、挂单和委托风控查询仍在每次对账执行，只有网页成交列表在 competition 模式按 30 秒刷新间隔缓存；核查恢复会强制更新。页面的 `last_trade_sync`、`cycle_seconds` 和 `query_failures` 可分别查看成交显示更新时间、实际循环耗时与查询失败次数。

## 12. 数据保存、备份与恢复

Compose 中策略数据库位于 `/app/data/paper.sqlite3` 或 `/app/data/competition.sqlite3`，真实盘口归档为 `/app/data/l1.sqlite3`，均在命名卷中。普通 `stop`、`restart`、`down` 不删除卷；`down -v` 会删除数据，日常不要使用。

SQLite 使用 WAL 和 `synchronous=FULL`：`settings` 保存参数、开关、基准与每日成交；`intents` 保存订单意图；`intent_observations` 保存首次观察到的状态及剩余量，用于检查部分成交、撤单与成交竞争的证据；策略库 `ticks` 保留最近 10000 个源时间递增的采样；`events` 保留最近 5000 条事件。订单意图不自动归档。数据库含账户和交易信息，不上传 GitHub。

采集器的独立 `l1.sqlite3` 不受策略库滚动窗口影响，不自动删除历史。只保存真实交易时段中、最多 8 秒年龄、源时间递增的有效 L1；重复、倒退、过期及异常数据不写入。其 `settings` 的 `collector` 键保留有限状态与计数：心跳、最后有效写入 / 源时间、接收数、重复数、回退数、故障数及连续故障数。健康要求心跳在 90 秒内；交易时段另要求近期有效写入，休市等待不算故障。归档会持续增长，应监控卷空间并按第 18 节制作一致性副本。

使用 SQLite backup API 获取一致备份，不直接复制运行中的主数据库而漏掉 WAL。以下以 paper 为例，competition 替换文件名：

```bash
docker compose -p im2610-debug exec -T bot python -c "import sqlite3; s=sqlite3.connect('/app/data/paper.sqlite3'); d=sqlite3.connect('/app/data/paper-backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose -p im2610-debug cp bot:/app/data/paper-backup.sqlite3 ./paper-backup.sqlite3
```

恢复前暂停撤单、停止服务、备份当前数据库并核对平台委托。旧备份无法代表备份后新增的交易：

```bash
docker compose -p im2610-debug stop
docker compose -p im2610-debug run --rm --no-deps -v "$PWD/paper-backup.sqlite3:/tmp/restore.sqlite3:ro" bot python -c "import sqlite3; s=sqlite3.connect('file:/tmp/restore.sqlite3?mode=ro',uri=True); d=sqlite3.connect('/app/data/paper.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose -p im2610-debug up -d --wait
```

上述恢复命令使用 Linux shell。PowerShell 的卷路径使用 `${PWD}/paper-backup.sqlite3:/tmp/restore.sqlite3:ro`。确认文件存在且容器用户可读。恢复后先对账再启动；不要并行恢复与运行。

## 13. 升级

先暂停并撤单、检查平台、备份数据库，然后：

```bash
git pull --ff-only
docker compose -p im2610-debug up -d --build --wait
docker compose -p im2610-debug logs --tail=100 bot
```

核对参数、模式、账户和订单后再启用。回退代码可检出已知提交并重新构建；数据库兼容性需单独检查，不应把代码回退等同于交易记录回退。

## 14. 检查与测试

安装本机依赖后：

```bash
python -m unittest -v test_bot test_robustness test_collector test_data_quality
python scripts/benchmark.py --output artifacts/benchmark.json
python scripts/probe_feed.py
python scripts/check_debug.py
python scripts/check_connections.py
```

离线测试不读密钥、不访问网络；覆盖行情、风控、零手续费、数据库迁移、订单不明结果、部分成交、跨日恢复和网页鉴权。`probe_feed.py` 只读公网行情；`check_debug.py` 只读本机 paper 服务；`check_connections.py` 加载 `.env` 并只读比赛账户，需账户密钥及后端依赖。

Linux Docker 中运行离线测试：

```bash
docker compose -p im2610-debug run --rm --no-deps -e PYTHONPATH=/tmp/check -v "$PWD:/tmp/check:ro" bot python -m unittest -q test_bot test_robustness test_collector test_data_quality
```

PowerShell 把挂载参数写为 `"${PWD}:/tmp/check:ro"`。该测试挂载项目目录供 Python 发现全部测试模块；测试本身不加载环境配置、不发起网络请求。`probe_platform.py` 直接使用进程环境中的 `CFAPIKEY`，不加载 `.env`；不要为探测把密钥写进命令历史。

本轮四个模块共 56 项离线测试已在 Windows Python 3.14 / Linux Python 3.13 容器通过，覆盖查询故障恢复、最终发送检查、身份隔离、状态竞争、采集退出、数据覆盖和固定参数样本外流程。bot 与 collector 均健康、本机 HTTP 14 项预检及比赛只读连接再次通过；休市采集器状态为 `waiting_session`、真实接收数为 0。**比赛下单和撤单写接口尚未实际发单验收，不能据此宣告策略已通过交易上线验收。**

paper 撮合要求后续快照有新增成交量，最新价及对手盘穿过委托价才按成交增量部分成交，触价不假定成交。没有真实排队与逐笔数据，模拟结果不能证明策略盈利。

## 15. API

除健康检查外，API 需要 `Authorization: Bearer <BOT_TOKEN>`。写请求还校验同源 Origin。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 进程及循环健康，不需要令牌 |
| GET | `/api/state` | 状态、行情、账户、委托、成交和日志 |
| POST | `/api/control` | 排队执行控制命令 |
| PUT | `/api/config` | 保存完整参数对象 |

控制命令和参数字段以 `app.py`、`web/app.js` 为准，推荐通过网页操作。配置 API 的缺省字段会回到默认值，因此提交完整参数对象，不把它当局部更新接口。健康检查通过不等于策略启用，也不等于没有故障锁定。

## 16. 常见问题

| 现象 | 处理 |
| --- | --- |
| Docker 无法连接 | 启动 Docker Desktop / Docker 服务，确认 Linux 容器模式 |
| 缺少 `.env` | 在项目根目录执行 `scripts/init_env.py` |
| 令牌太短或 401 | 使用本机 `.env` 生成的 `BOT_TOKEN`，不是 `CFAPIKEY` |
| 写接口 403 | 检查同源访问；反向代理需处理可信协议传递 |
| 8000 已占用 | 停止另一实例或使用另一 SSH 本地端口 |
| 修改 `.env` 没生效 | `up -d --force-recreate --wait`，检查进程环境覆盖 |
| 容器健康但不交易 | 检查暂停、锁定、交易时段、比赛阶段、行情时效和风险阈值 |
| 无新鲜行情 | 检查出站网络和系统时间；收盘及节假日等待开市 |
| 无法保存参数 | 先暂停撤单，等自身订单全部终结 |
| 不明订单锁定 | 在平台核对并“核查恢复”，不要删除记录重发 |
| 账户指纹不一致 | 核对模式、密钥、数据卷，保留旧库并处理旧账户委托 |
| 重启后数据消失 | 检查项目名和数据卷是否改变，是否执行了 `down -v` |
| 减仓后仍有持仓 | 被动限价单可能不成交，检查平台持仓和挂单 |
| 锁定页面后仍运行 | 页面锁定不暂停后台，需“暂停并撤单” |

## 17. 私密仓库与源码包

仓库：[ho2006/im2610-bot](https://github.com/ho2006/im2610-bot)。`.gitignore` 和 `.dockerignore` 排除真实环境配置、SQLite、虚拟环境、缓存和运行产物，`.env.example` 保留。

```bash
python scripts/package.py
```

源码包生成于 `artifacts/im2610-bot.zip`。部署时在目标机器重新生成 `.env`；数据库备份与密钥独立保存，不放进源码包或仓库。

## 18. 回放与上线前检查

没有经过验证的 IM2610 历史 L1 时，不用 OHLC 或编造的买卖量拟合策略收益。当前已留存真实数据只有一个源时间快照；压力测试是明确标记的合成路径，不是历史收益回测。工具直接复用生产执行引擎和 SQLite 查询，虚拟时间及内存数据库只用于离线加速。

采集器随 Compose 启动，先检查状态与数据覆盖；没有新鲜交易时段记录时等待开市，不用改写收盘时间戳填充。需要回放时，对归档制作 SQLite 一致性副本再评估；正在采集的库仍可继续运行：

```bash
docker compose -p im2610-debug exec -T collector python -c "import sqlite3; s=sqlite3.connect('/app/data/l1.sqlite3'); d=sqlite3.connect('/app/data/l1-validation.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose -p im2610-debug cp collector:/app/data/l1-validation.sqlite3 ./data/l1-validation.sqlite3
```

本机先创建 `data` 目录。历史策略库 `data/backtest-source.sqlite3` 仍可用来检查已有记录，但不能代替连续采集的 L1。回放和预检须使用同一份固定副本，否则新增行情会使数据哈希不匹配：

```bash
python scripts/backtest.py --db data/l1-validation.sqlite3 --output artifacts/backtest.json
python scripts/preflight.py --history data/l1-validation.sqlite3 --journal data/competition.sqlite3 --backtest-report artifacts/backtest.json
```

回放也接受 CSV 或每行一条 JSON：

```bash
python scripts/backtest.py --db data/im2610-l1.csv --output artifacts/backtest.json
python scripts/backtest.py --stress-only
```

必需字段为 `bid,ask,bid_size,ask_size,last,volume,upper,lower,timestamp,received`，时间为 Unix 秒；`volume` 为当日累计量，`received` 为实际采集时刻，不能把陈旧行情时间改成接收时间。可选 `symbol` 只能为 `IM2610`。错误盘口、缺字段、非有限数、非法跳价和倒序采样会被计数并排除；报告保留数据哈希和唯一新鲜源时间数。

质量门槛为至少 5 个完整交易日、1000 个唯一新鲜源时间；每个上午 / 下午时段的分钟覆盖率至少 95%，含开市与收市边界的最长缺口不超过 60 秒。不能用几个不完整日期或重复采样满足门槛。门槛仅说明数据可继续评估，不证明策略盈利。

`artifacts/backtest.json` 保存真实记录质量、历史回放和 84 组合成压力结果；假设包括单边费率 0、每轮单次变更、10/20 秒对账开销、3/10 秒提交及撤单延迟、10% 成交量参与率，比较穿价与乐观触价成交。模型没有真实排队位置，也没有平台成交规则的校准。

结果分别保留已实现盈亏、期末按中间价估值盈亏、最大回撤、剩余持仓和挂单，不强行把期末残仓当成已平仓成交。行情质量不足时不自动拟合 EMA 或失衡参数。

数据满足门槛时，按完整日期的时间顺序前 60% / 后 40% 分为训练与测试区间，固定比较既有两套参数，分别使用穿价和乐观触价模型；不按照测试表现反向挑参数。每个交易日从 100 万权益、空仓独立启动，残仓只报告、不跨日结转或复利，因此它是逐日实验，不能解释为连续持仓的跨日账户收益。数据不足时 `out_of_sample.status=insufficient_data`，不强行给出通过结论。

`preflight.py` 只连接本机 8000 端口，不下单、不撤单、不启动策略；验证网页资源、14 项接口与配置检查，并输出 `artifacts/preflight.json`。`--history` 指定真实 L1 副本，`--journal` 指定 competition 日志，`--backtest-report` 指定回放报告；默认分别为 `data/l1.sqlite3`、`data/competition.sqlite3`、`artifacts/backtest.json`。验收推荐使用上述固定 `l1-validation.sqlite3` 副本。它检查实际覆盖、相同 L1 内容哈希 `l1_sha256`、当前配置哈希及样本外结果；源文件 `sha256` 仅作文件追溯，不能替代包含 WAL 记录的数据内容核对。

比赛日志还必须绑定当前 `CFAPIKEY` 指纹，具有受理、撤销、成交记录，且没有未终结意图。部分成交及撤单竞争仍单独报告；默认单手委托 `partial_check_required=false`，不要求为验收人为制造无法拆分的单手部分成交，改为多手委托时要求真实部分成交证据。

这些观察只能证明发生过相应状态，不能证明比赛费用、冻结保证金和撮合规则已校准，工具会保留这部分核验条件，没有用审批勾选框或 paper 撮合伪造验收。`engineering_pass=true` 仅表示工程检查通过，`go_live_ready` 由实际缺口决定；当前仍为 `false`。

工具已包含在容器里，可运行：

```bash
docker compose -p im2610-debug exec -T bot python scripts/backtest.py --db /app/data/l1-validation.sqlite3 --output /tmp/backtest.json
docker compose -p im2610-debug exec -T bot python scripts/preflight.py --history /app/data/l1-validation.sqlite3 --journal /app/data/competition.sqlite3 --backtest-report /tmp/backtest.json --output /tmp/preflight.json
```

上线前须单独核对比赛账户已有持仓与保证金占用，且不能用 paper 的成功撮合替代比赛写接口验收。当前保持 paper 模式和策略暂停。

## 19. 性能与健壮性改进

历史页面直接从 SQL 读取最近 100 笔意图的显示字段，增加排序 / 活跃状态索引，避免每次刷新读取全部订单；未变化的参数和意图不重复写入，也不移动撤单确认的原始截止时间。SQLite 的订单提交前持久化及 `FULL` 同步保留。

本机 Windows、20000 条意图、15 次查询的同条件基准如下；它测量本地 SQLite 与 Python 分配，不代表新浪或比赛接口网络延迟：

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| `history()` 中位耗时 | 85.818 ms | 4.230 ms |
| `tracemalloc` 记录的峰值分配 | 16,499,464 字节 | 42,768 字节 |
| 20 组相同 `put` / `update` 的 SQL 行写入 | 40 | 0 |

复现命令为 `python scripts/benchmark.py --output artifacts/benchmark.json`。部署机器、磁盘及已有数据会改变结果；本轮不新增 Redis、缓存服务或异步交易架构。

## 20. 本地运行与维护

当前默认使用本机 Docker Desktop 的 Linux 容器，项目目录为 `C:\dev\zhongjin`，Compose 项目名保持 `im2610-debug`，数据卷保持 `im2610-debug_bot-data`。继续使用现有 `.env` 和数据库，日常停止、恢复不需要重新初始化配置。

2026-10-02 已完成本地切换：`bot` 和 `collector` 均健康，服务仅监听本机 `127.0.0.1:8000`；本机纸面模式检查及 14 项工程预检全部通过，实际状态为 `paper`、手续费 0、`enabled=false`。原远程应用容器已停止，原数据卷与备份保留。

当前 `go_live_ready=false`：真实 L1 记录不足，样本外评估及比赛下单 / 撤单写接口尚未完成验收。工程检查通过不代表策略收益或比赛交易已通过上线验收。

只有电脑开机、Docker Desktop 运行且容器启动时，网页、采集器和策略服务才可用；联网后才能获取公开盘口及连接比赛接口。电脑关机、休眠或 Docker Desktop 退出期间不会持续采集或执行策略。关闭浏览器不会停止后台服务。

恢复已有项目时，在本机 PowerShell 执行：

```powershell
Set-Location C:\dev\zhongjin
docker compose -p im2610-debug up -d --wait
docker compose -p im2610-debug ps
```

打开 [本地控制台](http://localhost:8000)，使用本机 `.env` 中的控制台令牌登录。首次安装或代码更新时，使用第 2 节带 `--build` 的启动命令；修改 `.env` 后按第 5 节重新创建容器。

查看两个服务的日志及采集器状态：

```powershell
docker compose -p im2610-debug logs --tail=100 bot collector
docker compose -p im2610-debug exec -T collector python scripts/collect_l1.py --db /app/data/l1.sqlite3 --health
```

需要关机或停止 Docker Desktop 时，先在网页“暂停并撤单”，确认本程序挂单已终结，再停止服务：

```powershell
docker compose -p im2610-debug stop
```

停止后仍保留 SQLite 策略记录、配置和真实盘口归档；再次使用同一项目名执行 `up -d --wait` 会复用原卷。不要运行 `down -v` 或删除 Docker 数据卷。数据库备份与恢复使用第 12 节的 SQLite backup API。

默认配置为 `paper`、`BOT_AUTOSTART=false` 和策略暂停；需要启用时在网页操作。是否满足比赛交易条件仍按第 18 节检查，网页或容器健康不代表交易已通过上线验收。

## 21. 策略研究与环境过滤

`codex/strategy-research` 增加离线比较工具，并将环境过滤接入现有策略，默认 `entry_filter=true`。原 OBI / 加权中间价报价继续生成，环境检查仅过滤开仓；一手上限、被动退出和残仓告警继续执行。缺少该字段的旧数据库按默认开启处理，升级不会自动启动策略。结论与证据见 [策略研究报告](STRATEGY_RESEARCH.md)，本届评分、费用冲突和数据来源见 [一手资料核验](STRATEGY_SOURCES.md)。零手续费仍是用户指定的运行情景，尚未通过比赛结算流水核实；研究另测公开费率，不修改运行费率。

```bash
python scripts/research_strategies.py --stress-only
python scripts/research_strategies.py --stress-only --production-check --output artifacts/strategy-improvement.json
python scripts/research_minutes.py --input artifacts/im2610-minute-source.json
python -m unittest -q test_strategy_research test_research_minutes test_entry_filter
```

普通模式运行 864 组合成压力比较；`--production-check` 追加实际引擎过滤路径，共 1080 次回放，输出生产接入与研究过滤的逐场景差异。默认买侧优先的生产检查不替换报价函数；卖侧优先仍为排序敏感性实验。分钟命令使用已保存的真实 IM2610 十五分钟数据，只诊断方向信号，不模拟被动成交。分钟原始数据不随源码分发，输入格式和来源见研究报告。工具不加载账户密钥、不连接比赛、不自动调参或启动策略。当前没有证据证明新方案已同时提高实际收益并降低实际风险。

## 22. 开源许可与发布范围

代码及随附原创文档采用 [MIT 许可证](LICENSE)，版权署名为 `2026 ho2006`；第三方依赖、平台、引用文献和数据仍遵循各自条款。公开仓库使用最新源码的独立历史，保留原私密开发历史；不包含账户凭证、私钥、数据库、原始盘口或本机运行记录。研究报告中的原开发提交号用于说明当时的版本，不对应公开仓库历史。
