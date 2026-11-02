# MiniCPM-o × MuseTalk 全双工数字人：改动与设计记录

记录日期：2026-10-29。本文档总结把 `minicpm_demo`（MiniCPM-o 4.5 全双工语音）作为上游、`musetalk_project/MuseTalk`（流式口型视频）作为下游，组成全双工数字人交互系统时，对运行环境、代码、调度机制、桥接层和前端的全部改动。

## 1. 总体架构

```
浏览器 https://<host>:8006/avatar（第一入口，纯音频数字人）
或 https://<host>:8006/omni（第二入口，完整 Omni 页面）
  │  麦克风 PCM (16k f32, 1s/块) [+ 摄像头 JPEG，可选]          ▲ 数字人 JPEG 帧 + 分段 WAV
  ▼                                                              │ (同源代理 /avatar-live/*)
gateway.py :8006 ──► worker :22400 ──► py_backend :22500 (MiniCPM-o, GPU0)
  │  下行 response.output.delta(audio/listen) 原样旁路复制        │
  ▼                                                              │
AvatarBridge ── ws://127.0.0.1:8000/ws/minicpm ──► MuseTalk stream_server :8000 (GPU0)
                                                   /ws/live、/api/live/audio/{id} ──┘
```

- MiniCPM-o 负责"听/说"决策和语音生成；MuseTalk 只根据 MiniCPM-o 的输出音频生成口型视频。
- 调度原则：**MuseTalk 的视频生成完全受 MiniCPM-o 的音频生成调度**。轮次由 `response_id` 区分，`listen` 表示轮次结束，打断由 MiniCPM-o 一侧的信号触发。
- `/avatar` 是独立的纯音频数字人页面，`/omni` 保留原有的视频输入、预设、Metrics、录制和文件模式。两个页面都复用数字人播放器；MiniCPM-o 自己的播放器静音，模型语音由数字人页面随口型同步播放。

## 2. 运行环境

### 2.1 共享 venv

- 在项目根目录新建 `.venv`，按 `minicpm_demo/.venv/base` 的 freeze 结果原样安装（`--no-deps`，版本完全一致：torch 2.8.0+cu128、numpy 2.2.6、transformers 4.51.0）。
- 额外安装 MuseTalk 运行时依赖：`torchvision==0.23.0`、`diffusers==0.30.2`、`opencv-python 4.14`、`omegaconf`、`ffmpeg-python`。
- 安装源：uv 不读 pip.conf，需要通过 `UV_INDEX_URL` 显式指定（`install.sh` 会自动传入 `PIP_INDEX_URL` 或 pip 配置里的地址）。
- `uv pip check` 的 2 条不兼容（`minicpmo-utils` 要求 librosa 0.9.0、`decord` 平台标记）在 MiniCPM 原环境里就存在。
- 新 venv 里**没有 mmpose/mmcv**（没有 torch 2.8 的预编译包），因此只能使用已生成缓存的 avatar；新建 avatar 仍需用旧环境 `musetalk_project/venv`。

### 2.2 为适配 torch 2.8 的 MuseTalk 兼容修改（旧环境下行为不变）

- `musetalk/utils/face_parsing/resnet.py`：`torch.load(..., weights_only=False)`。resnet18 权重是旧 `.tar` 格式，torch ≥2.6 默认 `weights_only=True` 会拒绝加载。
- `scripts/realtime_inference.py`：`preprocessing` 改为 try/except 导入。缺少 mmpose 时，`read_imgs` 用本地实现，`get_landmark_and_bbox` 直接报错。附带好处：运行时不再在 GPU 上加载 DWPose 和人脸检测模型。

### 2.3 一键启动脚本 `start_avatar.sh`

```bash
./start_avatar.sh          # 启动（默认 start）
./start_avatar.sh status
./start_avatar.sh stop
```

