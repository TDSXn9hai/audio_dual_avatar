# 数字人平台接入与接口使用说明

本文面向负责平台前端、平台后端和部署的同事，说明如何将本仓库的数字人能力接入外部平台。

适用范围：当前 `main` 分支，第一入口 `/avatar`、第二入口 `/omni`。

---

## 1. 先理解几个概念

### 1.1 平台业务 Session 与模型 Session 不是一回事

- **平台业务 Session**：平台自己的会话、用户、租户、工单或对话记录 ID。
- **模型 Session**：数字人服务在 WebSocket `/v1/realtime` 上维护的一次实时推理生命周期。

当前项目**没有**“创建一个模型 Session”的 REST 接口，例如 `POST /sessions`。

模型 Session 的建立过程是：

1. 客户端连接 `WSS /v1/realtime?mode=audio`；
2. 等待 `session.queue_done`；
3. 发送 `session.init`；
4. 收到包含 `session_id` 的 `session.created`。

`GET /api/sessions` 只是查询当前活跃任务，不是创建接口。

### 1.2 数字人画面与模型语音是两路数据

完整数字人链路包含：

```text
平台浏览器
  ├── WSS /v1/realtime?mode=audio
  │     └── Gateway -> Worker -> MiniCPM backend
  │
  └── WSS /avatar-live/ws/live
        └── Gateway -> MuseTalk 数字人口型服务

Gateway 的音频旁路：
  MiniCPM 输出音频 -> Gateway -> MuseTalk
```

平台如果只连接 `/v1/realtime`，可以得到模型文本和语音，但不会自动获得数字人口型视频。

平台如果使用项目自带的 `/avatar` 页面，则页面已经处理了模型 Session、数字人 viewer、音画同步和自动打断。

---

## 2. 推荐接入方式

### 2.1 方式一：iframe 嵌入 `/avatar`（推荐第一阶段）

平台页面嵌入：

```html
<iframe
  src="https://dh.example.com:8006/avatar"
  title="实时数字人"
  allow="microphone; autoplay; fullscreen"
  style="width:100%;height:800px;border:0">
</iframe>
```

如果需要第二入口 `/omni` 的摄像头能力：

```html
allow="microphone; camera; autoplay; fullscreen"
```

优点：

- 平台无需实现 WebSocket 实时协议。
- 麦克风采集、Session 排队、数字人画面、口型、音频同步、自动打断均由页面处理。
- 适合快速完成平台试点和功能验证。

当前限制：

- 没有 `postMessage` 控制协议，平台暂时不能从外部设置系统提示词、用户 ID 或业务 Session ID。
- 页面使用自身默认提示词，也允许用户在页面内修改。
- 平台登录态不会自动传给数字人服务，需要反向代理或后续增加 Token 机制。

### 2.2 方式二：平台自研前端直接使用 WebSocket 协议

适合需要完全控制 UI、消息记录、角色设置和平台业务状态的场景。

平台前端需要负责：

1. 如需非默认形象，先调用 `/avatar-live/api/avatar/select` 选择本地已处理缓存；
2. 连接 `/avatar-live/ws/live` 获取数字人画面并发送 `ready`；
3. 采集并编码麦克风音频；
4. 连接 `/v1/realtime`，完成 Session 初始化；
5. 接收并处理文本和音频增量；
6. 拉取片段 WAV、播放音频、渲染帧、发送 ACK；
7. 处理自动打断、`flush`、超时和重连；
8. 关闭 Session 和释放麦克风。

建议直接复用：

- `minicpm_demo/static/duplex/lib/realtime-session.js`
- `minicpm_demo/static/avatar/avatar-player.js`
- `minicpm_demo/static/avatar/speech-interruptor.js`

### 2.3 方式三：平台后端代理 WebSocket

平台后端可以作为 WebSocket 代理，统一做鉴权、审计和多实例路由：

```text
平台浏览器 <-> 平台 WebSocket BFF <-> 数字人 Gateway
```

注意：

- 麦克风音频仍然来自平台用户浏览器，平台后端通常只做转发，不应假设后端能直接访问用户麦克风。
- 代理必须支持长连接、WebSocket Upgrade、二进制帧和至少 10 分钟读超时。
- 如果平台后端需要录制或分析模型输出，应分别处理模型音频和数字人 viewer 数据。

