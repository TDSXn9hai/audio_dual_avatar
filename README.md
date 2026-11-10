# audio_dual_avatar

[English](README.en.md) | 简体中文

本项目基于 MuseTalk 和 MiniCPM 构建。

这是一个全双工数字人项目：[MiniCPM-o 4.5](https://huggingface.co/openbmb/MiniCPM-o-4_5)
负责实时听取用户输入并生成语音，[MuseTalk](https://github.com/TMElyralab/MuseTalk)
根据 MiniCPM-o 的流式语音输出生成口型同步的数字人画面。

本仓库只包含代码。约 23 GB 的模型权重由 `install.sh` 自动下载。

## 环境要求

- Linux x86_64，NVIDIA GPU，CUDA 12.8 驱动，GPU 0 至少需要约 45 GB 可用显存
- Python 3.10（也可使用 `uv` 自动获取），以及 `curl`、`openssl`
- 至少约 35 GB 可用磁盘空间；能够访问 Python 包索引和 Hugging Face（或镜像）

## 安装与启动

```bash
./install.sh          # 创建 venv、安装固定版本依赖、准备 ffmpeg 和 TLS 证书、下载权重并校验 sha256
                      # 可选参数：--hf-mirror --skip-env --skip-weights --no-verify
./start_avatar.sh     # 启动后访问 https://<主机地址>:8006/avatar
./start_avatar.sh stop
```

### 默认 720p 知识分享形象

`./start_avatar.sh` 现在默认启动 720p 知识分享形象。原有的
`musetalk_test_avatar` 配置和缓存仍保留，可作为备用形象。

```bash
# 使用支持 mmpose 的虚拟环境，构建一次独立的数字人缓存。
cd musetalk_project/MuseTalk
CUDA_VISIBLE_DEVICES=2 PYTHONPATH=. PATH="../../minicpm_demo/.bin:$PATH" \
  ../venv/bin/python scripts/realtime_inference.py \
  --version v15 --gpu_id 0 \
  --unet_config ./models/musetalkV15/musetalk.json \
  --unet_model_path ./models/musetalkV15/unet.pth \
  --whisper_dir ./models/whisper \
  --inference_config configs/inference/prepare_knowledge_host.yaml \
  --parsing_mode jaw --left_cheek_width 90 --right_cheek_width 90

# 使用新的默认形象启动完整服务。
cd ../..
./start_avatar.sh start

# 使用原有的形象缓存启动。
MUSETALK_CONFIG=configs/streaming_duplex.yaml ./start_avatar.sh start
```

缓存会生成到：

```text
musetalk_project/MuseTalk/results/v15/avatars/knowledge_host_720p/
```

仓库同时内置三套完整缓存：`knowledge_host_720p`、`musetalk_test_avatar` 和
`ecommerce`。部署时无需重新生成缓存，可直接通过形象预设切换。

### 通过脚本添加新形象

根目录的 `add_avatar.sh` 会从原始视频生成 MuseTalk 缓存，并把音色、提示词和
形象预设一起写入配置：

```bash
./add_avatar.sh \
  /path/to/new_avatar.mp4 \
  new_avatar \
  /path/to/voice.wav \
  /path/to/prompt.txt
```

也可以复用项目内已有的音色和提示词：

```bash
./add_avatar.sh \
  /path/to/new_avatar.mp4 \
  new_avatar \
  default \
  knowledge_host
```

脚本会自动选择空闲显存最多的 GPU，将视频规范化为 24 FPS、偶数宽高、
H.264/yuv420p，然后生成
`musetalk_project/MuseTalk/results/v15/avatars/<avatar_id>/`，并更新
`minicpm_demo/assets/avatar_presets.yaml` 和 `MANIFEST.sha256`。
新预设会立即出现在 `/avatar` 页面，不需要重启 Gateway 或 MuseTalk。

运行脚本前不需要手动执行 `source .venv/bin/activate`。脚本会自动激活共享
`.venv`；生成视频缓存时会在子进程中切换到 `musetalk_project/venv`，因为只有
该环境安装了预处理所需的 `mmpose/mmcv`。

常用选项：

```bash
# 指定物理 GPU。
./add_avatar.sh --gpu 1 video.mp4 new_avatar voice.wav prompt.txt

# 自定义卡片名称和说明。
./add_avatar.sh --name "讲解员小艾" --description "产品介绍形象" \
  video.mp4 new_avatar voice.wav prompt.txt

# 只检查参数和目标路径，不生成文件。
./add_avatar.sh --dry-run video.mp4 new_avatar voice.wav prompt.txt
```

已有 `AVATAR_ID`、规范化视频或目标资产不会被覆盖。生成失败时，脚本会回滚本次
创建的视频、缓存、音色和提示词。要通过脚本设置启动默认形象，仍需另外修改
`avatar_presets.yaml` 的 `default_preset` 和 MuseTalk 配置中的 `avatar.id`。

### 通过 API 选择预处理后的形象

Gateway 可以在实时会话开始前，在完整的本地形象缓存之间切换。请求中不传
`avatar_id` 时，仍使用 `configs/streaming_duplex_knowledge.yaml` 中配置的默认形象。

```bash
# 列出完整和无效的本地形象缓存。
curl -k https://127.0.0.1:8006/avatar-live/api/avatars

# 选择内置的备用形象。
curl -k -X POST https://127.0.0.1:8006/avatar-live/api/avatar/select \
  -H 'Content-Type: application/json' \
  -d '{"avatar_id":"musetalk_test_avatar"}'

# 恢复配置中的默认形象。
curl -k -X POST https://127.0.0.1:8006/avatar-live/api/avatar/select \
  -H 'Content-Type: application/json' \
  -d '{}'
```

请在连接 `/avatar-live/ws/live` 之前调用形象选择接口。如果当前已经存在 viewer、
音频上游连接或正在执行的推理任务，形象切换会被拒绝。错误响应包含
`detail.code` 和 `detail.message`，当前错误码包括：

- `avatar_not_found`
- `avatar_invalid`
- `avatar_busy`
- `avatar_switching`
- `avatar_unavailable`
- `avatar_activation_failed`

`/avatar` 页面会在系统提示词上方展示形象预设。预设定义在
[avatar_presets.yaml](minicpm_demo/assets/avatar_presets.yaml)：

```yaml
presets:
  knowledge_host_720p:
    source: system
    avatar_id: knowledge_host_720p
    voice_ref: assets/ref_audio/ref_avatar_default.wav
    prompt_file: assets/prompts/avatar/knowledge_host.txt
```

点击预设卡片会立即调用形象选择 API。开始会话时，前端会把该预设对应的参考音频同时
写入 `ref_audio_base64` 和 `tts_ref_audio_base64`。前端会请求
`/avatar-live/api/avatar-presets`，并为每个已配置的预设渲染一张卡片。

新增形象时需要同时准备形象缓存、参考音频、提示词文件，并在 YAML 中增加对应配置。
YAML 支持热加载，修改后不需要重启 Gateway。

`source` 必须为 `system` 或 `user`。用户上传生成的 preset 写入运行时配置并标记
为 `user`，系统内置 preset 标记为 `system`。用户页面只允许删除自己的 `user`
preset；管理员可以删除任意 preset：

```bash
./delete_avatar.sh --dry-run musetalk_test_avatar
./delete_avatar.sh musetalk_test_avatar
```

删除前会扫描所有剩余 preset 的参考音频、提示词、缓存和基底视频引用。被其他
preset 共用的文件会保留。删除当前 default preset 时必须通过 `--new-default`
指定替代 preset。

用户通过前端删除自己正在使用的形象时，前端会二次确认，并在删除请求中发送
`switch_to_default=true`。Gateway 先将 MuseTalk 切换到可用的系统默认形象，
再删除用户 preset；默认切换失败时不会删除任何资源。

`/avatar` 还会使用自己的默认参考音色
`assets/ref_audio/ref_avatar_default.wav`，对应配置项
`audio.avatar_ref_audio_path`。`/omni` 页面及其预设仍使用全局默认音色配置
`audio.ref_audio_path`。

### 单连接 Avatar Session API

`/avatar-v2` 展示新的公共会话 API。业务调用方只需要一个服务地址：

```http
POST /v1/avatar/sessions
WSS  /v1/avatar/sessions/{session_id}/stream?token=...
```

创建请求可以只指定预设：

```json
{
  "preset_id": "knowledge_host_720p",
  "metadata": {"user_id": "demo"}
}
```

服务端完成形象激活、提示词/声线解析、Worker 分配、MiniCPM 会话和
MuseTalk viewer 编排。返回的 WebSocket 同时承载：

- JSON 控制、文字和状态事件；
- `MTA1` 二进制 WAV 片段；
- `MTH1`/`MTF1` 二进制视频帧。

浏览器端可以复用
[`avatar-session-client.js`](minicpm_demo/static/sdk/avatar-session-client.js)：

```js
const client = new AvatarSessionClient({
  canvas: document.querySelector('canvas'),
  presetId: 'knowledge_host_720p',
});
await client.connect();
await client.startMicrophone();
client.interrupt();
client.close();
```

SDK 负责麦克风采集、WebCodecs/WebP 解码、音画同步、ACK、打断和资源清理。
如配置 `AVATAR_SESSION_API_KEY`，REST 请求使用
`Authorization: Bearer <key>`；WebSocket 使用创建会话时返回的短期 token。
非浏览器客户端可参考
[`avatar_session_api_client.py`](minicpm_demo/examples/avatar_session_api_client.py)。
完整字段和二进制协议见
[`AVATAR_SESSION_API.md`](AVATAR_SESSION_API.md)。
跨机器平台接入的最小 BFF/静态代理示例见
[`example/minimal-platform`](example/minimal-platform/README.md)。

`/avatar` 和 `/avatar-v2` 支持上传最长 20 秒的视频和参考音频，异步生成用户
数字人缓存。任务状态通过 `/v1/avatar/customizations` 查询；用户预设默认私有，
生成完成后出现在预设列表中。直接页面仅适合同源演示；多用户部署应由可信 BFF
注入 `X-Avatar-Owner-ID`，不要信任浏览器自行提交的 owner。

形象配置使用 `source: system|user` 区分来源。用户可在页面删除自己的自建形象，
系统预设不能通过 API 删除。`./delete_avatar.sh PRESET_ID` 可由管理员删除任意
preset；所有删除都会先检查其他 preset 的引用，共享的音频、提示词、缓存和视频
不会被删除。

### H.264 数字人视频流

实时 viewer 现在默认使用 ffmpeg/libx264 编码的 H.264 Annex-B 视频流。浏览器通过
WebCodecs 解码，原有的音频主时钟继续驱动 Canvas 渲染：

```text
ws://<主机地址>:8000/ws/live?video_codec=h264
```

Gateway 会转发相同的查询参数：

```text
wss://<主机地址>:8006/avatar-live/ws/live?video_codec=h264
```

不支持 `VideoDecoder` 的客户端可以通过 `video_codec=webp` 使用原有图片帧模式。
项目内置的 MuseTalk 调试页面和无头 viewer 会显式使用 WebP。

H.264 默认参数为：目标码率 `2M`、最大码率 `3M`、每 120 帧插入一个 IDR。收到
`flush` 时，服务端会重启编码器并立即生成新的 IDR。可以通过 `.avatar-env` 中的
`AVATAR_H264_*` 变量调整这些参数。

`MTH1` 数据包格式和 WebCodecs 恢复规则见
[PLATFORM_INTEGRATION_GUIDE.md](PLATFORM_INTEGRATION_GUIDE.md)。`install.sh` 会检查
当前使用的 ffmpeg 是否包含 `libx264` 编码器。

## 目录结构

- `minicpm_demo/` — MiniCPM-o 4.5 全双工示例，包含 Gateway、Worker、Backend、Web UI、数字人桥接和 `/avatar` 页面
- `musetalk_project/MuseTalk/` — 流式 MuseTalk 服务，包含 `muse_stream/` 和 `/ws/minicpm` 输入
- `install.sh`、`start_avatar.sh`、`add_avatar.sh`、`delete_avatar.sh` — 一键安装、启动和形象管理脚本
- `requirements-avatar.lock.txt`、`weights.sha256` — 固定依赖版本和模型权重校验文件

设计与调度、桥接层和前端实现说明：[DIGITAL_HUMAN_INTEGRATION.md](DIGITAL_HUMAN_INTEGRATION.md)。

平台接入与 API 对接说明：[PLATFORM_INTEGRATION_GUIDE.md](PLATFORM_INTEGRATION_GUIDE.md)。
