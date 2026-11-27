# 插话（barge-in）机制测试

直连 MiniCPM-o 双工 worker，用合成语音模拟用户插话，对比不同解码配置。背景、结果和结论见 `../../../DUPLEX_INTERRUPTION_REPORT.md`；历史数据在 `../../../deployment/duplex_interrupt_ab/`。

这些是需要 GPU 和模型的实验脚本，文件名不以 `test_` 开头，不会被 pytest 收集。

## 文件

- `duplex_probe.py`：单个会话。场景 `wait` / `barge` / `probe`（句中插话）/ `late`（句末附近插话），`--variant` 传入 duplex 配置覆盖项。
- `variants.json`：A/B 用的配置组合（base、A=方案 1、AC=加"已接管"规则、ACB=再加方案 2 的 p_speak 门限）。
- `run_ab.sh`：按轮交错跑多个变体。
- `analyze.py`：汇总停下率、停下后是否回答、句末插话结果分类、无人说话时自己开口等指标。
- `summarize.py`：打印逐会话转录。
- `calib_eot.py` / `calib_speak.py`：从 backend 日志的 TokenTrace 统计句末 `p_eot` 和 listen chunk 的 `p_speak`，用于校准阈值。要求日志里只有对应目录的会话。
- `test_instance.sh`：在 GPU 上另起一套 backend（22501）+ worker（22401），不注册到 gateway。
- `synth_questions.py`：用 PaddleSpeech 重新生成 `inputs/` 里的问题音频。
- `inputs/`：q1–q5 问题、两条追问音频。

## 复现 2026-11-27 的 A/B（AC vs ACB）

```bash
cd minicpm_demo/tests/interruption
bash test_instance.sh start
bash run_ab.sh --out ../results/interruption/ab6 --variants AC,ACB    # 约 75 分钟
python3 analyze.py ../results/interruption/ab6
python3 summarize.py ../results/interruption/ab6/ACB_lp1.05
bash test_instance.sh stop
```

单个会话：

```bash
../../../.venv/bin/python duplex_probe.py --url ws://127.0.0.1:22401/v1/worker/duplex \
  --scenario late --barge-after 10 --variant "$(python3 -c 'import json;print(json.dumps(json.load(open("variants.json"))["ACB"]))')"
```

阈值校准（只跑一种配置，日志与 JSON 一一对应）：

```bash
bash test_instance.sh stop; rm -f ../results/interruption/instance/backend.log; bash test_instance.sh start
bash run_ab.sh --out ../results/interruption/calib --variants AC --late 12 --probe 12 --wait 4
python3 calib_speak.py ../results/interruption/instance/backend.log ../results/interruption/calib/AC_lp1.05
```