- 启动顺序：先启动 MuseTalk（GPU 0，端口 8000，配置 `configs/streaming_duplex.yaml`），等 `/api/status` 返回 ready；再用共享 venv 调用 `minicpm_demo/start_all.sh`（gateway 8006、backend 22500、worker 22400）。
- `start_avatar.sh` 会读取 `AVATAR_ENV_FILE`（默认 `./.avatar-env`），支持 `MINICPM_CUDA_VISIBLE_DEVICES` 和 `MUSETALK_CUDA_VISIBLE_DEVICES` 拆分双卡；未配置时两者都回落到 `CUDA_VISIBLE_DEVICES` 或 GPU 0。可参考 `.avatar-env.example`，不要提交机器专用 UUID。
- 为 gateway 设置两个环境变量：`AVATAR_BRIDGE_URL=ws://127.0.0.1:8000/ws/minicpm`（旁路），`AVATAR_HTTP_URL=http://127.0.0.1:8000`（页面代理）。
- 重复 start 会跳过已运行的服务；stop 先等 30 秒，进程未退出再发 `kill -9`。
- 日志：MuseTalk 在 `.run/logs/musetalk.log`，MiniCPM 在 `minicpm_demo/.run-logs/`。
- 单卡部署：两个服务都在 GPU 0，同时运行时显存约 35GB。

### 2.4 在新机器上部署：`install.sh`

```bash
tar -xzf digital_human_avatar_20261030.tar.gz && cd full_avatar_project
./install.sh               # 可选：--hf-mirror  --skip-env  --skip-weights  --no-verify
./start_avatar.sh          # 打开 https://<host>:8006/avatar
```

- 压缩包只含代码、启动/安装脚本、数字人 avatar 缓存（约 350MB，新环境无法重新生成）和文档；不含 venv、模型权重、证书、运行日志和测试数据。
- `install.sh` 依次完成：检查主机（GPU、磁盘约 35GB）；用 Python 3.10 建 `./.venv` 并按 `requirements-avatar.lock.txt` 原样安装（`--no-deps`）；配置 ffmpeg 链接、运行目录、自签名证书；从 Hugging Face 下载权重（MiniCPM-o 4.5 固定 revision `eb4141de…`，MuseTalk 运行时所需的 musetalkV15、sd-vae、whisper-tiny、face-parse-bisent）；按 `weights.sha256` 校验全部 35 个权重文件。
- 已存在的文件会跳过，下载可断点续传，失败后重跑即可。
- 包索引取 `PIP_INDEX_URL` 或 pip 配置，默认 PyPI；HF 地址取 `HF_ENDPOINT`，`--hf-mirror` 使用 hf-mirror.com；代理用标准的 `http(s)_proxy`。
- `AVATAR_WEIGHTS_FROM=/path/to/existing/full_avatar_project` 可从已有安装硬链接或复制权重，不走网络。
## 3. 调度机制

### 3.1 节奏：首窗 20 帧，之后每窗 24 帧

- MiniCPM-o 每个 `input.append` 最多产出一段约 1 秒的音频（24kHz float32），生产速度严格等于 1 倍实时。
- MuseTalk 以 24fps 渲染，每个窗口 = 核心帧 + 左上下文 5 帧（200ms）+ 右上下文 4 帧（167ms）。
- 首窗 20 帧：833ms 核心 + 167ms 右上下文 = 1000ms，正好是第 1 块，第 1 块一到即可切窗。
- 后续窗口 24 帧（16000 样本）：每个窗口需要的音频恰好结束在下一块的末尾，每收到一块立刻切出一个窗口，不需要等待。
- 如果固定用 20 帧窗口，833ms 与 1000ms 的周期不一致，每 6 个窗口里约有 3 个要等下一块，会产生周期性卡顿；固定 24 帧则首窗要等第二块。
- 窗口边界按帧序号计算样本位置（`round(i × 16000 / 24)`），不再每窗向上取整累积漂移；左右上下文样本数向上取整，避免分析时少算一帧。
- `batch_size: 28`：24 帧核心 + 4 帧重叠一次推理完成（用 24 需要跑两次）。

### 3.2 起播缓冲

- `prebuffer: 32`（约 1.33 秒）：每轮回答开头的起播偏移。依据：录制会话中 MiniCPM-o 块间隔在 0.71–1.25 秒之间波动；24 帧时两段录制会话共 3 次卡顿，32 帧时为 0。
- 新增 `live.prebuffer_answer_start_only: true`：只在一轮回答开头应用 prebuffer。MuseTalk 原逻辑在每个窗口前都按 prebuffer 补 hold 帧，会在回答中途制造周期性卡顿。

