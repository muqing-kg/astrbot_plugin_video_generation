# 通用视频生成插件

面向 AstrBot 的通用视频生成插件，对接 **OpenAI 风格视频接口网关**（`grok2api`、`new-api` 等），支持 Grok、字节 Seedance 等主流视频模型。

> 不是火山方舟（Ark）等原生 API 的直连客户端；Seedance 等模型请经 new-api 等网关使用。

## 功能

- 文生视频 / 图生视频
- 命令参数与提示词内自动识别秒数、比例
- 默认：`6s`；比例与分辨率默认**不指定**（请求不携带，由网关决定）
- 自定义视频网关 `base_url` / `api_key` / `model`
- 自动适配网关字段差异（`image` 对象/字符串、`aspect_ratio`/`ratio`、`duration`/`seconds`），依据上游 400 校验反馈改写请求并重试
- URL 支持：
  - `http://host:8000`
  - `http://host:8000/v1`
  - `http://host:8000/v1/videos/generations`
- 任务开始提示、任务查询/取消
- 超时/重试/并发/排队
- 黑白名单、速率限制、每日额度
- 可选任务历史持久化

## 命令

| 命令 | 说明 |
|---|---|
| `/视频 [提示词]` | 文生/图生视频（有图则图生） |
| `/视频 [秒数] [比例] [分辨率] [提示词]` | 三个参数开头**任意顺序**固定写，如 `/视频 720p 9:16 6s 城市夜景` |
| `/视频 <预设名...> [额外提示词]` | 使用预设提示词生成，可叠加多个预设 |
| `/视频任务` | 查看进行中任务 |
| `/视频任务 <编号或任务ID>` | 查看详情 |
| `/视频取消 <编号或任务ID>` | 取消任务 |
| `/视频预设` | 查看预设提示词列表 |
| `/视频预设 <名称>` | 查看预设详情 |
| `/视频预设 添加 <名称:提示词>` | 添加/覆盖预设（管理员） |
| `/视频预设 删除 <名称>` | 删除预设（管理员） |

## 参数决策链

每个参数（秒数/比例/分辨率）独立按以下优先级取值：

```text
提示词内提取（6s / 时长:6 / 16:9 / 横屏 / 720p / 分辨率1080p，提取后从提示词剥离）
  > 预设固定值（高级 JSON）
  > [仅比例] 图生：自动适配首图比例
  > 配置默认（默认"不指定"）
  > 全部为空 → 请求不携带该字段 → 模型使用自己的默认值
```

- 文生视频：什么都不写时请求只有 `model + prompt`，全部走模型默认。
- 图生视频：比例默认看首图；提示词写明了比例则强制覆盖。
- **值不适配自动回落**：若设定的值被上游拒绝（如 `duration must be one of [5, 10]`），插件会自动剔除该字段重试，回落模型默认，任务不失败（日志留痕）。
- **多参考图**：默认只发首图；`max_reference_images` 设为 N>1 时会附带 `image_urls` 数组尝试多图，网关不支持自动回落首图。模型能力以主人配置声明为准，插件不做探测。

## 预设提示词

- 简单格式：`名称:提示词`，如 `电影感:电影感画面，浅景深，自然光影`
- 高级格式：`名称:{"prompt":"提示词","aspect_ratio":"16:9","resolution":"720p","duration":6,"model":"..."}`
- `/视频` 从提示词**开头连续匹配**预设名（长名优先，每个最多一次），剩余文字作为额外提示词
- 预设只影响提示词；高级格式可固定比例/分辨率/秒数/模型（用户显式指定时优先）
- 内置示例：电影感、赛博朋克、治愈系、运镜大师，可在 WebUI 配置或用 `/视频预设 添加` 管理

### 硬规则

- **提示词必填**：文生、图生都必须有 prompt
- 秒数：`1~15`；默认**不指定**（请求不携带，模型用默认时长）
- 比例：任意 `W:H` 数值；默认**不指定**
- 分辨率：`720p` `1080p` `2k` `4k` 等按网关；默认**不指定**
- 超时最大：`1200` 秒（20 分钟）
- 参考图默认最多 `1` 张（可配到 8；多图是否生效取决于模型，不支持自动回落首图）

## 推荐模型

- Grok：`grok-imagine-video`、`grok-imagine-video-1.5`
- Seedance（经 new-api 等网关）：按网关模型名填写，例如 `seedance-2.5`

## 配置

1. **供应商配置**
   - API 地址：grok2api 地址
   - API Key：grok2api 客户端密钥
   - 模型：Grok 视频模型
2. **生成与结果设置**
3. **运行控制**（超时最大 1200）
4. **使用限制**
5. **任务持久化**

### 开始提示模板默认

```text
已开始视频生成任务{reference_images_block} [任务ID: {task_id}]
```

