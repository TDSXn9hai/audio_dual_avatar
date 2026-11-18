# 本机安装与运行记录

验证日期：2026-11-17。

项目目录：`/root/paddlejob/workspace/env_run/avatar_full/audio_dual_avatar`

Git 源：`https://github.com/TDSXn9hai/audio_dual_avatar`

安装的提交：`16a4e8b0ef0b4740c90805c9e773899422be6b63`。

## 日常启动

```bash
cd /root/paddlejob/workspace/env_run/avatar_full/audio_dual_avatar
./start_avatar.sh
./start_avatar.sh status
./start_avatar.sh stop
```

启动脚本会选择项目虚拟环境，无需手动激活。

- 数字人页面：`https://127.0.0.1:8006/avatar`
- 单连接 API 页面：`https://127.0.0.1:8006/avatar-v2`
- 远程访问：把 `127.0.0.1` 换成服务器可访问的地址，并确保 8006 端口可达。
- 使用安装脚本生成的自签名 HTTPS 证书。浏览器首次访问时需要接受证书，并在开始对话时允许麦克风。
- 当前服务保持运行；测试会话已经关闭，Worker 恢复 idle。

GPU 分配保存在项目根目录 `.avatar-env`：

```bash
MINICPM_CUDA_VISIBLE_DEVICES=0
MUSETALK_CUDA_VISIBLE_DEVICES=0
```

## 虚拟环境

| 用途 | 路径 | 已验证的核心版本 |
|---|---|---|
| 实时服务 | `.venv` | Python 3.10.12、torch 2.8.0+cu128、transformers 4.51.0、diffusers 0.30.2 |
| 新形象预处理 | `musetalk_project/venv` | Python 3.10.12、torch 2.1.0+cu121、torchvision 0.16.0+cu121、numpy 1.26.4、mmcv 2.1.0、mmpose 1.3.2 |
| 浏览器验证工具 | `deployment/browser-venv` | Playwright / Chromium |

实时依赖按照 `requirements-avatar.lock.txt` 安装，预处理依赖按照安装脚本要求和
`requirements-musetalk-preprocess.lock.txt` 安装。完整安装版本分别记录在
`runtime-installed.txt`、`preprocess-installed.txt`。

本机安装时解决了以下兼容问题：

1. 系统默认 pip 内网源返回 502，安装改用官方 PyPI，并通过用户提供的代理访问。
2. `chumpy` 构建依赖未声明 `pip`，使用 `uv-preprocess.toml` 为隔离构建补充依赖。
3. MMCV 使用 OpenMMLab 官方提供的 CUDA 12.1 / torch 2.1 Linux CPython 3.10 wheel，
   文件保存在 `deployment/wheels/`，已检查 ZIP CRC 完整性。
4. MMEngine 的实际预处理导入需要 `pkg_resources`，因此把预处理环境的 setuptools
   固定为 80.9.0，额外固定要求记录在 `requirements-preprocess-compat.txt`。
5. 为 Chromium 验证补齐了缺少的系统动态库和中文字体。

预处理环境 `pip check` 返回 `No broken requirements found`。
已实际导入 `musetalk.utils.preprocessing` 并加载 DWPose/S3FD，且运行了 MMCV CUDA NMS 算子。
运行环境遵照上游使用 `--no-deps` 安装：上游已明确说明
`minicpmo-utils` 声明的 librosa 0.9.0 与锁文件中的 0.11.0 不一致；
运行环境 `pip check` 的输出保留在 `runtime-pip-check.log`，不能将其解释为依赖检查全通过。
实际模型推理和音视频测试结果见下文。

## 权重和形象缓存

- `/root/paddlejob/workspace/env_run/model` 中的 26 个 MiniCPM 文件逐一通过项目清单的
  SHA256 校验后，复制到 `minicpm_demo/model`，共 19,991,358,546 字节。
- 另外下载了 MuseTalk V1.5 UNet、SD VAE、Whisper tiny、面部解析模型、S3FD 和 DWPose。
- `install.sh --skip-env --skip-preprocess-env --skip-weights` 对清单中的全部 36 个文件
  重新校验，结果为 `Checksums OK`，同时确认经典形象缓存完整。
- DWPose 另按 Hugging Face 文件元数据中的 SHA256 校验。
- 大文件使用经过 Content-Range 检查的 HTTP 分段续传，最终仍进行 SHA256 校验。

该 Git 提交实际上只带经典形象的完整缓存，README 提到的默认 720p 和电商缓存缺失。
首次实际启动因此触发了实时环境无法完成的预处理。安装过程中已使用独立预处理环境，
从仓库自带视频生成这两套缓存。当前形象接口确认以下三套均 `valid: true`：

| 形象 | 尺寸 | 循环帧数 |
|---|---|---|
| knowledge_host_720p | 768×1344 | 690 |
| musetalk_test_avatar | 480×864 | 690 |
| ecommerce | 768×1344 | 690 |

默认形象仍为 `knowledge_host_720p`。预处理命令及输出见
`prepare-knowledge-host.log`、`prepare-ecommerce.log`，电商预处理配置为
`prepare-ecommerce.yaml`。

## 实际运行验证

最终 `start_avatar.sh` 启动成功，四个服务均 healthy：

