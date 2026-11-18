# GPEN 流式修复

已接入并在本机启用。处理路径：

```text
MuseTalk 256 生成脸 → raw_queue
→ 修复线程：缓存原帧对齐 → GPEN-BFR-512 → 逆变换
→ restored_queue（容量 2）
→ 贴回线程：bbox 缩放 → 原 mask 贴回
→ fused_queue（容量 8） → 原 H.264 编码器 → 原 WebSocket 协议
```

第一版复现模型对照中 GPEN 的处理方式，**没有叠加肤色校正、嘴周区域约束或边界淡入淡出**。

## 行为

- GPEN 是常驻模型，启动时载入、校验权重并预热；关闭时不载入 GPEN。
- 只修复 `FRAME_GENERATED`，原帧待机、静音旁路、hold 继续使用原逻辑。
- H.264 采用独立修复线程、独立贴回线程及 GPEN CUDA stream；两个阶段通过容量 2 的有界队列重叠执行。WebP/JPEG 保留原串行调用路径。
- PTS、segment ID 和帧序保持不变。GPEN 错误上报并结束该流，不逐帧交替回退到未修复效果。
- 原始帧入队时携带 flush/形象版本；修复与贴回各自前后检查版本及取消状态，过期结果不进入编码器。H.264 flush/IDR 重置仍沿用原机制。
- 原帧对齐在离线预计算，在线不依赖人脸检测、facexlib、GFPGAN 或 `/tmp` 文件。
- 仅修改并重启了 MuseTalk；MiniCPM/gateway 服务未重启。

## 配置

项目根目录 `.avatar-env` 已设置：

```bash
MUSETALK_FACE_RESTORATION=gpen
```

代码默认 `off`；本地配置覆盖为 `gpen`。将其改为 `off` 并重启 MuseTalk 即可回到原版处理。已有启动器会导出此参数。

默认使用 MuseTalk 的逻辑设备。若明确有另一张可用 GPU，可配置：

```bash
MUSETALK_CUDA_VISIBLE_DEVICES=2
GPEN_CUDA_VISIBLE_DEVICES=3
```

启动器自动合并为可见列表 `2,3`，将 GPEN 映射到逻辑 `cuda:1`。当前本机配置为 MiniCPM=GPU0、MuseTalk=GPU2、GPEN=GPU2。

可选配置：

```bash
MUSETALK_GPEN_WEIGHTS=models/gpen/GPEN-BFR-512.pth
MUSETALK_GPEN_OPS=cuda
```

默认 `cuda`，使用官方 CUDA 算子。`native` 是诊断用的慢速兼容实现，不建议在线使用。首次启动会编译算子，后续复用缓存；需要 CUDA 工具链及：

```bash
.venv/bin/python -m pip install -r musetalk_project/MuseTalk/requirements-gpen.txt
```

本机已经安装 `ninja==1.13.2`。编译默认最多 4 个任务，缓存默认位于 `models/gpen/extensions/`；已有 `MAX_JOBS` / `TORCH_EXTENSIONS_DIR` 环境设置会优先保留。

## 持久资源和新形象

位于 MuseTalk 目录：

```text
models/gpen/GPEN-BFR-512.pth
models/gpen/alignment/detection_Resnet50_Final.pth
models/gpen/alignment/parsing_parsenet.pth
results/v15/avatars/<avatar_id>/gpen_alignment.npz
```

权重 SHA256：

```text
f1002c41add95b0decad69604d80455576f7187dd99ca16bd611bcfd44c10b51
```

三个原有 v15 形象 `knowledge_host_720p`、`ecommerce`、`musetalk_test_avatar` 均已准备 690 帧循环对齐缓存。没有覆盖原帧、bbox、mask 或 latent。

对齐文件记录坐标内容及原帧文件大小/时间签名、256 输入尺寸和版本。缺失、过期、形状错误、非有限值或奇异矩阵会明确拒绝加载；不会静默进入另一种外观。

**新建/重建形象后，需要在启用 GPEN 前运行对齐准备。** 当前第一版尚未把此步骤加入形象定制任务。准备工具使用独立的 facexlib/torchvision 环境；本机会话可执行：

```bash
PYTHONPATH=/root/paddlejob/workspace/env_run/avatar_full/audio_dual_avatar/.venv/lib/python3.10/site-packages \
CUDA_VISIBLE_DEVICES=1 \
/tmp/avatar-gfpgan-venv/bin/python deployment/prepare_gpen_alignment.py \
  --avatars knowledge_host_720p ecommerce musetalk_test_avatar
```

只有这个离线准备命令使用临时检测环境；在线服务完全使用项目内资源。未来清理 `/tmp` 后，需要另行创建含 facexlib 的准备环境。复制/更新原帧导致签名改变时也应重新准备。

## 状态与性能

`GET /api/status` 增加：