## 上游接口

```http
POST {base}/v1/videos/generations
Authorization: Bearer <api_key>
```

```json
{
  "model": "grok-imagine-video",
  "prompt": "一只猫在海边奔跑",
  "duration": 6,
  "aspect_ratio": "16:9",
  "resolution": "720p",
  "image": {"url": "https://..."}
}
```

然后轮询：

```http
GET {base}/v1/videos/{request_id}
GET {base}/v1/videos/{request_id}/content
```

创建路径自动按序尝试：`/v1/videos/generations`（grok2api）、`/v1/videos`（OpenAI 风格）、`/v1/video/generations`（new-api 风格）。轮询同时识别 `status` / `task_status` / `state` 字段。

不同网关对字段形态的要求不一致（例如 Seedance 网关要求 `image` 为字符串，grok2api 要求 `{"url": ...}` 对象）。插件遇到 400/422 字段校验报错时，会解析错误中点名的字段并自动改写请求重试：`image` 对象⇄字符串、`aspect_ratio`→`ratio`、`duration`→`seconds`、类型不匹配时自动转换，无需手动配置。

## QQ 视频发送说明

QQ（aiocqhttp/NapCat）下，插件按以下顺序尝试发送视频：

1. **base64 内联 Video**（零配置，默认路径）：将视频以 `base64://` 直传 NapCat，由 NapCat 解码到自身临时目录后上传，不依赖文件系统互通，也不依赖任何全局配置。仅当未配置 AstrBot 全局 `callback_api_base` 时启用；文件超过 50MB 时跳过（OneBot 报文膨胀约 1/3）。
2. **AstrBot 文件回调 URL**（需配置 AstrBot 全局 `callback_api_base`）：注册本地视频为可访问 HTTP URL，NapCat 可直接下载播放，跨机部署也适用。配置了 `callback_api_base` 时此项优先、base64 自动跳过（两者组合会触发 FileNotFoundError）。
3. **本地文件路径**（`file:///` 或绝对路径）：要求 AstrBot 与 NapCat 同机，文件系统互通。NapCat 实测对 Video 段直接拒绝这两种形式（retcode 1200）时，会继续降级。
4. **`File` 附件兜底**：仅当上述 Video 全部失败时才发送文件附件（显示为文件卡片，非播放气泡）。

因此若 QQ 收到的是文件而非可播放视频，默认无需额外配置：base64 内联会优先尝试。若 base64 因超过 50MB 被跳过，再考虑配置 AstrBot 全局 `callback_api_base`（形如 `http://<AstrBot主机>:<端口>`），并确保 NapCat 能访问该地址。插件日志会打印实际发送的组件类型（`type=Video` 或 `type=File`）与失败原因。

发送前会先剥离 Grok/xAI MP4 常见的顶层 C2PA `uuid` 等非必要 box（无需 ffmpeg）。可选：主机安装 `ffmpeg` 后，会再以 `-map_metadata -1` remux `+faststart` 或转码 H.264+AAC，进一步清掉 moov 元数据，提升 NT 可播放气泡成功率。

## 平台判定（QQ / 微信 / 其他）

QQ 与微信可能同时挂在同一个 aiocqhttp 适配器下，且 AstrBot 平台实例名可随时修改。插件按以下顺序判定发送链路：

1. **配置钉住机器人账号（推荐）**：在插件配置的 `platform` 段填写
   - `qq_self_ids`：QQ 机器人的 `self_id`（QQ 号）。命中则走 QQ 链路（清洗+可播放气泡），不依赖实例名，改实例名也不受影响。
   - `wechat_self_ids`：微信机器人的 `self_id`（wxid 或微信号）。命中则走微信/其他链路（原始 mp4）。
   - 判定优先级：`qq_self_ids` > `wechat_self_ids` > 启发式。
2. **启发式兜底（未配置时）**：
   - id 含 `wxid_` / `@chatroom` / `gh_` / `wechat` / `weixin` / `微信` → 微信链路
   - 适配器为 `aiocqhttp` / `onebot` 或 UMO 以 `qq:` 开头 → QQ 链路
   - 其他适配器 → 其他链路（原始 mp4）

日志中的 `platform=qq|wechat|other` 即为判定结果，便于核对。

## 安装

1. 放入 AstrBot 插件目录
2. 依赖：`aiohttp>=3.9.0`
3. 填写 grok2api 地址与客户端密钥
4. 重载后使用 `/视频 ...`

## 注意

- 视频接口按 OpenAI 风格网关设计；火山方舟（Ark）原生接口未适配，Seedance 等请经 new-api 等网关使用
- 不要把视频模型当 Chat/Responses 调用
- Build/Console 图生视频通常只稳吃 1 张首图
- 图生视频也必须带提示词