- MuseTalk：8000，默认形象 ready；
- Gateway：HTTPS 8006，内部 HTTP 8007；
- Backend：22500，模型 ready；
- Worker：22400，注册为 `worker-0`，测试后恢复 idle。

浏览器打开 `/avatar`，返回 HTTP 200，页面显示“服务已就绪”，三个预设均展示；
未捕获到页面异常或失败网络请求。截图为 `avatar-page.png`，详细记录为
`browser-result.json`。

通过公共 Avatar Session API 建立真实会话，输入仓库中的
`minicpm_demo/tests/cases/common/user_audio/000_user_audio0.wav`，随后发送静音，
接收模型回复、生成语音和口型视频：

| 视频路径 | 视频包数 | 语音包数 | 生成帧验证 |
|---|---:|---:|---|
| WebP | 1,398 | 22 | MuseTalk 记录 521 个生成帧 |
| 默认 H.264 | 1,811 | 5 | 客户端收到 105 个生成帧 |

两个会话均未返回 `error`。导出的 H.264 流由 ffmpeg 成功完整解码
1,811 帧，尺寸为 768×1344，24 FPS。导出的语音 WAV 可读取，16 kHz，
峰值约 0.528，确认具有非零音频信号。

测试通过服务器 API 和本机无头浏览器完成；远程用户的浏览器麦克风、
扬声器及网络访问条件仍取决于其客户端环境。

主要证据：

- `install-final-verification.log`：36 个权重文件的 SHA256 校验。
- `local-model-check.log`：本地 MiniCPM 比对与复制记录。
- `preprocess-check.json`、`preprocess-imports.log`：预处理环境与 CUDA 算子检查。
- `start-avatar.log`、`service-status.txt`、`final-health.json`：启动及最终健康状态。
- `avatars.json`：三个形象缓存均有效。
- `smoke-session-result.json`、`smoke-session-result-h264.json`：完整会话结果。
- `musetalk-during-session.json`：WebP 会话之后的生成帧计数。
- `avatar-stream.h264`、`h264-decode.log`、`h264-frame.png`：H.264 实际媒体及解码证据。
- `generated-speech.wav`：生成的语音片段。

## 重跑验证与日志

```bash
cd /root/paddlejob/workspace/env_run/avatar_full/audio_dual_avatar
.venv/bin/python deployment/smoke_session.py
AVATAR_SMOKE_CODEC=h264 .venv/bin/python deployment/smoke_session.py
deployment/browser-venv/bin/python deployment/check_browser.py
```

重跑验证前应结束页面中已有的对话，服务同一时刻只允许一个数字人会话。

常驻日志：

```text
.run/logs/musetalk.log
minicpm_demo/.run-logs/gateway.log
minicpm_demo/.run-logs/backend.log
minicpm_demo/.run-logs/worker.log
```

需要重新安装或下载时，外网代理使用：

```bash
export https_proxy=http://agent.baidu.com:8891
export http_proxy=http://agent.baidu.com:8891
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
```

运行本地服务不需要外网代理。安装日志、下载脚本和测试脚本保存在本目录，便于复查。


## 2026-11-17：停止超时排查及 GPU 0 合并部署

用户停止时，日志显示内部端口先完成退出，公开端口停留在
`Waiting for connections to close`，Worker 健康检查继续运行，30 秒后被外层脚本 SIGKILL。
Gateway 的两个 Uvicorn 配置原本没有指定 `timeout_graceful_shutdown`，默认值为 None，
会无限等待未完成连接，导致应用 lifespan 清理无法开始。原进程已经被杀掉，
无法从现有日志确认当时具体残留的连接类型。

新增 `minicpm_demo/tests/test_gateway_shutdown.py`，用真实独立 Gateway 进程和未发送完
请求体的 HTTP 客户端复现：修复前空闲退出通过，但有挂起请求时 15 秒仍不退出，
日志与原来的等待阶段相同。给公开和内部 Uvicorn 配置均设置
`timeout_graceful_shutdown=10` 后，两项测试均通过（约 12 秒），两个端口均释放。
达到该时限时 Uvicorn 取消未完成请求，然后运行应用清理；外层 30 秒 SIGKILL 保护保留。

`.avatar-env` 已改成 MiniCPM 与 MuseTalk 均使用物理 GPU 0。
回归结果见 `gateway-shutdown-before.log`、`gateway-shutdown-after.log`，
原现场日志备份为 `gateway-original-stop-timeout.log`。

单卡实际验证：H.264 会话收到 1777 个视频帧、
5 个语音片段，其中 100 个生成帧；无会话 error。
本机浏览器检查通过，无脚本异常或失败请求。随后完整执行 `start_avatar.sh stop`，
耗时 1.88 秒，返回码 0，没有 SIGKILL，Gateway 完成 lifespan 清理。
相关证据：`smoke-gpu0-result.json`、`gpu0-process-placement.json`、
`stop-gpu0.log`、`stop-gpu0-result.json`、`restart-gpu0.log`。

再次启动完成，四个服务健康，MiniCPM 与 MuseTalk 进程均确认 CUDA_VISIBLE_DEVICES=0。最终记录为 `final-health-gpu0.json`、`service-status-gpu0.txt` 和 `gpu-final-gpu0.txt`；服务保持运行。
