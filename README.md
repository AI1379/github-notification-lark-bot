# larkbot · GitHub → 飞书通知机器人

把 GitHub 仓库的动态推到飞书群，并且**每个群可以单独配置关心哪些仓库 / 哪些事件**。

- **GitHub 侧双通道**：`webhook` 实时推送 + `Events API` 轮询回退（webhook 挂了也不会漏）
- **飞书侧双通道**：群「自定义机器人 webhook」（最省事）或「自建应用 + chat_id」（可控性强）
- **投递去重**：去重粒度是 `(事件, 群)`，webhook 与轮询同时开启也不会重复刷屏
- **配置驱动路由**：仓库支持 `org/*` 通配，可按事件类型 / action / 分支 / actor / label 过滤

技术栈：Python 3.11+ / FastAPI / httpx / aiosqlite / uv。单进程即可运行。

---

## 1. 架构

```
                    ┌──────────────────────────────┐
   GitHub ─webhook─▶│  POST /webhooks/github       │─┐
                    │  · HMAC-SHA256 签名校验       │  │
                    └──────────────────────────────┘  │
                                                      ▼
   GitHub ◀─ETag───┐                            ┌───────────────┐
   Events API      │                            │ 归一化 RepoEvent│
                   │                            │ + dedup_key    │
        ┌──────────┴───────────┐                └───────┬───────┘
        │  Poller（后台周期任务）│                       │
        │  · 游标 + 回溯窗口     │───────────────────────┘
        │  · webhook 健康就跳过  │
        │  · 首轮只打基线        │                       ▼
        └──────────────────────┘              ┌──────────────────┐
                                              │ Router 按群解析   │
                                     ┌────────┤ repos/events/... │
                                     │        └──────────────────┘
                                     ▼                 │
                          ┌──────────────────┐         │
                          │ SQLite 去重状态库 │◀────────┘
                          │ (event, chat)    │
                          └──────────────────┘
                                     │
                                     ▼
                          ┌──────────────────┐     ┌──────────────┐
                          │ 飞书卡片渲染      │────▶│ 自定义机器人  │
                          │ interactive card │     │ 或 自建应用   │
                          └──────────────────┘     └──────────────┘
```

### 为什么 webhook 和轮询能共存而不重复

两条链路拿到的负载字段并不一样（push 事件 webhook 里是 `after`，Events API 里是 `head`），
所以 `src/larkbot/github/normalize.py` 会把它们归一化成同一条 `RepoEvent`，并生成**内容派生**的
`dedup_key`，例如：

| 事件 | dedup_key |
| --- | --- |
| push | `push\|owner/repo\|refs/heads/main\|<head_sha>` |
| PR | `pull_request\|owner/repo\|42\|closed\|<updated_at>` |
| Release | `release\|owner/repo\|300001\|published` |

送消息之前先查状态库：`(dedup_key, chat)` 已经成功投递过就跳过。因此某个群推送失败时，
**下一轮只会补发给失败的群**，已收到的群不会被重复打扰。

去重的**裁定**用的是单条 SQL 的原子抢占（`claim_delivery`）：无记录则插入，记录是 `failed`
（或 `pending` 已超时）则抢占，`ok` 与正在发送中的行不可抢占。之所以不能写成
“先查一下 → 再发 → 再写记录”，是因为同一次 GitHub 事件可能被**两个 webhook 同时投递**
（实测相隔 11ms）：并发下两边都会在对方写记录之前通过检查，结果同一个群收到两条消息。
改成原子抢占后，无论到达顺序如何都只发一条。

### 投递失败怎么办（有界重试）

失败时会把事件快照存进状态库，调度器每轮开头先做一次重试扫描：

- 重试**不依赖 GitHub**：webhook 已发过、轮询又被跳过的事件也能补发；
- 重试范围以**首次失败时间**为界（默认 60 分钟，`delivery.retry_max_age_minutes`），避免无限重试；
- 即使 `poll_mode: never`（不轮询 GitHub），调度器仍在跑，只负责重试投递；
- 仍未成功的记录会出现在 `/status` 的 `store.recent_failures`，并计入 `store.pending_retries`。

---

## 2. 完整配置流程（从零到第一条消息）

整条链路只有三段需要你决策：**GitHub 给 larkbot 看什么** → **飞书用哪条通道发** → **哪个群收哪些消息**。
下面按顺序走一遍，细节分别展开在 §3 / §4 / §5。

### 2.1 速通（凭证已经有的时候）

```bash
uv sync
cp .env.example .env            # 填 4~6 个值
cp config/config.example.yaml config/config.yaml
uv run larkbot check            # 缺什么会逐条列出来（有问题退出码 1）
uv run larkbot send-test dev-frontend   # 确认这个群真的能收到
uv run larkbot serve
```

`larkbot` **会自己加载当前目录的 `.env`**（解析交给 python-dotenv，已存在的环境变量优先），
所以手动跑 CLI 和用 systemd 跑都不会出现“明明填了却报未配置”的情况。
`.env` 不在当前目录时用 `LARKBOT_ENV_FILE=/path/to/.env` 指定。

### 2.2 第 1 步：GitHub 侧 —— 让 larkbot 看得到仓库

两张「门票」，可以只用其一，也可以都开（**推荐都开**，互为备胎）：

**A. Webhook（实时推送）** 仓库 → Settings → Webhooks → Add webhook：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://<你的域名>/webhooks/github` |
| Content type | `application/json` |
| Secret | 与 `.env` 的 `GITHUB_WEBHOOK_SECRET` 完全一致 |
| Which events | 选 **Send me everything**（过滤交给 larkbot 的 config，改规则不用回 GitHub 改） |