### 3.3 重采样

- 24k float32 → 16k PCM16 使用持续的 `soxr.ResampleStream`（HQ），每轮重建，块之间连续无咔哒声。
- soxr 流有可变输出延迟（实测最多约 585 样本），会让第 1 块输出不足 16000 样本、首窗被推迟一块。每轮开头补 640 个零样本（40ms）抵消，保证每个 1 秒块到达后输出至少 16000 样本。
- 不裁掉 MiniCPM-o 首块的前导静音：裁掉会使首窗等不到 16000 样本，反而推迟一块。

### 3.4 轮次状态机（MuseTalk `/ws/minicpm`）

| 事件 | 处理 |
|---|---|
| `kind:"audio"`，新的 `response_id` | 开始新的一轮：重置窗口计数（首窗 20 帧）、重建重采样器、补 40ms 零 |
| `kind:"audio"`，同一 `response_id` | 重采样后送入窗口切分 |
| `kind:"listen"`（且本轮已开始） | 轮次结束：冲刷重采样尾部，剩余音频切成最后一窗，标记 `is_final`（worker 停止补 hold 帧） |
| 听的阶段每秒一次的 `listen` | 忽略（只认"说话 → 听"的切换） |
| `bridge.flush`（客户端 force_listen） | 打断：清空全部内容，不恢复 |
| 没收到 `listen` 就出现新 `response_id` | 视为打断，清空 |
| 已被清空的 `response_id` 迟到的音频块 | 丢弃 |
| `session.closed` / `bridge.reset` / 断开 | 结束当前轮 |

- 上一轮已正常 `listen` 结束、尾音还在播放时出现新回答：**排在后面播放，不清空**。最初设计是清空，但回放录制会话时发现 MiniCPM-o 可能 1.4 秒后就开始下一轮，而 MuseTalk 管线约有 1 秒以上延迟，会把正常回答的结尾切掉。

### 3.5 打断与清空

- 协议层面，MiniCPM-o 被用户打断和正常结束都表现为"说话 → 听"，无法区分；因此"说话 → 听"一律按正常结束处理，剩余画面放完（约 1 秒以上）。
- `/avatar` 在数字人说话时监测麦克风 RMS，持续约 180ms 后自动触发 `force_listen`，同时本地立即停播并丢弃旧画面。
- 自动打断会启动 2500ms 的 flush watchdog。正常收到 `flush` 时取消；超时则清除丢弃状态、重置播放器时间轴并重连 viewer，避免页面永久卡在"正在听你说"。
- 真正清空仍由 `force_listen` 和"无 listen 换回答"触发。
- MuseTalk 原有的打断（viewer 发起，需要 `segment_id`/`frame_index`，10 秒后会**重放**旧回答）不适合全双工，因此新增服务端主动清空：
  - `SegmentManager.flush_all()`：取消所有排队中和生成中的片段，清掉 viewer 的打断/恢复状态，下一帧从当前播放位置之后重新开始。
  - `/ws/live` 向 viewer 推送 `{"type":"flush","frame_index":F}`，viewer 立即停掉已排好的音频并丢弃之后的帧。
- 实测：清空消息 10ms 内到达 viewer。
## 4. 桥接层

### 4.1 Gateway 旁路（`minicpm_demo/gateway_modules/avatar_bridge.py`）

- 设置了 `AVATAR_BRIDGE_URL` 才启用；只对双工会话（`request_type != "chat"`）生效。
- 接入点在 `gateway.py` 的 `_api_worker_passthrough_ws`：
  - `worker_to_client()`：把 `response.output.delta` 中 `kind` 为 `audio`/`listen` 的消息和 `session.closed` **原样**复制给旁路。
  - `client_to_worker()`：检测上行 `input.append` 的 `force_listen` 从关变开的那一刻，转成一条 `{"type":"bridge.flush"}`。
  - 会话结束时发送 `bridge.reset` 并关闭旁路。