---

## 3. 服务地址与端口

假设数字人服务器可被平台用户访问的地址是：

```text
https://dh.example.com:8006
```

对应 WebSocket：

```text
wss://dh.example.com:8006/v1/realtime
wss://dh.example.com:8006/avatar-live/ws/live
```

| 端口 | 服务 | 是否对平台暴露 |
|---|---|---|
| `8006` | Gateway、页面、公开 API、WSS | 是 |
| `8007` | Gateway 内部 Worker 注册 | 否 |
| `22400` | Python Worker | 否 |
| `22500` | 模型 Backend | 否 |
| `8000` | MuseTalk 原始服务 | 否，仅 Gateway 内部访问 |

生产环境建议在 `8006` 前增加 Nginx、API Gateway 或统一接入层，对外只暴露 `443`。

---

## 4. 页面入口

| 接口 | 用途 |
|---|---|
| `GET /avatar` | 第一入口，纯音频数字人，带自动打断 |
| `GET /omni` | 第二入口，完整 Omni 页面，支持视频输入和高级设置 |
| `GET /` | 总入口页，列出已启用应用 |
| `GET /realtime` | 通用 Realtime 协议演示页 |
| `GET /audio_duplex` | 纯语音双工演示页 |
| `GET /turnbased` | 轮次对话演示页 |
| `GET /s/{session_id}` | 会话回看页面 |

平台正式接入时通常只需要 `/avatar`；其他页面主要用于调试和管理。

---

## 5. 核心 Session 接口

### 5.1 连接地址

```text
WSS /v1/realtime?mode=audio
WSS /v1/realtime?mode=video
WSS /v1/realtime?mode=chat
```

| mode | 输入 | 输出 | 最大会话时长 |
|---|---|---|---|
| `audio` | 麦克风音频 | listen、文本、24kHz 音频 | 600 秒 |
| `video` | 麦克风音频、可选 JPEG 视频帧 | listen、文本、24kHz 音频 | 300 秒 |
| `chat` | 一次提交 messages | 文本、可选音频 | 单轮请求 |

数字人第一入口使用 `mode=audio`。

### 5.2 推荐连接参数

```text
wss://host/v1/realtime
  ?mode=audio
  &client_id=<平台用户或设备ID>
  &page_session_id=<平台页面会话ID>
  &page_route=<平台来源页面>
  &client_surface=<web|desktop|mobile>
```

服务端会把这些参数写入 Session 记录，用于运营排查。它们是可选的匿名标识，不是认证凭据。

如果平台有正式业务 Session ID，建议映射到 `client_id` 或 `page_session_id`。

### 5.3 基本时序

```text
Client -> Gateway
  Client: WebSocket 连接
  Server: session.queued              （有排队时）
  Server: session.queue_update        （排队位置更新）
  Server: session.queue_done
  Client: session.init
  Server: session.created
  Client: input.append
  Server: response.output.delta
  Client: session.close
  Server: session.closed
```

客户端**不要在 `session.queue_done` 之前发送 `session.init`**。

### 5.4 初始化

```json
{
  "type": "session.init",
  "payload": {
    "system_prompt": "你是一名专业、亲切的中文知识传播助手……",
    "config": {
      "length_penalty": 1.1
    },
    "use_tts": true,
    "voice": {
      "ref_audio_base64": "<可选，base64 float32 PCM>",
      "tts_ref_audio_base64": "<可选，base64 float32 PCM>"
    }
  }
}
```

成功响应：

```json
{
  "type": "session.created",
  "session_id": "sess_xxx",
  "mode": "full_duplex",
  "metrics": {
    "backend": "pytorch",
    "kv_cache_length": 70
  }
}
```

`session_id` 由服务端生成，客户端不能自行指定。

### 5.5 提交音频

```json
{
  "type": "input.append",
  "input": {
    "audio": "<base64 float32 PCM>",
    "force_listen": false
  }
}
```

音频约束：

- 采样率：16000 Hz
- 声道：单声道
- 样本类型：little-endian float32
- 编码：base64
- 不使用 WAV、MP3 或其他容器封装

当前数字人页面每 1 秒提交一批音频。

视频模式可附带：

```json
{
  "video_frames": ["<base64 JPEG>", "<base64 JPEG>"],
  "max_slice_nums": 1
}
```

