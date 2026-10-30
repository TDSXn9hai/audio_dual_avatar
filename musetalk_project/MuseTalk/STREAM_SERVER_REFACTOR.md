# MuseTalk 流式服务代码快照

- 生成日期：2026-10-23
- 快照目录：`output/code_snapshot/`
- 快照范围：本次模块化拆分和流式 PCM 音频输入涉及的修改/新增文件
- 默认输入：`/ws/audio`，16 kHz 单声道 PCM S16LE
- 兼容模式：`file`、`hybrid`
- 输出协议：保持现有 WebSocket `MTF1` 视频帧协议和窗口音频 URL

## 目录结构

```text
output/code_snapshot/
├── LIVE_STREAMING_GUIDE.md
├── README.md
├── STREAMING_QUICKSTART.md
├── STREAM_SERVER_REFACTOR.md
├── stream_server.py
├── simulate_pcm_interrupt_flow.py
├── configs/
│   └── streaming_web.yaml
├── web/
│   ├── README.md
│   ├── app.js
│   └── index.html
└── muse_stream/
    ├── __init__.py
    ├── config.py
    ├── domain.py
    ├── lifecycle.py
    ├── log.py
    ├── main.py
    ├── models.py
    ├── protocol.py
    ├── state.py
    ├── api/
    │   ├── __init__.py
    │   ├── app.py
    │   ├── audio_websocket.py
    │   ├── routes.py
    │   └── websocket.py
    ├── audio/
    │   ├── __init__.py
    │   ├── analysis.py
    │   ├── idle.py
    │   ├── ingest.py
    │   ├── io.py
    │   ├── segments.py
    │   └── stream.py
    └── inference/
        ├── __init__.py
        ├── pipeline.py
        └── workers.py
```

## 模块职责

| 文件 | 职责 |
|---|---|
| `stream_server.py` | 兼容入口，保留 `STATE`、`app`、`main()` 和原命令行用法 |
| `simulate_pcm_interrupt_flow.py` | 模拟上游回答 PCM、两次人工打断、自动恢复和最终空闲退出 |
| `STREAMING_QUICKSTART.md` | 当前 stream 模式的精简启动、URL、协议和故障排查教程 |
| `muse_stream/config.py` | YAML 参数解析、校验、路径解析和状态初始化 |
| `muse_stream/domain.py` | `LiveSegment`、`Runtime` 等共享数据对象 |
| `muse_stream/state.py` | 服务、模型、流水线、打断、观众、目录输入和流式输入状态 |
| `muse_stream/protocol.py` | 帧常量、24 字节帧头、PTS 和 `pack_frame` |
| `muse_stream/models.py` | UNet、VAE、Whisper、avatar 加载和 warmup |
| `muse_stream/lifecycle.py` | 初始化线程、ready/error 状态和输入模式选择 |
| `muse_stream/audio/io.py` | WAV 读取、ffmpeg 转码和裁剪 |
| `muse_stream/audio/analysis.py` | 帧级 RMS、VAD mask、短静音合并和整段/内存采样分析 |
| `muse_stream/audio/segments.py` | 文件/内存窗口注册、段队列、打断和恢复 |
| `muse_stream/audio/ingest.py` | `data/audio/` 文件扫描和文件稳定检测 |
| `muse_stream/audio/stream.py` | `/ws/audio` PCM 接收、窗口聚合、ACK 和背压 |
| `muse_stream/audio/idle.py` | 无目录扫描模式下的打断定时器和空闲退出 |
| `muse_stream/inference/pipeline.py` | 短窗口补零到 30 秒、Whisper 特征、UNet/VAE 和融合 |
| `muse_stream/inference/workers.py` | 推理线程、JPEG 编码线程和线程/异步队列桥接 |
| `muse_stream/api/app.py` | FastAPI 应用组装、静态目录和 HTTP 中间件 |
| `muse_stream/api/routes.py` | 首页、状态接口和音频窗口 URL |
| `muse_stream/api/audio_websocket.py` | `/ws/audio` 路由 |
| `muse_stream/api/websocket.py` | `/ws/live` 路由和单观众 `LiveStreamSession` |
| `muse_stream/main.py` | 配置加载、初始化线程和 uvicorn 生命周期 |