```json
{
  "face_restoration": {
    "mode": "gpen",
    "device": "cuda:0",
    "operators": "cuda",
    "frames": 137,
    "mean_ms": 42.63,
    "last_ms": 24.37
  }
}
```

耗时包括对齐、模型、GPU/CPU 转换及逆变换，不包括 raw_queue 等待、最终贴回、H.264、网络或播放器缓存。每修复 120 帧也会打印汇总日志。修复后因打断而被丢弃的帧仍计入模型调用数。

本次 GPU 0 同时承担 MiniCPM、MuseTalk、GPEN，实链路修复均值 **42.63 ms**，末帧 **24.37 ms**。这高于空闲 GPU 离线数据，**暂不承诺共享 GPU 下长期稳定 24 fps**。测试客户端收到帧就 ACK，验证了真实模型/传输，但没有测浏览器真实播放延迟及持续欠帧。若实际播放发现积压，应先测试独立可用 GPU；不应通过无限扩充队列隐藏延迟。

## 验证

- 单元测试：MuseTalk `tests/` 共 **23 passed**，覆盖原有编码/形象切换、GPEN 位置、关闭开关、资源校验，以及排队/执行中版本过期。新增功能先观察到对应失败，再实现。
- 8 个抽样帧中，接入版 CUDA 输出与官方离线 GPEN 输出逐像素一致。
- 免编译 native 算子也逐像素一致，但约 43 ms，不作为默认实现。
- 真实 MiniCPM 全双工会话：**137 个有声生成帧，137 次 GPEN 修复，1598 个 H.264 视频帧，6 个音频包，0 个会话错误**。
- 保存的整个 H.264 流成功解码，解码帧数 1598，与收到的数据包数一致。
- 切换 `ecommerce` 后切回 `knowledge_host_720p`，对齐缓存加载与状态恢复均成功。
- 没有逐帧导出修复图片。

结果位于 [gpen_streaming_validation](gpen_streaming_validation/)：

- `smoke-session-result-h264.json`：全双工事件及包计数。
- `gpen-status.json`：修复调用数及耗时。
- `decode-verification.json`：完整 H.264 解码检查。
- `avatar_switch.json`：真实形象切换检查。
- `model-parity.json`：离线/接入版输出比对。
- `unit-tests.txt`：测试运行结果。

复测运行：

```bash
.venv/bin/python deployment/validate_gpen_streaming.py
```

它会创建真实会话并播放固定测试输入，应在没有其他会话使用时执行。

## Physical GPU placement (2026-11-18)

`.avatar-env` supports `GPEN_CUDA_VISIBLE_DEVICES`, selecting exactly one physical
GPU index or CUDA UUID. The launcher keeps MuseTalk devices first, appends the
GPEN device if needed, and exports its logical `MUSETALK_GPEN_DEVICE`. This
physical setting takes precedence over the legacy logical override. Omit it to
retain the old behavior (GPEN defaults to the MuseTalk device). Use consistent
index/UUID notation for a device across both settings.

```bash
MINICPM_CUDA_VISIBLE_DEVICES=0
MUSETALK_CUDA_VISIBLE_DEVICES=2
GPEN_CUDA_VISIBLE_DEVICES=2
```

This example gives MuseTalk process visibility `2` and GPEN logical device
`cuda:0`. For GPEN on GPU3 instead, set `GPEN_CUDA_VISIBLE_DEVICES=3`; visibility
becomes `2,3`, with GPEN on logical `cuda:1`. Restart MuseTalk after changing GPU
placement; an already healthy process retains its startup environment.

The benchmark-only `deployment/profile_stream_server.py` records CUDA event
intervals without device synchronization and wall durations in a JSONL file.
Normal startup does not load these hooks. The measured split-GPU results are in
`deployment/gpu02_latency/REPORT.md`.

## 2026-11-18 修复与贴回异步拆分

`InferencePipeline.restore_frame` 负责原始生成脸调试导出及现有完整修复；
`composite_frame` 只负责原 bbox/mask 贴回。`blend_frame` 组合两者，保留旧调用兼容性。
H.264 会话启动两个 worker，恢复后的256uint8 crop通过容量2的FIFO传递；
所有idle/hold/segment/segment_end/error同序传递。新增队列纳入flush及断开清理，
修复/贴回各自检查flush_seq/avatar_revision/canceled_ids，编码端仍有最终过期检查。

每段结束打印`restoration stage`和`composite stage`的累计帧数、处理均值及队列深度，
均值不含等待输入或输出队列的时间。它们用于观察实际阶段耗时，不代表端到端延迟。

36项MuseTalk测试通过。真实gateway会话76帧生成/修复；固定16.637秒音频按播放时序ACK测试
400帧生成/修复且最终播放完成；中断测试未出现flush后旧segment泄漏，后续回答正常完成。
实测修复49.70ms/帧、贴回18.59ms/帧，合成客户端观察到约19.90fps，仍不能稳定24fps。
详情与边界见`deployment/gpen_async_validation/REPORT.md`。