`force_listen=true` 表示要求模型停止输出并回到倾听状态。平台自动打断时应发送该字段。

### 5.6 接收输出

模型继续听：

```json
{
  "type": "response.output.delta",
  "kind": "listen"
}
```

文本增量：

```json
{
  "type": "response.output.delta",
  "kind": "text",
  "text": "这是一段增量文本",
  "response_id": "resp_xxx",
  "session_id": "sess_xxx"
}
```

音频增量：

```json
{
  "type": "response.output.delta",
  "kind": "audio",
  "audio": "<base64 float32 PCM>",
  "response_id": "resp_xxx",
  "session_id": "sess_xxx"
}
```

输出音频约束：

- 采样率：24000 Hz
- 声道：单声道
- 样本类型：little-endian float32
- 编码：base64

文本和音频是独立增量，不保证一一对应。

### 5.7 关闭

```json
{
  "type": "session.close",
  "reason": "user_stop"
}
```

服务端会尽力返回：

```json
{
  "type": "session.closed",
  "reason": "user_stop"
}
```

达到时长上限时，服务端会发送 `reason=timeout`。

### 5.8 错误与排队

常见服务端事件：

```json
{"type":"session.queued","position":2,"estimated_wait_s":60,"ticket_id":"q_xxx"}
{"type":"session.queue_update","position":1,"estimated_wait_s":30}
{"type":"session.queue_done"}
{"type":"error","error":{"code":"queue_full","message":"Queue full","type":"server_error"}}
```

平台应处理：

- `queue_full`
- `no worker available`
- WebSocket 非正常关闭
- 600 秒音频 Session 超时
- 服务端 `session.closed`

---

## 6. 数字人 viewer 接口

### 6.1 建立 viewer

如果平台使用自带 `/avatar` 页面，本节由页面处理。

如果平台自研 UI，需要在模型开始输出前连接：

```text
WSS /avatar-live/ws/live?video_codec=h264
WSS /avatar-live/ws/live?video_codec=webp
```

`video_codec` 可选值为 `h264`、`webp`、`jpeg`。不传时使用服务端
`AVATAR_STREAM_FORMAT`，当前默认是 `h264`。客户端不支持 WebCodecs 时应显式使用
`video_codec=webp`。

连接成功后立即发送：

```json
{"type":"ready"}
```

服务端会先返回：

```json
{
  "type": "meta",
  "fps": 24,
  "prebuffer": 32,
  "max_prefetch_seconds": 4,
  "frame_header_bytes": 24,
  "video_codec": "h264",
  "codec_string": "avc1.4D4028",
  "width": 480,
  "height": 864
}
```

### 6.2 控制消息

服务端可能发送：

```json
{
  "type": "segment",
  "id": 3,
  "duration": 1.0,
  "total_frames": 24,
  "start_frame": 20,
  "start_pts_us": 833333,
  "audio_url": "/api/live/audio/3"
}
```

```json
{"type":"segment_end","segment_id":3}
```

```json
{"type":"flush","frame_index":42}
```

```json
{"type":"error","message":"数字人推理或编码线程异常"}
```

```json
{"type":"live_end","reason":"idle_timeout"}
```

`flush` 表示模型被打断或回答被清空。客户端应立即停止旧音频、丢弃旧帧和旧片段，并从 `frame_index` 之后重新播放。

### 6.3 二进制帧格式

默认 H.264 格式下，每个 WebSocket binary message 携带一个 H.264 Annex-B
Access Unit：

```text
[24 字节 MTH1 头][H.264 Annex-B Access Unit]
```

头部采用 big-endian：

| 偏移 | 类型 | 字段 |
|---:|---|---|
| 0 | `4s` | magic，固定 `MTH1` |
| 4 | `uint8` | kind；0=idle，1=generated，2=silent |
| 5 | `uint8` | codec；1=H.264 |
| 6 | `uint8` | flags；bit 0=1 表示 IDR 关键帧 |
| 7 | `1 byte` | 保留 |
| 8 | `uint32` | segment_id；`0xffffffff` 表示 idle |
| 12 | `int64` | pts_us |
| 20 | `uint32` | frame_index |

H.264 必须由浏览器 `WebCodecs VideoDecoder` 解码：

