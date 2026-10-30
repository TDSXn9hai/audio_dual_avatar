# MuseTalk 流式直播精简启动教程

本文只覆盖当前推荐的 `stream` 模式：上游通过 WebSocket 推送 PCM 音频，
服务端实时生成数字人视频，浏览器通过 WebSocket 接收画面和窗口音频。

旧版文件监听、历史验证记录等内容不再写在本教程中。

## 1. 启动前检查

```bash
cd <project-dir>

python --version          # 推荐 Python 3.10
ffmpeg -version
nvidia-smi
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

模型文件至少应存在：

```text
models/whisper/
models/sd-vae/
models/musetalkV15/unet.pth
models/musetalkV15/musetalk.json
```

首次部署如缺少 Python 依赖：

```bash
python -m pip install -r requirements.txt
```

## 2. 启动服务

前台启动：

```bash
./start_streaming.sh
```

后台启动：

```bash
mkdir -p output
nohup ./start_streaming.sh > output/stream_server.log 2>&1 &
```

不要对运行中的服务按 `Ctrl+Z`。停止服务使用 `Ctrl+C`，让它正常释放 8000
端口和 GPU。

检查状态：

```bash
curl http://127.0.0.1:8000/api/status
```

等待返回：

```json
{"status": "ready", "ready": true}
```

首次启动需要加载模型和 avatar 缓存，通常会明显慢于后续启动。

## 3. 浏览器访问 URL

浏览器打开的是 HTTP 页面，不是 WebSocket：

| 场景 | URL |
|---|---|
| 在虚拟机内部访问 | `http://127.0.0.1:8000/` |
| 通过虚拟机可路由 IP 访问 | `http://<server-ip>:8000/` |
| 通过宿主机端口映射访问 | `http://<host-ip>:<published-port>/` |
| 同一内网访问虚拟机私网 IP | `http://<vm-private-ip>:8000/` |
| HTTPS 反向代理 | `https://<domain>/` |

例如宿主机 `8000` 映射到虚拟机 `8000`：

```text
http://<host-ip>:8000/
```

文档中不要写入真实 IP。云主机需要确认安全组、防火墙和端口映射允许访问。

通过 HTTPS 反向代理时，WebSocket 也必须允许 Upgrade：

```text
wss://<domain>/ws/audio
wss://<domain>/ws/live
```

## 4. 打开直播页面

浏览器打开对应 HTTP URL 后：

1. 点击“进入直播”。
2. 确认页面状态进入直播或待机播放。
3. 保持页面打开，不要刷新。

当前服务同一时间支持：

- 1 路 `/ws/audio` 音频上行。
- 1 路 `/ws/live` 浏览器观众连接。

## 5. 上游 PCM 接入

默认音频上行地址：

```text
ws://<server-ip>:8000/ws/audio
ws://<host-ip>:<published-port>/ws/audio
wss://<domain>/ws/audio
```

固定格式：

| 参数 | 要求 |
|---|---|
| 采样率 | 16000 Hz |
| 声道 | 单声道 |
| 编码 | 有符号 16 位小端 PCM，S16LE |
| 推荐块大小 | 每 20～60 ms 一个二进制块 |

连接后先接收：

```json
{
  "type": "audio_meta",
  "sample_rate": 16000,
  "channels": 1,
  "format": "s16le",
  "window_frames": 20,
  "window_samples": 13334,
  "window_ms": 833.375,
  "context_left_ms": 200,
  "context_right_ms": 300,
  "max_pending_windows": 12
}
```

发送开始消息：

```json
{"type":"audio_start","sample_rate":16000,"channels":1,"format":"s16le"}
```

收到 `audio_started` 后，发送 PCM16 二进制数据。每处理完一个音频窗口，
服务端返回：

```json
{
  "type": "audio_ack",
  "samples": 26668,
  "windows": 2,
  "buffered_samples": 0,
  "queued_windows": 1
}
```

一段回答结束后发送：

```json
{"type":"audio_end"}
```

服务端提交剩余完整视频帧并返回 `audio_done`。每段独立回答建议使用新的
`/ws/audio` 连接。

回答被提前打断时发送 `{"type":"audio_cancel"}`：服务端丢弃尚未成窗的缓冲
PCM，不注册新语音段，并返回 `audio_cancelled`。

上游需要注意：

1. 先把 TTS 音频转换为 16 kHz 单声道 PCM16。
2. 服务端处理不过来时会背压，不要另开第二路并行上行。
3. 回答被打断后，停止发送该回答的剩余 PCM，并发送 `audio_cancel` 丢弃
   未成窗缓冲。
4. 新回答必须在打断等待时间内重新连接并发送首段 PCM。
5. 发送 `audio_end` 后等待 `audio_done`，再关闭连接。

完整实现可参考根目录的
[simulate_pcm_interrupt_flow.py](simulate_pcm_interrupt_flow.py)。

## 6. 打断流程测试

先启动服务，并在浏览器中点击“进入直播”，然后执行：

```bash
python simulate_pcm_interrupt_flow.py
```

> 注意：该脚本只模拟上游 PCM 客户端，不会启动模型服务，也不会创建端口转发。
> 如果输出一直显示“无法连接”，请先执行 `./start_streaming.sh`，并确认
> `http://127.0.0.1:8000/api/status` 可访问。

脚本默认参数：

```text
base-url: http://127.0.0.1:8000
answer-1: data/audio/sun.wav
answer-2: data/audio/eng.wav
answer-3: data/audio/musetalk_test_audio.wav
```

