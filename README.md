# Grok 生视频插件

面向 AstrBot 的 **Grok 专用** 视频生成插件，对接 `grok2api`。

> 不是通用多供应商插件；一期只服务 Grok 视频链路。

## 功能

- 文生视频 / 图生视频
- 命令参数与提示词内自动识别秒数、比例
- 默认：`6s` / `16:9` / `720p`
- 自定义 grok2api `base_url` / `api_key` / `model`
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
| `/视频 [秒数] [提示词]` | 指定秒数 |
| `/视频 [秒数] [比例] [提示词]` | 指定秒数和比例 |
| `/视频任务` | 查看进行中任务 |
| `/视频任务 <编号或任务ID>` | 查看详情 |
| `/视频取消 <编号或任务ID>` | 取消任务 |

### 硬规则

- **提示词必填**：文生、图生都必须有 prompt
- 秒数范围：`1~15`
- 比例：`1:1` `16:9` `9:16` `4:3` `3:4` `3:2` `2:3`
- 分辨率：`480p` `720p` `1080p`
- 超时最大：`1200` 秒（20 分钟）
- 参考图默认最多 `1` 张（可配到 8；Build/Console 实际常仅 1 张）

## 推荐模型

- `grok-imagine-video`
- `grok-imagine-video-1.5`

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

- 只适配 Grok / grok2api
- 不要把视频模型当 Chat/Responses 调用
- Build/Console 图生视频通常只稳吃 1 张首图
- 图生视频也必须带提示词