```javascript
decoder.configure({
  codec: meta.codec_string,
  codedWidth: meta.width,
  codedHeight: meta.height,
  optimizeForLatency: true,
  avc: { format: "annexb" },
});
```

收到帧后构造 `EncodedVideoChunk`，`timestamp` 使用帧头中的 `pts_us`。解码输出
`VideoFrame` 后再按音频主时钟绘制到 Canvas，并在使用后调用 `VideoFrame.close()`。

`flush` 后必须 reset 解码器，丢弃所有非关键帧，直到收到新的 IDR。服务端在
`flush` 后会重启 H.264 编码器并立即产生新的 IDR，因此不依赖长 GOP 等待。
正常运行时默认每 120 帧插入一个周期性 IDR。

#### WebP/JPEG 回退格式

请求 `video_codec=webp` 或 `video_codec=jpeg` 时，每个 WebSocket binary message
仍使用原有 `MTF1` 格式：

```text
[24 字节 MTF1 头][编码后的单帧图片]
```

| 偏移 | 类型 | 字段 |
|---:|---|---|
| 0 | `4s` | magic，固定 `MTF1` |
| 4 | `uint8` | kind |
| 5-7 | `3 bytes` | 保留 |
| 8 | `uint32` | segment_id |
| 12 | `int64` | pts_us |
| 20 | `uint32` | frame_index |

图片负载可通过文件头判断：

- WebP：前 4 字节 `RIFF`，第 8-11 字节 `WEBP`
- JPEG：前 2 字节 `FF D8`

H.264 编码参数可通过以下环境变量调整：

| 环境变量 | 默认值 |
|---|---|
| `AVATAR_STREAM_FORMAT` | `h264` |
| `AVATAR_H264_CODEC_STRING` | `avc1.4D4028` |
| `AVATAR_H264_BITRATE` | `2M` |
| `AVATAR_H264_MAXRATE` | `3M` |
| `AVATAR_H264_BUFSIZE` | `2M` |
| `AVATAR_H264_GOP` | `120` |
| `AVATAR_H264_PRESET` | `ultrafast` |
| `AVATAR_H264_PROFILE` | `main` |
| `AVATAR_H264_LEVEL` | `4.0` |

### 6.4 片段音频

收到 `segment` 后，请求：

```text
GET /avatar-live/api/live/audio/{segment_id}
```

返回 WAV。平台应以音频为主时钟，在音频就绪后同步播放对应帧，不要先播放画面。

### 6.5 ACK

客户端需要定期回传播放进度：

```json
{
  "type": "ack",
  "frame_index": 120,
  "pts_us": 5000000,
  "kind": "speech"
}
```

建议每 6 帧发送一次 ACK，并在一个片段最后一帧额外发送一次。

`kind` 可为：

- `speech`
- `silent`
- `idle`

不发送 ACK 会导致服务端预取达到上限并停止继续发送。

### 6.6 自动打断与恢复

项目参考实现：

1. 检测用户持续说话；
2. 本地立即停止旧音频和旧画面；
3. 在下一次 `input.append` 中发送 `force_listen=true`；
4. Gateway 将 `force_listen` 上升沿转换为 MuseTalk `flush`；
5. viewer 收到 `flush` 后清理旧片段；
6. 如果 2500ms 内没有收到 `flush`，播放器重连 viewer 并恢复。

平台自研前端需要实现等价逻辑，否则可能出现“旧回答继续播放”或“打断后永久黑屏”。

---

## 7. HTTP 状态与管理接口

### 7.1 健康检查

| 方法 | 接口 | 用途 |
|---|---|---|
| `GET` | `/health` | Gateway 健康检查 |
| `GET` | `/status` | Worker 数量、忙碌数量、队列状态 |
| `GET` | `/workers` | Worker 明细 |
| `GET` | `/avatar-live/api/status` | MuseTalk 数字人服务状态 |
| `GET` | `/avatar-live/api/avatars` | 本地已预处理 avatar 缓存列表 |
| `GET` | `/avatar-live/api/avatar` | 当前激活的 avatar |
| `POST` | `/avatar-live/api/avatar/select` | 选择 avatar；传空对象恢复 config 默认值 |

平台启动前至少要检查：

```text
GET /health -> 200
GET /status -> total_workers > 0
GET /avatar-live/api/status -> ready=true
```

### 7.2 队列