如果服务不在本机，或需要连接其他地址，再覆盖 `--base-url`：

```bash
python simulate_pcm_interrupt_flow.py --base-url http://<server-ip>:8000
```

默认测试流程：

1. `data/audio/sun.wav` 作为第一段回答。
2. `data/audio/eng.wav` 作为第二段回答。
3. 按终端提示，在前端点击第一次“打断”。
4. 保持上游静默，等待 10 秒自动恢复。
5. 恢复播放后，再按提示点击第二次“打断”。
6. 脚本发送 `data/audio/musetalk_test_audio.wav` 作为新回答。
7. 新回答播放完成后，等待服务端空闲退出。

整个流程只应主动打断两次：第一次等待自动恢复，第二次触发新回答。脚本会检测
额外打断并明确报错；如果多点了一次，请重启服务和脚本后按提示重新测试。

服务端会区分待机帧 ACK 和媒体帧 ACK。回答 3 只有在浏览器确认绘制到最后一个
媒体帧后才会被判定为播放完成，避免待机画面提前推进时间线。

打断期间到达的新回答首个窗口会绕过旧恢复队列的背压，并立即清除已取消的旧恢复
段，确保新回答能在 10 秒窗口内进入优先队列。

为避免等待整段 60 秒音频全部入队后才出现打断提示，脚本默认只把 `eng.wav`
的前 15 秒作为第二段可打断内容。需要测试完整 60 秒时可显式关闭截取：

```bash
python simulate_pcm_interrupt_flow.py \
  --answer2-seconds-before-interrupt 0
```

注意：

- `sun.wav` 是 44.1 kHz，脚本会自动重采样。
- `eng.wav` 当前约 60 秒。
- `musetalk_test_audio.wav` 当前约 63.6 秒。
- 完整测试通常需要 3 分钟以上。

回答 1 和回答 3 默认按真实时间每 20 ms 推送一个 PCM 块。回答 2 默认使用
`--answer2-send-delay-ms 5` 加速预送，避免流式播放已经结束时才出现打断提示；
脚本会打印回答 2 的实际剩余播放时间。不要使用 0 延迟连续灌流进行正式测试，
否则可能触发服务端队列背压和 WebSocket keepalive 超时。

测试报告：

```text
output/stream_interrupt_test.json
```

## 7. 关键配置

配置文件：

```text
configs/streaming_web.yaml
```

流式模式：

```yaml
live:
  input_mode: stream
  idle_timeout_seconds: 30
  interrupt_wait_seconds: 10

audio_stream:
  sample_rate: 16000
  channels: 1
  format: s16le
  window_frames: 20
  context_left_ms: 200
  context_right_ms: 300
  overlap_frames: 4
  pause_overlap_frames: 5
  pause_boundary_frames: 6
  max_pending_windows: 12
```

修改配置后需要重启服务。

### 特征窗口 A/B 对比

两套配置只改变逐帧 Whisper 特征窗口，其他参数保持一致：

```bash
# 原逻辑：局部窗口偏过去
bash start_streaming_legacy.sh

# LiveTalking 风格：10 个特征居中、略偏未来
bash start_streaming_centered.sh
```

对应配置：

- `configs/streaming_web_feature_legacy.yaml`
- `configs/streaming_web_feature_centered.yaml`

核心参数：

```yaml
inference:
  feature_window_mode: legacy   # legacy | centered
  feature_window_left: 4
  feature_window_right: 6
```

两个脚本默认都使用 8000 端口，一次只能启动一个。

段边界融合由以下参数控制：

```yaml
audio_stream:
  overlap_frames: 4
  pause_overlap_frames: 5
```

`overlap_frames` 会额外生成少量右上下文帧，与下一段同时间戳首帧融合；边界落在
静音时使用 `pause_overlap_frames`。两个值都不能超过右上下文帧数，设为 `0`
可关闭融合。

`pause_boundary_frames` 控制边界前后检查停顿的范围：`6` 表示前后各约
`250ms`，设为 `0` 可关闭停顿检测。

如需临时测试目录文件输入：

```yaml
live:
  input_mode: file
  process_existing: false
```

`hybrid` 表示同时启用 WebSocket PCM 和 `data/audio/` 文件输入。

## 8. 常见问题

### 页面一直显示加载模型

查看服务日志，确认模型和 avatar 缓存加载完成：

```bash
tail -f output/stream_server.log
```

### 浏览器无法连接

检查：

- 服务是否监听 `0.0.0.0:8000`。
- 虚拟机端口是否映射到宿主机。
- 安全组和防火墙是否开放端口。
- 反向代理是否允许 WebSocket Upgrade。

### `/ws/audio` 返回 not_ready

服务还在加载模型。等待 `/api/status` 返回 `ready: true` 后再连接。

### `/ws/audio` 返回 stream_busy

已经存在一路音频上行连接。关闭旧连接后再连接。

### 页面没有声音或画面

确认：

- 浏览器已经点击“进入直播”。
- PCM 为 16 kHz、单声道、S16LE。
- 上游已经收到 `audio_started`。
- 上游持续发送二进制 PCM，而不是文本或 base64。

### 直播自动结束

默认连续 30 秒没有新音频、没有待机播放活动和生成任务时自动退出。
测试时可通过 `live.idle_timeout_seconds` 调整。

## 9. 检查服务是否完全退出

```bash
curl http://127.0.0.1:8000/api/status
ss -ltnp '( sport = :8000 )'
nvidia-smi
```

端口无监听、GPU 无对应 Python 进程即表示已释放。