## 流式音频数据流

```text
16 kHz mono PCM16
  → /ws/audio
  → 20 ms 级别的二进制块
  → 按 window_frames 聚合成约 0.833 秒窗口
  → 写入窗口 WAV
  → 短音频补零到 Whisper 要求的 30 秒输入
  → Whisper encoder
  → UNet / VAE
  → WebSocket JPEG 帧 + 窗口音频 URL
```

默认窗口参数：

```text
window_frames  = 20
fps            = 24
window_samples = ceil(20 * 16000 / 24) = 13334
window_ms      = 833.375
```

## 流式输入协议

连接：

```text
ws://<server>:8000/ws/audio
```

开始：

```json
{"type":"audio_start","sample_rate":16000,"channels":1,"format":"s16le"}
```

音频：

```text
小端序 PCM16 二进制，每 20 ms 为 640 字节
```

结束：

```json
{"type":"audio_end"}
```

服务端返回 `audio_meta`、`audio_started`、`audio_ack`、`audio_done` 和
`error` 控制消息。单个服务实例同时只接受一路音频上行和一路直播观众。

## 验证结果

- `python -m compileall -q stream_server.py muse_stream`：通过
- `ruff check --select F`：通过
- 默认配置解析：`input_mode=stream`，16 kHz、单声道、S16LE
- 窗口计算：13334 个采样得到 20 个视频帧
- 窗口 WAV：成功写入并可通过 `/api/live/audio/{id}` 获取
- 错误采样率：返回 `unsupported_audio_format`
- 双 WebSocket：`/ws/audio` 实时输入，`/ws/live` 同时接收视频
- 端到端测试：6 秒 PCM 聚合为 8 个窗口，120 帧无协议错误
- 自动打断验收：4 秒 + 8 秒 + 8 秒截短音频完整通过；第一次打断自动恢复，
  第二次打断后新回答首窗口 0.033 秒内被接受，最终 15 秒测试空闲后退出的
- 完整回答 3 验收：63.6 秒音频发送 77 个窗口，首窗口 0.829 秒内被接受；
  1526 个媒体帧全部收到，缺失 0；媒体 ACK 到达后才进入空闲退出
- 短窗口推理：20 帧窗口补零到 30 秒后正常执行 Whisper/UNet/VAE
- 文件模式配置解析：仍支持 `input_mode=file`
- 测试结束后：8000 端口释放，GPU 无残留进程

## 性能基准

环境：A800 80 GB、24 FPS、batch=20、VAD 关闭。

| 阶段 | 实测 |
|---|---:|
| Whisper Mel 提取 | 约 2.9 ms |
| Whisper encoder，固定 30 秒输入 | 约 3.2 ms |
| 10 个 0.8 秒窗口的 Mel + encoder | 67.2 ms |
| UNet + VAE，20 帧 | 182.2 ms |
| 融合 + JPEG，20 帧 | 338.3 ms |
| 当前真实输出吞吐 | 约 50 FPS |

流式窗口每分钟约执行 72 次 Whisper 编码，额外耗时约 0.48 秒/分钟。
当前吞吐瓶颈仍是 CPU 融合和 JPEG 编码。

## SHA-256