| 方法 | 接口 | 用途 |
|---|---|---|
| `GET` | `/api/queue` | 当前队列快照 |
| `GET` | `/api/queue/{ticket_id}` | 单张排队凭证 |
| `DELETE` | `/api/queue/{ticket_id}` | 取消排队，管理接口 |
| `GET` | `/api/config/eta` | ETA 配置和估算状态 |
| `PUT` | `/api/config/eta` | 修改 ETA，管理接口 |

### 7.3 会话查询与回看

| 方法 | 接口 | 用途 |
|---|---|---|
| `GET` | `/api/sessions` | 当前活跃任务 |
| `GET` | `/api/sessions/{session_id}` | 会话元数据 |
| `GET` | `/api/sessions/{session_id}/recording` | 完整事件流 |
| `GET` | `/api/sessions/{session_id}/download` | 下载会话 ZIP |
| `GET` | `/api/sessions/{session_id}/assets/{path}` | 下载录音、视频等资源 |
| `POST` | `/api/sessions/{session_id}/upload-recording` | 上传前端录制文件 |
| `POST` | `/api/sessions/{session_id}/comment` | 写评语 |
| `GET` | `/api/sessions/{session_id}/comment` | 读评语 |

### 7.4 预设与参考音频

| 方法 | 接口 | 用途 |
|---|---|---|
| `GET` | `/api/apps` | 启用中的应用和入口 |
| `GET` | `/api/frontend_defaults` | 前端默认配置 |
| `GET` | `/api/presets` | 预设列表 |
| `GET` | `/api/presets/{mode}/{preset_id}/audio` | 预设音频 |
| `GET` | `/api/default_ref_audio` | 默认参考音频 |
| `GET` | `/api/avatar_ref_audio` | `/avatar` 专属参考音频 |
| `GET` | `/api/assets/ref_audio` | 参考音频列表 |
| `POST` | `/api/assets/ref_audio` | 上传参考音频 |
| `DELETE` | `/api/assets/ref_audio/{id}` | 删除参考音频 |

如果平台不开放音色切换，可以不暴露这组接口。

`/avatar` 使用独立的声线参考：

```json
{
  "audio": {
    "ref_audio_path": "assets/ref_audio/ref_minicpm_signature.wav",
    "avatar_ref_audio_path": "assets/ref_audio/ref_avatar_default.wav"
  }
}
```

- `ref_audio_path` 继续作为 `/omni`、preset 和其他 TTS 入口的默认音色；
- `avatar_ref_audio_path` 只由 `/avatar` 页面通过 `/api/avatar_ref_audio` 加载；
- `/avatar` 初始化会话时会把该音频同时放入 `ref_audio_base64` 和
  `tts_ref_audio_base64`；
- 该配置不会修改 `/omni` 或参考音频 preset。

### 7.5 Avatar 缓存选择

服务端只会扫描已经预处理完成的本地缓存，不会根据参考视频动态生成形象。
缓存目录为：

```text
musetalk_project/MuseTalk/results/v15/avatars/<avatar_id>/
```

默认 avatar 来自 `configs/streaming_duplex_knowledge.yaml` 的 `avatar.id`。
服务启动时加载该默认缓存。

#### 查询可用缓存

```http
GET /avatar-live/api/avatars
```

响应示例：

```json
{
  "default_avatar_id": "knowledge_host_720p",
  "active_avatar_id": "knowledge_host_720p",
  "avatar_revision": 1,
  "avatar_ready": true,
  "avatar_switching": false,
  "avatars": [
    {
      "id": "knowledge_host_720p",
      "valid": true,
      "width": 768,
      "height": 1344,
      "frame_count": 690,
      "source_frame_count": 345,
      "error": null
    },
    {
      "id": "musetalk_test_avatar",
      "valid": true,
      "width": 480,
      "height": 864,
      "frame_count": 690,
      "source_frame_count": 345,
      "error": null
    }
  ]
}
```

`valid=false` 的缓存也会返回，但 `error` 会说明缺文件、帧数不一致或解码失败等
原因。这类缓存不能激活。

#### 选择指定缓存

```http
POST /avatar-live/api/avatar/select
Content-Type: application/json
```

```json
{
  "avatar_id": "musetalk_test_avatar"
}
```

成功响应：