- 每个会话一条有界队列（256）和独立发送任务，与客户端转发完全解耦。MuseTalk 连不上、报 `stream_busy` 或出错时，只停掉这条旁路，MiniCPM-o 的客户端转发不受影响。
- 格式转换不在 gateway 做，gateway 事件循环里没有 numpy 计算。

### 4.2 MuseTalk 入口（`muse_stream/audio/minicpm.py`，`MiniCPMBridgeSession`）

- 继承 `StreamAudioSession`，复用其窗口切分、上下文和反压逻辑；不发送 `audio_ack`（`send_acks = False`）。
- 路由 `/ws/minicpm`（`api/audio_websocket.py`）。与 `/ws/audio` 共用上行锁，同一时间只有一个上游。
- 解码 base64 float32 → soxr 重采样 → PCM16 → `_handle_pcm()`；轮次逻辑见 3.4。

### 4.3 MuseTalk 服务端的其他修改

- `audio/stream.py`：新增 `first_window_frames`；窗口边界按帧序号计算；上下文样本向上取整；准入检查抽成 `_admit()`；`send_acks` 类属性。
- `config.py`：解析并校验 `audio_stream.first_window_frames`（默认等于 `window_frames`）、`live.prebuffer_answer_start_only`（默认 false）。原有配置行为不变。
- `inference/workers.py`：`prebuffer_answer_start_only` 开启时，回答进行中的窗口不再补 prebuffer。
- `state.py`：`PipelineState.flush_seq`、`flush_frame`。
- `audio/segments.py`：`flush_all()`。
- `api/websocket.py`：
  - 在 `_send_controls()` 中检测 `flush_seq` 变化，丢弃已排队帧并推送 `flush` 消息。
  - **修复原有缺陷**：viewer 在空闲时断开，服务端主循环可能永远不再发送数据、检测不到断开，导致之后所有新 viewer 被"已有直播观众连接"拒绝（刷新页面就进不去）。现在 ACK 读取任务发现连接关闭时置位 `viewer_gone`，主循环据此退出。
- `web/app.js`（MuseTalk 自带调试页）、`headless_live_viewer.py`：处理 `flush` 消息。
- 新配置 `configs/streaming_duplex.yaml`（原 `streaming_web.yaml` 未改），相对原配置的区别：

| 配置项 | 值 | 原值 |
|---|---|---|
| `server.batch_size` | 28 | 20 |
| `server.prebuffer` | 32 | 8 |
| `live.idle_timeout_seconds` | 86400（长期运行，不因空闲退出） | 30 |
| `live.prebuffer_answer_start_only` | true | （新增） |
| `audio_stream.first_window_frames` | 20 | （新增） |
| `audio_stream.window_frames` | 24 | 20 |
| `audio_stream.context_right_ms` | 167（4 帧） | 300 |
## 5. 前端

### 5.1 页面与同源代理（`gateway.py`）

- `/avatar`：第一入口，返回独立页面 `static/avatar/avatar.html`，使用纯音频 `mode=audio` 和自动打断。`/omni` 是第二入口，保留原完整 Omni 页面。
- App Registry 中 `avatar` 排在 `omni` 前，首页卡片和全局导航按此顺序生成。
- 未设置 `AVATAR_HTTP_URL` 或数字人入口被禁用时，`/avatar` 回退到 `/omni`。
- gateway 走 HTTPS，MuseTalk 是 http/ws，浏览器会拦截混合内容，因此由 gateway 同源转发：
  - `GET /avatar-live/api/status`
  - `GET /avatar-live/api/live/audio/{id}`（每段 WAV）
  - `WS /avatar-live/ws/live`（帧和控制消息双向转发）

### 5.2 数字人播放器（`static/avatar/avatar-player.js`，`AvatarPlayer`）