组织级 webhook 同理，放在 Organization → Settings → Webhooks。没有公网地址时用内网穿透（cloudflared / ngrok / frp）。

**B. 轮询（兜底）** 在 `.env` 填 `GITHUB_TOKEN`：

- 建议用 fine-grained PAT，权限只给 **Contents: Read** 就够（Events API 只需读）
- 不填：匿名 60 次/小时、**看不到私有仓库**，只适合先试跑
- 填了：5000 次/小时，能读该 token 可访问的私有仓库

两条通道同时开**不会重复推送**（靠 dedup_key 去重）。轮询策略、首次基线、
通配订阅的「已知仓库」限制见 §5 的 `github` 表。

### 2.3 第 2 步：飞书侧 —— 决定用哪种机器人

先回答一个问题，它决定了后面的所有步骤：

| 你的需求 | 选哪种通道 |
| --- | --- |
| 几个固定群收通知，越省事越好 | **自定义机器人 webhook**（群管理员点一下就行） |
| 群很多 / 要群内指令 / 要卡片按钮 / 跨群管理 | **自建应用 + chat_id** |

**只做通知（自定义机器人）**：每个群各配一个

1. 群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人**
2. 安全设置勾「签名校验」，密钥填 `.env` 的 `FEISHU_WEBHOOK_SECRET_<名>`
3. 复制 webhook 地址填 `.env` 的 `FEISHU_WEBHOOK_<名>`

**要群内指令（自建应用）**