```json
{
  "success": true,
  "id": "musetalk_test_avatar",
  "valid": true,
  "width": 480,
  "height": 864,
  "frame_count": 690,
  "source_frame_count": 345,
  "error": null,
  "default_avatar_id": "knowledge_host_720p",
  "active_avatar_id": "musetalk_test_avatar",
  "avatar_revision": 2
}
```

#### 恢复 config 默认缓存

请求体为空对象时不传 `avatar_id`，服务会重新激活 config 中的默认缓存：

```http
POST /avatar-live/api/avatar/select
Content-Type: application/json

{}
```

也可以显式传：

```json
{
  "avatar_id": null
}
```

#### 调用顺序

切换 avatar 会导致画面分辨率变化，并重新加载整份缓存，因此必须按以下顺序：

```text
POST /avatar-live/api/avatar/select
  -> WSS /avatar-live/ws/live
  -> WSS /v1/realtime?mode=audio
```

不能在会话进行中切换。存在以下任一状态时，选择新缓存会返回
`409 avatar_busy`：

- 已有 viewer 连接；
- 已有 `/ws/audio` 或 `/ws/minicpm` 上游连接；
- 仍有帧正在推理生成。

如果请求的 avatar 与当前已激活 avatar 相同，接口幂等返回成功，不会重新加载。

切换期间，新 viewer 和音频上游会暂时收到 `avatar_switching` 或
`avatar_unavailable`。切换失败时会尝试恢复之前的 avatar，并将目标缓存标记为
不可用。

错误示例：

```json
{
  "detail": {
    "code": "avatar_not_found",
    "message": "Avatar cache not found: unknown"
  }
}
```

主要错误码：

| HTTP | code | 含义 |
|---:|---|---|
| 404 | `avatar_not_found` | 缓存目录不存在或 ID 非法 |
| 409 | `avatar_busy` | 当前有活跃 viewer、音频流或推理任务 |
| 409 | `avatar_switching` | 另一个切换请求正在执行 |
| 422 | `avatar_invalid` | 缓存结构不完整或内容不一致 |
| 500 | `avatar_activation_failed` | 加载失败 |
| 503 | `avatar_unavailable` | 模型运行时尚未初始化 |

平台只需访问 Gateway 的 `8006`。MuseTalk 的 `8000` 端口及其内部
`/api/avatar/select` 不应对平台暴露。

---

## 8. 不应暴露给平台的内部接口

以下接口只用于本项目内部服务通信：

- `PUT http://gateway:8007/internal/workers/{worker_id}`
- `WSS http://worker:22400/v1/worker/chat`
- `WSS http://worker:22400/v1/worker/duplex`
- `WSS http://backend:22500/backend`
- `POST http://backend:22500/sessions/{session_id}/close`
- `WSS http://musetalk:8000/ws/audio`
- `WSS http://musetalk:8000/ws/minicpm`
- `WSS http://musetalk:8000/ws/live`（平台应使用 gateway 的 `/avatar-live/ws/live`）
- `POST /api/_debug/record_trace`
- `/admin`、`/api/admin/*`

生产环境不要公开 `8000`、`8007`、`22400`、`22500`。

---

## 9. 鉴权、跨域和部署

### 9.1 当前鉴权状况

当前项目没有平台级登录、Token 验证、租户隔离或 RBAC。

必须在生产环境增加：

- 平台统一登录态校验；
- API Token / Cookie / HMAC 签名；
- WebSocket 连接鉴权；
- 用户和业务 Session 白名单；
- 速率限制；
- Origin 白名单。

### 9.2 HTTPS 与 iframe

- 平台和数字人服务都必须使用 HTTPS。
- 麦克风只在 Secure Context 下可用。
- 自签名证书会导致 Chrome 拒绝麦克风或阻止 iframe 加载，正式环境必须换成可信证书。
- iframe 必须声明 `allow="microphone; autoplay; fullscreen"`。

### 9.3 跨域

当前 Gateway 没有配置 CORS Middleware。

- iframe 页面内部访问同源资源不受普通 CORS 影响。
- 平台前端如果跨域 `fetch('/api/...')`，会被浏览器拦截。
- 如果确实需要跨域 REST，必须在 Gateway 或反向代理增加明确的 CORS 白名单。
- WebSocket 虽然不受普通 CORS 限制，但仍需要鉴权和 Origin 校验。

### 9.4 反向代理

代理必须支持：