```text
86324a355c14d2d8437df0094cd7f07054642344f8e2eb27d0e1589b7a496068  LIVE_STREAMING_GUIDE.md
20db2e1507676aa43e93cccc2b235aa30bde2ed4dca1c0f00dc887eec016071f  README.md
348ca58ff26aaae52adf5c1e9fea1f41b3d0cc0b59d733f7fbf1987d903d6164  simulate_pcm_interrupt_flow.py
f912428c8b1e037d886ee13a644af9c35d86f081551b213a4dd1e30321065811  STREAMING_QUICKSTART.md
89aefe92d43c7877a0fb0394c97a31cb0e3d4876fe3c0005c7fd56ba4021a30e  configs/streaming_web.yaml
e6c0b644cbfdc2b736312699580119fc4f7e164448b13cb60c4272eca41913ac  muse_stream/__init__.py
70c497a923e6a0cad9fe5e3bec6deea0a55fcc9c2b5f613372c75783dc9da1cd  muse_stream/api/__init__.py
44a42558dd1cada362f8675344398144acb684bf0af326788f0d137fa9fa7fb0  muse_stream/api/app.py
76775a8d3984f3746da24b1807a0517f59e3ec7c17a99d50d2e3771290a6a574  muse_stream/api/audio_websocket.py
e8e5aed0b760353ebe805e5aaea44daa8e9b347781e188275a71742ddf0c28cd  muse_stream/api/routes.py
a09c44f22a739e9496212abf71b3e9aef8d51c9314826ee5523217e55a13fb86  muse_stream/api/websocket.py
c22e5e28eb8195b9ecc401e69e1ed9dfc1905509c2a108c4ef36040ed75d48f9  muse_stream/audio/__init__.py
01dbf08d81ce05fff8f048ed18a87cf04372890e3086dc6c5025f7168de72b3f  muse_stream/audio/analysis.py
83edd121ed921a6715b289ded86964c9f14c830883fe395c8039a7f648152759  muse_stream/audio/idle.py
c63f64454ed127a2f3461238c232c5c1ef02adad37f4c61baa7cb20d24db8aa2  muse_stream/audio/ingest.py
a5b07b78639f5540515f4b5d74e8168bf403fbbeb215efc2cd426e97c802c97e  muse_stream/audio/io.py
9f6eedea4c93613b1d785f2f44f22bddd35ee53bcf470f8a4c59be6ac35d2e39  muse_stream/audio/segments.py
c477ce71374d030c693d7bbd3d2d777f1b18c040f9ac478798f3a59ca5ee95e1  muse_stream/audio/stream.py
8cb62f0bc19942ec84f5553bf6d2b0d2e0c0229b43075ed8d114ac4608eb02a1  muse_stream/config.py
e57a602a1cc732b2e30fb4feb6e1d437cf09e11e4845e74066f70b34072528a8  muse_stream/domain.py
eda8ec4e5e31adad8c848b9b7e0ea3ad69e033753e4f6c4659358065a6a76c53  muse_stream/inference/__init__.py
5a2ec5602c65513f596ff0e380ac7dfca3d764939984b010a78d2092a67d2718  muse_stream/inference/pipeline.py
23b19779f96aa053a8170f5b966e806b9b0fe90cfcfeb6d92d01372e1ff64f99  muse_stream/inference/workers.py
bc8dabffb99497690367209ca9c378ec8c5d7fefab142c21df7af246a6be2f92  muse_stream/lifecycle.py
8596a1cf075875335401dbc502be280f8298148a368368354dbdefadca933a06  muse_stream/log.py
2a459b59692e3b4a335c39a693ff42c221d5579776acfa9b5e8c1a013e4cc8c6  muse_stream/main.py
e62d0e46f503ba30c1a4e6a1f4dab8025f5887d31089891ae36f3ad79ecc3c15  muse_stream/models.py
5d44f18689232ef4d94de7f8b87fb95030ca3d5d4a6434101cf7e8c5aea79ed2  muse_stream/protocol.py
9b67dce64a3be91e0a249c4700a4bb1e5a4c537665180455fad33a6ae46b3d1e  muse_stream/state.py
49f45939fee7c8dfe5c7738db64ed5beb1ac17a517e6ff0192395830d613706f  stream_server.py
e0f05a54a8ceb0d6a2c5b348902c513d23f7bc15729686015d5fe212b496eb2a  web/README.md
312bc4f58b714b51dc45b852250f7791036b6272a1fb5f8732326e964f83adf0  web/app.js
cbe748d99629128a5f42e78769b69920b602dd621118d0265ab3c7a752944748  web/index.html
```