1. [开发者后台](https://open.feishu.cn/app) 建企业自建应用 → 拿 `App ID` / `App Secret`
2. **应用能力 → 添加「机器人」**
3. **权限管理**按需开通：
   - `im:message:send_as_bot` —— 发消息（必需）
   - `im:chat:readonly` —— 列群、查群主（用 `larkbot chats` / 群主权限时需要）
   - `im:message.group_at_msg:readonly` —— 收群里 @机器人 的消息（群内指令需要）
   - `application:app_slash_command:write` / `:read` —— 注册 `/` 指令面板（可选）
4. **创建版本并发布** —— 能力/权限/事件订阅的改动**不发版不生效**（最常见的坑）
5. 把机器人拉进目标群，然后拿 `chat_id`：

```bash
uv run larkbot chats --yaml     # 列出机器人在的群，直接生成可粘贴的 chats 片段
uv run larkbot check --verify   # 确认凭证可用 + chat_id 对得上
```

6. 要用群内指令，再配一步事件订阅：请求地址填 `https://<你的域名>/webhooks/feishu`，
   订阅 `im.message.receive_v1`，Encrypt Key **留空**，Verification Token 填进 `.env`（详见 §5.1）

### 2.4 第 3 步：哪个群收哪些消息（唯一需要你设计的地方）

配置就两张表，一一对应：**`chats` 给群起别名并绑通道**，**`subscriptions` 说「哪些仓库的哪些事件进哪些别名」**。

```yaml
chats:                                  # 表 1：群 = 别名 + 通道
  - {name: frontend, transport: webhook, webhook_url: env:FEISHU_WEBHOOK_FE}
  - {name: release,  transport: app,     chat_id: oc_xxxxxxxx}

subscriptions:                          # 表 2：规则
  - {repos: ["myorg/web-*"], chats: [frontend]}                    # 前端仓库 → 前端群
  - {repos: ["myorg/*"], chats: [release], events: [release]}      # 所有仓库的 Release → 发布群
```

三个要点：

- `name` 是**你起的别名**，跟飞书群名无关，只在这份 YAML 里有效——不用去飞书登记什么
- 一个群想收多类消息**不需要多个机器人**，同一个别名被多条规则命中即可（自动去重）
- 想要例外就再写一条更具体的：多条订阅是**叠加**关系

三套现成模板（按团队分群 / 按事件严重度分群 / 一仓库多环境）见 §5.4。

### 2.5 第 4 步：验收

```bash
uv run larkbot check                                                    # 配置/权限缺什么
uv run larkbot check --verify                                           # 联网验证 app 凭证 + chat_id
uv run larkbot send-test frontend                                       # 这个群真的能收到
uv run larkbot simulate --repo myorg/web-api --event push --print-card  # 卡片长什么样（不发送）
uv run larkbot simulate --repo myorg/web-api --event release            # 命中哪些群
```

`simulate` 的 stdout 是纯 JSON，可以直接接 `jq`。改完 YAML 不用重启：

```bash
curl -X POST localhost:8000/admin/reload -H 'X-Admin-Token: <token>'
```

### 2.6 第 5 步（可选）：开启群内指令

开启后群主可以在群里 `@机器人 sub acme/api` 自助增删订阅，见 §5.1。
如果还想让这些指令出现在客户端的 `/` 面板里（纯可发现性优化，飞书客户端 PC 7.70+）：

```bash
uv run larkbot slash --register --dry-run   # 先看会发什么请求体，不联网
uv run larkbot slash --register             # 注册（幂等，已存在的默认跳过）
uv run larkbot slash                        # 查看已注册的
```

---

## 3. GitHub 侧细节

### 3.1 Webhook（实时通道）

仓库 → Settings → Webhooks → Add webhook：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://<你的域名>/webhooks/github` |
| Content type | `application/json` |
| Secret | 与 `.env` 的 `GITHUB_WEBHOOK_SECRET` 完全一致 |
| Events | 建议选 “Send me **everything**”（过滤交给 larkbot 的配置做，改配置不用回 GitHub 改） |

组织级 webhook 同理，放在 Organization → Settings → Webhooks。

本地调试可以用内网穿透（cloudflared / ngrok / frp）把 `127.0.0.1:8000` 暴露出去：

```bash
cloudflared tunnel --url http://localhost:8000
```

> 签名校验用的是 `X-Hub-Signature-256`（HMAC-SHA256，兼容 `sha1`）。
> **没有配置 secret 时，webhook 入口会直接返回 `503` 拒绝所有请求**（安全默认值）——因为一个不验签的
> 公开端点等于“任何人都能伪造 GitHub 事件往群里发消息”。本地调试如果确实不想配 secret，
> 显式设 `github.allow_unsigned_webhooks: true`。

### 3.2 Token（轮询通道）

`GITHUB_TOKEN` 是可选的，但强烈建议配置：

- 未配置：走匿名请求，限额只有 60 次/小时，且**看不到私有仓库**的事件
- 配置了：限额 5000 次/小时，能读取该 token 有权访问的仓库

建议用 **fine-grained PAT**，权限只给 `Contents: Read`（Events API 只需要读权限）；
只是轮询公共仓库的话，经典 PAT 的 `public_repo` 就够。

轮询会用到：

```
GET /repos/{owner}/{repo}/events?per_page=50   # 带 ETag 条件请求，304 几乎不消耗限额
```

注意 Events API 只保留最近 90 天、且最多 300 条事件。

---

## 4. 飞书侧配置

飞书侧首先要建立「把消息塞进某个群」的**通道**，然后由 larkbot 的 config 决定*哪个群收哪些消息*。支持两种通道，可以混用。

|  | `transport: webhook` 自定义机器人 | `transport: app` 自建应用机器人 |
| --- | --- | --- |
| 怎么建通道 | 群 → 设置 → 群机器人 → 添加「自定义机器人」，复制 webhook URL | 开发者后台建自建应用 → 加机器人能力 → 发布版本 → 把机器人拉进群 |
| 一个通道覆盖几个群 | **只能 1 个**（webhook URL 本身就是那个群的句柄） | 一个应用可进任意多个群，用 `chat_id` 指定目标 |
| 能否列出群列表 / 读群成员 | ❌ | ✅（需 `im:chat:readonly`） |
| 能否被 @ 触发、卡片按钮回调 | ❌ | ✅ |
| 发送接口 | 该 webhook 地址（`bot/v2/hook/...`） | `POST /open-apis/im/v1/messages` |
| 成本 | 零，群管理员点一下即可 | 需配权限 + 创建版本 + 发布生效 |
| 限制 | 只能往這一个群发，拿不到其他群 | 机器人必须在目标群内且有发言权限；同一群限频 5 QPS（群内机器人共享） |
| 适用 | 几个固定群、只想收通知 | 群很多、需要交互能力、想自动发现群 |

> 官方文档明确：`im/v1/messages` **仅支持开发者后台创建的应用机器人调用，群自定义机器人无法调用该接口**；反之自定义机器人只能用它的 webhook 地址。两条路完全分开，所以 config 里每个群要写清楚走哪条。

> **另外：自定义机器人是单向的** —— 只能发消息，收不到任何东西。所以「群内指令」只能用在 `transport: app` 的群上（见第 5.1 节）。

### 4.1 自定义机器人（`transport: webhook`，推荐先跑通这个）

1. 群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人**
2. 安全设置建议勾选「签名校验」，把密钥填到 `.env` 的 `FEISHU_WEBHOOK_SECRET_<群名>`
3. 复制 webhook 地址到 `FEISHU_WEBHOOK_<群名>`

局限：一个 webhook 只对应一个群，机器人不能主动读群列表、不能被 @ 触发交互、不能往别的群发。
本项目的用法正好匹配——**每个群一个 webhook，用哪个群收哪些仓库由 config 决定**。

### 4.2 自建应用（`transport: app`，需要跨多群或交互能力时用）

1. [飞书开放平台](https://open.feishu.cn/app) 建企业自建应用 → 拿 `App ID` / `App Secret`
2. **应用能力 → 添加「机器人」能力**（不开启就无法收发消息）
3. **权限管理** 开通应用身份权限：
   - `im:message:send_as_bot`（以应用身份发消息）——发送必需
   - `im:chat:readonly`（获取群组信息）——要用下面的“列出机器人在哪些群”与群主权限时需要
   - `im:message.group_at_msg:readonly`（接收群里 @机器人 的消息）——群内指令需要（§5.1）
   - `application:app_slash_command:write` / `:read` ——把指令注册到 `/` 面板时需要（§2.6）
4. **创建版本并发布**：能力与权限变更必须发版后才生效
5. 把机器人拉进目标群（机器人必须在群内、且有发言权限才能发消息）
6. 拿群 `chat_id`（格式 `oc_` 开头）—— 用内置命令最省事：
```bash
uv run larkbot chats              # 列出机器人在的群：群名 / chat_id / 成员 / 是否外部群
uv run larkbot chats --yaml       # 直接生成可粘进 config.yaml 的 chats 片段
uv run larkbot chats --json       # 给脚本用
```

输出示例：

```
群名称        chat_id    成员  群模式  外部群
------------  ---------  ----  ------  ------
产品研发群    oc_mock_1  12    group   否
Release Room  oc_mock_2  5     group   是
```

7. 加进 config，然后用 `check --verify` 确认机器人在这个群里：

```yaml
chats:
  - name: plan-a          # 别名由你起，subscriptions 里引用它
    transport: app
    chat_id: oc_mock_1
```

```bash
uv run larkbot check --verify
# 联网验证结果（示例）：
#   [OK] 凭证可用，机器人当前在 2 个群里
#   [OK] chat plan-a (oc_mock_1) → 群「产品研发群」
#   [!] chat oncall (oc_wrong_id) → 不在机器人所在的群列表里
```

不想用 CLI 的话，也可以直接打接口：

```bash
TOKEN=$(curl -s -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"cli_xxx","app_secret":"xxx"}' | jq -r .tenant_access_token)

curl -s "https://open.feishu.cn/open-apis/im/v1/chats?page_size=20" \
  -H "Authorization: Bearer $TOKEN" | jq '.data.items[] | {chat_id, name}'
```

`tenant_access_token` 由 larkbot 自动缓存与刷新（提前 60s 过期）。

权限与常见报错：`larkbot chats` / `check --verify` 报错时会直接列出排查顺序（机器人能力、`im:chat:readonly`、
创建版本并发布、凭证是否正确、机器人是否在群里）。要记住的只有一点：**飞书的能力/权限改动必须
「创建版本并发布」后才生效**，这是最常踩的坑。退出码：`0` 正常，`1` 有问题，`2` 缺必要凭证。

关于外部群（成员含其他企业）：自建应用机器人默认进不了外部群，需要为应用开启**对外共享能力**
（仅企业自建应用支持）；而自定义机器人不受这个限制。因此跨企业的群推荐用 `transport: webhook`。

### 4.3 群与组织架构的关系（容易踩的认知坑）

组织架构（部门、上下级、负责人）和「群」是**两套完全独立的对象**：

- 普通群只关心“成员 + 群主”，成员可以来自任何部门甚至外部企业，与部门树没有绑定关系
- 只有**部门群 / 全员群**由管理员按组织架构自动维护；普通群不受此影响
- 因此「往某个部门推消息」在飞书里**做不到**，能做的粒度就是具体的群

这带来一个直接结论：**larkbot 的配置单位是「群」，不是「部门」**。如果你想让“前端组的人收到前端仓库的通知”，
就在飞书里建一个前端群、给它配一个通道，然后在 config 里把仓库指向这个群。若确实要按人员而不是按群分发，
只能在订阅里用 `ignore_actors` / 事件类型做过滤，或者让卡片里 @ 到具体的人（见第 9 节待办）。

---

## 5. 配置说明（`config/config.yaml`）

任何字符串都可以写成 `env:变量名`，运行时从环境变量取值，避免把密钥写进仓库。

### chats：推送目标（一个 chat = 一个飞书群）

| 字段 | 说明 |
| --- | --- |
| `name` | 群标识，被 `subscriptions[].chats` 引用 |
| `transport` | `webhook`（自定义机器人）或 `app`（自建应用） |
| `webhook_url` / `webhook_secret` | `transport: webhook` 必填 url，开通签名校验时填 secret |
| `chat_id` | `transport: app` 必填 |
| `enabled` | 关掉后该群静默，配置可以留着 |

### subscriptions：谁关心什么

```yaml
subscriptions:
  - repos: ["myorg/*", "octocat/Hello-World"]  # 支持 * ? 通配；[] 里的字符按字面量处理
    chats: [dev-frontend, releases]
    events: [push, pull_request, release]      # 支持别名 pr / issue / comment
    actions: [opened, closed, merged]          # 匹配 action，或 merged / conclusion / review_state
    branches: [main, "release/*"]              # 对带分支名的事件生效（push、PR 的 base 分支等）
    ignore_actors: ["*[bot]"]                  # 匹配触发人**和作者**
    ignore_labels: ["skip-ci"]
    ignore_drafts: true                        # 草稿 PR 不推
```

合并规则：

- `events` / `actions` / `branches` / `ignore_drafts`：`null`（不写）→ 继承 `defaults`；写了 → 覆盖
- `ignore_actors` / `ignore_labels`：`null` → 继承；写了 → **覆盖**（写 `[]` 表示显式不做任何过滤，
  例如想让机器人发的 Release 也推送）
- 多条订阅可以叠加命中同一个群，群会被自动去重

### github：轮询策略

| 字段 | 说明 |
| --- | --- |
| `poll_mode` | `auto`（推荐，最近收到过 webhook 就跳过该仓库）/ `always`（双保险）/ `never`（只靠 webhook，但仍保留失败投递重试） |
| `poll_interval_seconds` | 轮询周期，实际会加 ±10% 抖动 |
| `poll_overlap_seconds` | 游标回溯窗口，防止边界丢事件（重复靠去重兜住） |
| `webhook_freshness_seconds` | `auto` 模式下判定「webhook 还健康」的时间窗 |
| `first_poll` | `baseline`（真正的全新仓库只记录游标，不补推 90 天历史）/ `backlog`（补推最近 1 小时） |
| `fallback_lookback_seconds` | **webhook 断链后轮询接管时，最多向前补多久的漏掉事件**（默认 6h） |

#### 什么时候会从 webhook 回退到轮询

判断是**逐仓库**做的，每轮（默认 180s）问同一个问题：*这个仓库最近 `webhook_freshness_seconds`（默认 900s）内收到过 webhook 吗？*

| 情况 | 会轮询吗 |
| --- | --- |
| 从没配 webhook（`webhook_last_seen_at` 为 NULL） | ✅ 一开始就是纯轮询 |
| webhook 配了但隧道断了超过 15 分钟 | ✅ 下一轮自动接管 |
| webhook 恢复正常 | ❌ 15 分钟内又开始跳过（自动交回给 webhook） |
| `poll_mode: always` | ✅ 每轮都跑（并行双保险，不是回退） |
| `poll_mode: never` | ❌ 永不轮询 |

“webhook 还活着”是**按“收到”判断、不是按“事件有用”判断**：哪怕推来的全是没订阅的仓库，也算通道健康，不会白白轮询。

**切换延迟**：webhook 在 T 时刻断，T+900s 判定过期，再等下一个周期 → **最坏约 18 分钟**（平均 ~16 分钟）。

**断链期间的事件不会丢**：一直靠 webhook 的仓库，`poll_state` 里根本没有游标，如果直接走 `first_poll: baseline`
就会把断链期间的动静全部丢掉（回退机制恰好丢掉了它最该救的那批事件）。所以轮询接管时会改用
**“最后一次确认 webhook 活着”的时刻**作为起点补推，并用 `fallback_lookback_seconds` 封顶避免长时间断链一次刷屏。
补推到的如果其实已经由 webhook 投递过，会被去重识别为重复（不会重推）；真正漏掉的才会补上。
| `poll_repos` | 额外轮询的仓库。**通配订阅（`org/*`）只对「已知仓库」生效**，已知 = 收到过 webhook，或写在这里 |

### delivery：重试与保留策略

| 字段 | 说明 |
| --- | --- |
| `retry_limit` | 每轮最多重试多少条失败投递 |
| `retry_max_age_minutes` | 首次失败超过这么久就放弃重试 |
| `ttl_days` | 投递记录保留天数（去重依赖它） |

### 5.1 群内指令：让群主自助增删订阅（可选）

默认关闭。开启后，群主（或 `commands.admins` 白名单）可以在群里直接改本群的订阅，不用每次改 YAML：

```
@larkbot help              显示帮助
@larkbot list              查看本群都有哪些订阅（区分 config 规则 / 指令订阅）
@larkbot sub acme/api      订阅（支持 org/* 通配，可一次写多个）
@larkbot unsub acme/api    退订
@larkbot whoami            查看自己的 open_id 与群 chat_id
```

中文别名也支持：`@larkbot 订阅 acme/api`、`@larkbot 列表`、`@larkbot 退订 ...`。

**四条必须知道的语义**
1. **指令永远动不了 config.yaml**。它只能增删「指令自己创建的订阅」，两者是叠加关系。
   退订一个被 config 规则覆盖的仓库时，回复里会明确告诉你“config 里还有规则在推，要彻底停请改 YAML”——
   不然你会以为退订失灵了。
2. **指令订阅按 `defaults` 的事件范围推送**。想按分支/action/actor 精细过滤，还是得写进 config。
3. **权限默认只给群主**（比较消息 sender 与群 `owner_id`，需要 `im:chat:readonly`）。也可以把 open_id 填进
   `commands.admins` 授予跨群管理权；用 `@我 whoami` 查自己的 open_id。
4. **可订阅的仓库范围默认限定在 config 已声明过的仓库内**（`commands.repo_allowlist` 可以放宽到 `other/*`，
   也可以收紧）。否则任何群主都能让机器人去拉一个不相干的仓库。

每次变更都会在群里回执（谁改的、改了什么），相当于自带的审计记录。

**开启步骤**

1. 开发者后台 → 应用能力：已开「机器人」；权限管理加 `im:message.group_at_msg:readonly`（接收群里 @机器人 的消息）
2. 事件与回调 → **事件订阅**：订阅 `im.message.receive_v1`；请求地址填 `https://<你的域名>/webhooks/feishu`
   - 飞书会先发一个 `challenge` 校验请求，larkbot 会自动应答（要求 1 秒内返回）
   - **加密策略的 Encrypt Key 请留空**：本版本未实现 AES 解密，配了加密会收到明确的 400 报错并写日志
   - 把 Verification Token 填到 `.env` 的 `FEISHU_VERIFICATION_TOKEN` —— **必填**：不填 larkbot 会直接
     503 拒绝处理回调事件，因为飞书的 token 是唯一的来源校验手段，没它任何人都能伪造“群内指令”
     （`open_id` 都能随便编）。只有本地调试才设 `feishu.allow_unverified_callbacks: true`。
   - challenge 地址校验的规则：**未配 token 时始终应答**（方便先把地址存下来）；
     一旦配了 token 就会校验，所以如果控制台提示“校验失败”，说明 `.env` 里的 token 与控制台不一致。
     推荐顺序：从控制台复制 token → 写进 `.env` → `systemctl restart larkbot` → 再回控制台保存请求地址。
3. 创建版本并发布（权限/事件订阅改动都要发版才生效）
4. config 里打开：

```yaml
commands:
  enabled: true
  admins: []              # 需要跨群管理权时填 open_id
  allow_group_owner: true
  repo_allowlist: []      # 留空 = 只能用 subscriptions 里声明过的仓库
```

5. 注意：**只有 config `chats:` 里登记过的群能用指令**（因为发消息/回执必须知道这个群）。
   未登记的群 @机器人 时会回复它的 `chat_id` 并告诉你怎么加。

命令行查看/维护（不经过飞书，方便运维）：

```bash
uv run larkbot subs                      # 表格列出所有指令创建的订阅
uv run larkbot subs --json
uv run larkbot subs --add dev:acme/api   # 手动补一条（相当于在群里执行 sub）
uv run larkbot subs --remove dev:acme/api
```

已知限制：只处理**文本**消息（图片/文件忽略）；不支持卡片按钮交互（`card.action.trigger` 已接收但只记日志）；
不支持加密封回调。

### 5.2 指令前面要不要加 `/`？（飞书交互机制的真实情况）

**结论：不用。** 飞书官方 FAQ 写得很直白——「机器人是否支持 `/help` 这类斜线命令？**机器人本身不支持**」，
并建议开发者自己订阅接收消息事件、在服务端解析文本。所以飞书机器人的指令就是**普通文本消息**，
`@机器人 sub acme/api` 就是地道用法。

容易弄混的是飞书有**三套完全不同的交互机制**：

| 机制 | 用户怎么用 | 群聊可用 | 需要 `/` | 说明 |
| --- | --- | --- | --- | --- |
| 文本消息 | `@机器人 sub acme/api` | ✅ | ❌ | 官方推荐路径；larkbot 用的就是这个 |
| 机器人自定义菜单 | 点输入框上方的菜单按钮 | ❌ **仅支持单聊** | ❌ | 事件 `application.bot.menu_v6`，群聊用不上 |
| Slash Command（新） | 输入框打 `/` 弹出指令面板 | 待验证 | ✅ | 需调 OpenAPI 注册；客户端 7.70+ |

补充两点：

- **larkbot 已经兼容 `/` 前缀**：`/sub acme/api`、`/订阅 acme/api`、`/help` 都能识别（解析时会去掉前导 `/`）。
  原因就是上面第三行那个新机制——如果以后用上指令面板，用户选完发过来的仍是文本，不需要改代码。
- `commands.require_mention: true`（默认）要求消息里必须 @ 了机器人。按本文档只开
  `im:message.group_at_msg:readonly` 时收到的消息本来就带 @，这项是白送的保险；
  万一你为了别的原因申请了「接收群内所有消息」的敏感权限，它能避免群里随便一句 `sub ...` 被当成指令。
  若发现指令面板发来的消息不带 @，把它改成 `false`。

### 5.3 PR 与评论事件：默认到底推哪些

这是最容易踩坑的地方：**`pull_request` 不是只推 opened，默认是所有 action 都推**；而「评审」是另外两类独立事件，**默认不推**。
以下是实测结果（用 `config.example.yaml` 的默认值跑出来的）：

| 你在 GitHub 上做的事 | 事件 | 默认推吗 |
| --- | --- | --- |
| 开 PR / 重开 PR | `pull_request` opened / reopened | ✅ |
| 往 PR 分支推新提交 | `pull_request` synchronize | ✅ |
| PR 被合并或被关闭 | `pull_request` closed（合并时 `merged=true`） | ✅ |
| 转 Ready for review / 转回草稿 | ready_for_review / converted_to_draft | ✅（草稿本身被 `ignore_drafts` 拦掉） |
| 加标签 / 删标签 / 指派 / 请求评审 / 改标题 | labeled / unlabeled / assigned / review_requested / edited | ✅（**通常最吵的就是这些**） |
| 在 PR **会话区**发评论 | `issue_comment` | ✅（与普通 Issue 评论是同一个事件，**当前无法区分**） |
| 提交评审（Approve / Request changes） | `pull_request_review` | ❌ 不在默认 `events` 里 |
| 在**代码行内**评论 | `pull_request_review_comment` | ❌ 同上 |

按需收紧（复制即用）：

```yaml
defaults:
  events: [push, pull_request, issues, issue_comment, release, pull_request_review]
  ignore_drafts: true      # 草稿 PR 不推（默认就是 true）
  # actions 有两种写法，一定要分清：
  #
  # A) 映射（推荐）：只对列到的事件类型过滤，其它类型不受影响
  actions:
    pull_request: [opened, reopened, synchronize, ready_for_review, closed, merged]
    # pull_request_review: [submitted, approved, changes_requested]
  #
  # B) 列表：作用于**所有**事件类型。写窄了会静默丢掉其它事件！
  #    例：actions: [opened, closed] 会连带丢掉 issue_comment(created) 与 release(published)
```

几点说明：

- `merged` 是「closed 且已合并」的别名；只想看合并就写 `[merged]`，只想看“被关没合”就写 `[closed]`
- `actions` 的候选值不只 action 本身，还包括 `merged`、Actions 的 `conclusion`（success/failure/timed_out）、
  status 的 `state`、review 的 `state`——所以同一个字段也能用来过滤 CI 结果
- 映射的 key 支持通配：`pull_request*` 能同时盖住 `pull_request_review`、`pull_request_review_comment`
- `larkbot check` 会在你用了列表写法时提醒你（因为它容易误伤）
- **PR 评论 vs Issue 评论现在分不开**（都是 `issue_comment`）。需要的话告诉我，加个过滤条件很快

### 5.4 三种常见分发策略

「哪个群收哪些消息」完全由 `chats` + `subscriptions` 两张表决定，下面是实践中最常用的三种写法。

**策略 1：按团队分群** —— 每个团队一个群，仓库→群。

```yaml
chats:
  - {name: team-frontend, transport: webhook, webhook_url: env:FEISHU_WEBHOOK_FE}
  - {name: team-backend,  transport: webhook, webhook_url: env:FEISHU_WEBHOOK_BE}
subscriptions:
  - {repos: ["acme/web-*", "acme/design-system"], chats: [team-frontend]}
  - {repos: ["acme/api", "acme/worker"],       chats: [team-backend]}
```

**策略 2：按事件严重度分群** —— 日常刷在团队群，发布/失败单独进“需要关注”的群。

```yaml
subscriptions:
  # 团队群：啥都收
  - {repos: ["acme/*"], chats: [team-backend], events: [push, pull_request, issues]}
  # 发布群：只收 Release（且保留机器人发的，不限 actor）
  - {repos: ["acme/*"], chats: [release-room], events: [release], ignore_actors: []}
  # 值班群：只收 Actions 失败（其它事件不关心）
  - repos: ["acme/*"]
    chats: [oncall]
    events: [workflow_run]
    actions: [failure, timed_out]
    ignore_actors: []
```

**策略 3：一仓库一群，但只关心分支** —— 适合单仓库多环境（`main` 进生产群，`release/*` 进发布群）。

```yaml
subscriptions:
  - {repos: ["acme/api"], chats: [prod-room], events: [push, pull_request], branches: [main]}
  - {repos: ["acme/api"], chats: [qa-room],   events: [push], branches: ["release/*", "hotfix/*"]}
```

验证办法：`larkbot simulate` 会直接告诉你这条事件命中了哪些群（stdout 是纯 JSON，可直接接 `jq`），
不命中时 stderr 会提示去哪查。以策略 2 为例（已实际跑通）：

| 模拟事件 | 命中群 |
| --- | --- |
| `--event workflow --conclusion failure` | `oncall` |
| `--event workflow --conclusion success` | （无，符合预期） |
| `--event release --actor 'github-actions[bot]'` | `release-room`（因为该条写了 `ignore_actors: []`） |
| `--event push --branch main` | `team-backend`, `prod-room` |
| `--event push --branch release/1.0` | `team-backend`, `qa-room` |
| `--event push --actor 'dependabot[bot]'` | （无，被默认的 `*[bot]` 过滤） |
| `--event pr --draft` | （无，被 `ignore_drafts` 过滤） |

可用参数：`--event`（`push`/`pr`/`issue`/`comment`/`release`/`workflow`/`create`/`delete`/`fork`/`star`）、
`--repo`、`--actor`、`--branch`、`--action`、`--conclusion`、`--merged`、`--draft`、`--print-card`、`--send`。

改完 YAML 不用重启，`curl -X POST localhost:8000/admin/reload -H 'X-Admin-Token: <token>'` 即可生效。

---

## 6. 运行与调试

| 命令 | 用途 |
| --- | --- |
| `larkbot serve` | 启动 HTTP 服务（webhook + 后台轮询） |
| `larkbot poll --loop` | 只跑轮询；`--repo owner/name` 可只轮询单个仓库 |
| `larkbot check` | 校验配置并给出常见坑提示（有问题时退出码 1）；`--verify` 额外联网验证 app 凭证与 `chat_id` |
| `larkbot chats` | 列出应用机器人所在的群，用于获取 `chat_id`；`--yaml` 直接生成配置片段 |
| `larkbot subs` | 查看/维护群内指令创建的订阅（`--add dev:acme/api` / `--remove` / `--json`） |
| `larkbot slash` | 管理客户端的 `/` 指令面板：`--register` / `--force` / `--dry-run` / `--delete <id>` |
| `larkbot send-test <chat>` | 往指定群发测试卡片 |
| `larkbot simulate --event pr --merged --print-card` | 本地构造事件，打印卡片 JSON 但不发送 |
| `larkbot simulate --event push --send` | 真发一条（会走真实路由与去重） |

HTTP 接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活检查（含调度器是否在跑） |
| GET | `/status` | 计数、各仓库轮询状态、最近失败（不含任何密钥，出错信息中的 webhook URL 已脱敏） |
| POST | `/webhooks/github` | GitHub webhook 入口 |
| POST | `/webhooks/feishu` | 飞书事件回调（challenge 校验 + 群内指令） |
| POST | `/admin/reload` | 热重载 YAML 配置（需要 `admin_token`） |
| POST | `/admin/poll` | 手动触发一轮轮询（需要 `admin_token`） |
| POST | `/admin/test/{chat}` | 向指定群发测试卡片（需要 `admin_token`） |

管理接口的凭证：请求头 `X-Admin-Token: <token>` 或 `Authorization: Bearer <token>`；
`server.admin_token` 留空则管理接口整体关闭。

排查「为什么没推」：`simulate` 会打印路由命中的每条订阅与原因（`event` / `branch` / `actor` / `draft` / `label`），
`--print-card` 可以直接看渲染结果。

---

## 7. 安全与部署注意

### 7.1 先分清方向：需要公网入口的只有「入站」那两条

| 能力 | 方向 | 需要公网入口吗 |
| --- | --- | --- |
| 收 GitHub 事件（webhook） | GitHub → 你 | ✅ 需要（就是这一条逼你暴露端口） |
| 收飞书群指令 | 飞书 → 你 | ✅ 需要（不用群内指令就不用暴露） |
| 收 GitHub 事件（轮询回退） | 你 → GitHub | ❌ 纯出站 |
| 发飞书消息（自定义机器人 / 自建应用都是） | 你 → 飞书 | ❌ 纯出站 |

所以有三种部署形态：

1. **零暴露**：不要 webhook，只靠轮询 + 发送。延迟 = `poll_interval_seconds`（默认 180s）
2. **只开 GitHub 入口**（推荐）：只暴露 `/webhooks/github`，事件秒级到达；轮询当备胎
3. **全开**：再暴露 `/webhooks/feishu`，才能用群内指令

### 7.2 用 cloudflared tunnel 把 GitHub 打进来

命名隧道（URL 固定，生产用）：

```bash
cloudflared tunnel login
cloudflared tunnel create larkbot
cloudflared tunnel route dns larkbot larkbot.example.com
```

`~/.cloudflared/config.yml`（系统级装在 `/etc/cloudflared/config.yml`）：

```yaml
tunnel: <上一条输出的 UUID>
credentials-file: /root/.cloudflared/<UUID>.json
ingress:
  # 只放行 webhook 路径，其余一律 404：/status 与 /admin/* 继续留在内网
  - hostname: larkbot.example.com
    path: ^/webhooks/
    service: http://localhost:8000
  # 需要从外面看健康检查就放开这两行
  # - hostname: larkbot.example.com
  #   path: ^/healthz$
  #   service: http://localhost:8000
  - service: http_status:404   # 必须是最后一条 catch-all
```

```bash
cloudflared tunnel ingress validate                              # 校验配置
cloudflared tunnel ingress rule https://larkbot.example.com/status  # 看某条 URL 命中哪条规则
cloudflared tunnel run larkbot
```

临时调试可以用快速隧道（**URL 每次重启都变，别用于生产**）：`cloudflared tunnel --url http://localhost:8000`

### 7.3 告诉 GitHub 打到哪个端点

仓库（或组织）→ Settings → Webhooks → Add webhook：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://larkbot.example.com/webhooks/github` —— **路径必须一致** |
| Content type | `application/json` |
| Secret | 与 `.env` 的 `GITHUB_WEBHOOK_SECRET` 完全一致（**先填 secret 再测试**） |
| SSL verification | Enable |

这个路径是代码里固定的（见 `src/larkbot/app.py` 的路由）。保存后 GitHub 会立即发一个 `ping`：

| 你看到 | 含义 |
| --- | --- |
| `200` + `{"msg":"pong"}` | 通了 |
| `401` | Secret 与 `GITHUB_WEBHOOK_SECRET` 不一致 |
| `503` | 服务端没配 `GITHUB_WEBHOOK_SECRET`（默认拒绝未签名请求）——先把 secret 填好再测 |
| `404` | 隧道 ingress 没放行这个 path，或 `larkbot serve` 没起来 |
| 连接超时 | 隧道没跑，或 hostname 没解析到隧道 |

排查入口就是该 webhook 页面的 **Recent Deliveries**：能看请求头/请求体/响应码/响应体，修好后可以 **Redeliver** 重放，不用傻等下一个事件。

飞书群内指令同理，事件订阅的请求地址填 `https://larkbot.example.com/webhooks/feishu`。
注意：飞书要求 **1 秒内**应答地址校验请求、**3 秒内**响应事件，所以别在隧道前面套需要登录的 Cloudflare Access 策略。

### 7.4 其余安全项

- `.env` 与 `config/config.yaml` 已在 `.gitignore` 里，别提交真实密钥
- `/status` 与 `/` 默认公开，只暴露群名、仓库名与计数，不含 webhook 地址和 token（出错信息里的 webhook URL 已脱敏）。
  如果隧道放行了整个站点，建议用 `server.admin_token` 锁管理接口，或在 ingress 里按 path 只放行 `/webhooks/`
- 状态库是单文件 SQLite（默认 `data/larkbot.db`），投递记录默认保留 `delivery.ttl_days`（14 天）后清理
- **隧道断了不等于丢事件**：`poll_mode: auto` 下，超过 `webhook_freshness_seconds`（默认 900s）没收到 webhook，
  轮询会自动接管，并把断链期间漏掉的事件补推（封顶 `fallback_lookback_seconds`，默认 6h），
  最坏退化成 ~18 分钟延迟，而不是静默丢消息

---

## 8. 开发

```bash
uv sync                       # 安装依赖（含 dev 组）
uv run pytest -q              # 测试（217 个）
uv run ruff check .           # lint
uv run ruff format .          # 格式化
uv run pyright                # 类型检查（当前 0 error）
```

类型检查用 pyright 的 `standard` 模式，配置在 `pyproject.toml` 的 `[tool.pyright]`：

- `venvPath` / `venv` 指向 `.venv`——不加这两项，第三方包会全部误报 `reportMissingImports`
- `pythonVersion = "3.11"` 钉在 `requires-python` 的下限，比当前解释器更早暴露「用了新版本才有的 API」
- `pyright` 包依赖 Node（没有时由 `nodeenv` 自动装一个）

目录结构：

```
src/larkbot/
  config.py        # YAML 配置模型、env: 引用展开、校验
  models.py        # 统一事件模型 RepoEvent + dedup_key
  router.py        # 按群订阅仓库的匹配与过滤（含指令订阅的叠加）
  commands.py      # 群内指令：解析、权限、范围校验、回执卡片
  service.py       # 编排：去重 -> 路由 -> 渲染 -> 投递
  poller.py        # 轮询回退（游标 / ETag / 跳过策略）
  store.py         # SQLite：投递去重、仓库登记、轮询状态
  runtime.py       # 运行时装配（CLI 与 HTTP 共用）+ app 通道联网自检
  console.py       # 终端表格（中文宽度对齐）与可粘贴的 YAML 片段
  app.py           # FastAPI 应用与路由
  fixtures.py      # 本地演练用的合成事件
  github/          # 归一化、签名校验、REST 客户端
  feishu/          # 卡片渲染、双通道发送
```

---

## 9. 已知边界 / 下一步

已经够用的部分：事件归一化（20+ 类型）、双通道去重、群级订阅、卡片渲染、热重载配置、演练 CLI。

后续可以按需加：

- [ ] 飞书群内交互：卡片按钮「订阅/退订」（`card.action.trigger` 已接收，需要补按钮与回调处理）
- [ ] 区分“PR 会话区评论”与“普通 Issue 评论”（现在都是 `issue_comment`）
- [ ] **Encrypt Key（AES-256-CBC）回调解密**：现在配了加密会报错，需关掉才能用
- [ ] 指令支持精细范围：`sub acme/api --events push,release --branch main`
- [ ] **机器人被拉进群时自动登记**（订阅 `im.chat.member.bot.added_v1` 事件 + `url_verification` 握手），省得手抄 chat_id
- [ ] `@` 提醒：给群配置需要 @ 的人（`<at user_id="ou_xxx">`）
- [ ] 按 actor 定向 @、按 label/priority 分级配色
- [ ] 群发限流与消息合并（同一仓库短时间内多次 push 合并成一条）
- [ ] 迁移到飞书卡片 2.0（`schema: 2.0`）以获得更好的排版能力
- [ ] 组织级通配订阅的自动发现（用 Org API 列出仓库，而不是只认「已知仓库」）
- [ ] 持久化重试队列 + 指数退避（现在是有界重试，超过窗口仍会丢）