- WebSocket Upgrade；
- 二进制 WebSocket 帧；
- 长连接至少 10 分钟；
- 关闭响应缓冲；
- 正确传递 `X-Forwarded-For`；
- TLS 终止。

不要对 `/v1/realtime` 或 `/avatar-live/ws/live` 做短超时、压缩或消息聚合。

---

## 10. 并发和容量限制

### 10.1 当前数字人单实例只支持一个并发 Session

MuseTalk 当前限制：

- 同一时间只支持一个 viewer；
- 同一时间只支持一个上游音频流；
- 第二个数字人 Session 会收到 `stream_busy`。

MiniCPM Worker 可以排队，但 Gateway 的数字人音频旁路仍指向同一个 MuseTalk 实例。

### 10.2 平台并发方案

短期方案：

- 平台维护数字人实例池；
- 每个并发用户分配一个“Gateway + MuseTalk + MiniCPM”实例；
- 平台负责排队、路由、回收和健康检查。

中长期方案：

- 改造 MuseTalk 为多 Session / 多 viewer 架构；
- 按租户或用户分片；
- 引入独立 WebRTC 媒体服务，用于弱网自适应和更低传输延迟。

如果只增加 MiniCPM Worker 数量，**不能解决数字人 viewer 单实例限制**。

---

## 11. 和 Gitee 版本的关系

当前版本与 Gitee 版本的接口路径、HTTP 方法、WebSocket 路由以及 `/v1/realtime` 消息协议一致。

需要平台同事注意的非协议差异：

- 当前 `/api/apps` 中新增第一入口 `avatar`。
- 当前 `/avatar` 使用独立数字人页面。
- 当前默认使用 H.264 Annex-B + WebCodecs，并保留 WebP/JPEG 回退。
- 当前保留本地 `prebuffer=32`、客户端 8 帧起播等参数。
- 当前增加麦克风诊断和打断恢复 watchdog。

如果平台只依赖 `/v1/realtime`，当前版本可以直接替换 Gitee 版本。

如果平台依赖 `/api/apps` 的固定列表，需要允许 `avatar` 这一新增项。

---

## 12. 联调验收清单

### 12.1 页面接入

- [ ] 平台电脑能访问 `https://<host>:8006/health`
- [ ] 平台页面能通过 HTTPS 嵌入 `/avatar`
- [ ] iframe 已允许 microphone、autoplay
- [ ] 点击开始后浏览器能获得麦克风权限
- [ ] `/avatar-live/api/status` 返回 `ready=true`
- [ ] 如需切换形象，先调用 `/avatar-live/api/avatar/select`
- [ ] 模型能收到非全零音频
- [ ] 数字人画面、语音和字幕正常
- [ ] 用户开口可以自动打断

### 12.2 自研前端接入

- [ ] 连接 `/v1/realtime?mode=audio`
- [ ] 收到 `session.queue_done`
- [ ] 正确发送 `session.init`
- [ ] 收到 `session.created` 和 `session_id`
- [ ] 正确编码 16kHz float32 PCM
- [ ] 正确处理 24kHz float32 PCM
- [ ] 正确处理 text/audio/listen 增量
- [ ] 根据 WebCodecs 能力选择 `video_codec=h264`，否则使用 `webp`
- [ ] 连接 `/avatar-live/ws/live` 并发送 `ready`
- [ ] 切换形象后重新连接 viewer，并读取新的 `meta.width/meta.height`
- [ ] 能解析 24 字节帧头
- [ ] 能识别 H.264 `MTH1`、WebP/JPEG `MTF1`
- [ ] H.264 模式下能处理关键帧标记和 `flush` 后等待 IDR
- [ ] 正确拉取 segment WAV
- [ ] 每 6 帧发送 ACK
- [ ] 能处理 `flush`
- [ ] 能处理 `session.closed` 和超时
- [ ] WebSocket 断开后能恢复

### 12.3 生产上线

- [ ] 只暴露 Gateway 端口
- [ ] 关闭公开 Admin
- [ ] 配置可信 HTTPS 证书
- [ ] 增加平台鉴权
- [ ] 配置 Origin 白名单
- [ ] 配置 WebSocket 长连接超时
- [ ] 完成单 Session 并发压测
- [ ] 明确并发实例扩容方案
- [ ] 配置日志、指标和告警