- 从 MuseTalk `web/app.js` 移植 `/ws/live` 客户端：解析 24 字节帧头（`MTF1`、kind、segment id、pts_us、frame index）、按 `segment` 消息拉取并解码 WAV、ACK 回传播放进度、处理 `flush`。
- 默认接收 WebP 帧（同时兼容 JPEG）；MuseTalk worker 通过 `AVATAR_STREAM_FORMAT=webp` 编码。保持本地现有 480×864、`prebuffer: 32`、客户端 8 帧起播和 4 秒 prefetch 参数。
- 在 Start 点击时创建 AudioContext 并连接（满足浏览器自动播放限制，也保证模型开口前 viewer 已连上，MuseTalk 只在有 viewer 时渲染）。断开后每 3 秒自动重连。
- **音视频同步（音频为主时钟）**：
  - 每个窗口收到 8 帧（`CLIENT_PREBUFFER_FRAMES`）且音频解码完成后才排上播放。
  - 画面停在第一个未排上音频的窗口之前，不提前播放该窗口。
  - 音频只能晚于预定时间开始时，把画面时间轴整体后移同样时长（控制台打印 `video timeline shifted +XXms`）。
  - 音频下载失败时该段画面无声播放，不会卡住。
  - 这一机制修复了"第一句音频晚于视频"的问题：原逻辑使用服务端的 prebuffer（32 帧），首窗 20 帧必须全部到齐声音才开始，而画面按时间点先播放。
- 字幕：按 `prebuffer/fps + 300ms`（约 1.6 秒）推后显示，只显示最后约 48 个字。

### 5.3 Omni 页面：第二入口（`static/omni/*`、`static/duplex/lib/audio-player.js`）

- `AudioPlayer.setMuted()`：输出前加音量节点。数字人模式下静音，排队计时、指标和录制回调照常工作，避免两路声音。
- 布局（`applyAvatarLayout()` 移动现有元素，id 和事件绑定不变）：
  - 左栏：竖屏 9:16 数字人铺满高度（最大占左栏 64%）；摄像头预览（4:3）和输入模式叠放在右侧窄列。
  - 右栏：上方并排"预设/系统提示词 | 回复长度与视觉等配置 | Metrics"（1.3 : 1 : 0.8，最高 46%），下方对话记录。
  - 左右栏 11:9，页面最大宽度 1680px；宽度 ≤1024px 时改为纵向排列。
  - 全屏仍是摄像头画面，数字人隐藏（沿用原全屏逻辑）。
- **仅音频输入**（勾选框，默认开启，存 localStorage `avatar.audioOnly`）：
  - 不开摄像头、不发送 `video_frames`，连接改用 `mode=audio`（会话上限 600 秒，视频模式 300 秒）。
  - 目的是省掉每块的视觉编码（`benchmark.json` 中视频输入的 prefill 约 237ms），只影响模型一侧时延。
  - 会话进行中不能切换；仅音频模式下跳过会话录制（录制器依赖摄像头画面）。
- Start 时解锁数字人音频；Force Listen 会清空数字人当前回答并清字幕；扬声器选择同时作用于数字人音频。
## 6. 改动文件清单

新增：

- `start_avatar.sh`、`.venv/`（项目根目录）
- `.avatar-env.example`、`.gitignore`
- `minicpm_demo/gateway_modules/avatar_bridge.py`
- `minicpm_demo/static/avatar/avatar.html`、`avatar.css`、`avatar-app.js`、`avatar-poster.jpg`、`speech-interruptor.js`
- `minicpm_demo/static/avatar/avatar-player.js`
- `musetalk_project/MuseTalk/muse_stream/audio/minicpm.py`
- `musetalk_project/MuseTalk/configs/streaming_duplex.yaml`

修改：

- MiniCPM 侧：`gateway.py`、`static/omni/omni.html`、`static/omni/omni.css`、`static/omni/omni-app.js`、`static/duplex/lib/audio-player.js`
- MuseTalk 侧：`muse_stream/api/audio_websocket.py`、`muse_stream/api/websocket.py`、`muse_stream/audio/segments.py`、`muse_stream/audio/stream.py`、`muse_stream/config.py`、`muse_stream/inference/workers.py`、`muse_stream/state.py`、`web/app.js`、`musetalk/utils/face_parsing/resnet.py`、`scripts/realtime_inference.py`
- `musetalk_project/headless_live_viewer.py`

## 7. 验证记录

| 项目 | 结果 |
|---|---|
| 新 venv 下 MuseTalk 打断验收测试（headless） | `passed: true`（报告 `MuseTalk/output/stream_interrupt_test_newvenv.json`） |
| 新 venv 下 MiniCPM-o 双工探测 | 多次 `success: true` |
| 1 秒块按实时推流（`/ws/audio`） | 第 1 块后 0.01s 切出 20 帧窗口，之后每块立即切出 24 帧窗口 |
| 两段录制会话回放（`/ws/minicpm`，7 轮 40 窗口） | 窗口切分正确，卡顿 0 次，误清空 0 次 |
| 回答中途 `bridge.flush` | viewer 10ms 内收到清空，同回答后续 3 块被丢弃，下一轮正常 |
| 经 gateway 代理的 viewer（wss） | 38 秒 943 帧（约 24.8fps），9 段音频全部下载成功，清空送达 |
| 实时全链路（MiniCPM-o 探测 → 旁路 → MuseTalk → 代理 viewer） | 成功，音视频与音频两种模式都测过 |
| 新入口与旧入口 | `/avatar`、`/omni` 均返回 200；`/api/apps` 顺序为 `avatar → omni` |
| 双卡启动 | MiniCPM-o 在 GPU 0、MuseTalk 在 GPU 1 启动并通过健康检查 |
| 直连 WebP 流 | `/ws/audio → /ws/live` 前 12 帧全部为 WebP，0 帧 JPEG |
| gateway WSS 代理 | `/avatar-live/ws/live` 前 4 帧全部为 WebP |
| 自动打断超时合同 | 2500ms watchdog 已覆盖播放器丢弃态和页面等待态 |
| JS 语法 | esprima 检查通过（机器上无 node，也无浏览器） |

未在浏览器中实际验证：页面显示效果、布局、浏览器回声消除效果、`/avatar` 页面的实际音画同步（"第一句音频晚于视频"已由用户确认修复）。

## 8. 已知问题与后续方向

1. **画面滞后于音频（已分析，未修改）**：每个窗口只在开始时对齐一次，窗口内后续帧到得晚就会落后于声音。主要可能原因是带宽：480×864、JPEG q88 约 61KB/帧，24fps 约 12 Mbit/s，经 MuseTalk → gateway → 浏览器两段有序 WebSocket；音频是单独的小 WAV，不受影响。其他原因：两服务共卡时渲染偶尔慢于实时、客户端 8 帧即起播。建议方向：
   - 加偏差监测（每帧到达时间 − 应显示时间）；
   - 检测到迟到时，下一窗等全部帧到齐再起播，用时间轴后移在窗口边界纠正（偏差限制在 1 秒内，代价是卡顿时声音在窗口间短暂停顿）；
   - JPEG 质量降到 75（约 41KB/帧、8 Mbit/s）；进一步可做自适应码率、只传嘴部区域，或改用 WebRTC/H.264（约 1–2 Mbit/s）。
2. **管线延迟**：从 MiniCPM-o 第一块音频到出画面约 1.3–1.6 秒（起播缓冲 32 帧 + 渲染）。被打断时，模型认为"已说出"的内容比用户实际听到的多约 1 秒以上，目前无法从协议层修正。
3. **单观看端**：MuseTalk 同一时间只支持一个 viewer；同时打开 `http://<host>:8000/` 调试页时，`/avatar` 会显示错误并不断重连。也只支持一个上游会话，与 MiniCPM-o 单 backend 单会话 1:1 配对。
4. **回声消除未测**：数字人声音经 WebAudio 播放，与原 Omni 页面相同机制，但不戴耳机插话的效果未实测。
5. **窗口 WAV 堆积**：MuseTalk 每个窗口向 `results/live_audio/<run_id>/` 写两个 WAV，从不删除。
6. **测试残留**：测试期间 gateway 录制功能新增了约 19 个会话（`minicpm_demo/data/sessions/`），MuseTalk 窗口 WAV 约 74MB；`/tmp` 下有测试脚本（`chunk_stream_test.py`、`replay_minicpm_session.py`、`proxy_viewer.py`、`hold_viewer.py`）和 esprima（`/tmp/jscheck`）。均未清理。
7. **未知原因的残留卡顿**：在固定 prebuffer 24 帧、未开启 `prebuffer_answer_start_only` 时，20 帧切到 24 帧后的第一个衔接处偶发单帧 hold；改为"只在回答开头缓冲"并用 32 帧后，回放测试中未再出现。
