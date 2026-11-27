"""Synthesize question audio with PaddleSpeech (fastspeech2_csmsc + hifigan_csmsc, CPU).

Usage: python synth_questions.py <out_dir> <text1> [<text2> ...]   -> <out_dir>/q1.wav, q2.wav, ...
inputs/ was generated with:
  q1-q5: 什么是认知 / 认知和记忆有什么关系 / 如何提升认知 / 认知提升和思维拓展的关系 / 如何实现自我成长
  probe_what_asked.wav: 我刚才问了你什么问题;  probe_second_question.wav: 我第二个问题问的是什么
Needs a separate venv with paddlespeech; not required to run the other scripts.
"""
import sys

import nltk

nltk.download = lambda *a, **k: True  # no internet; English g2p data is not needed for zh

from paddlespeech.cli.tts.infer import TTSExecutor

tts = TTSExecutor()
for i, text in enumerate(sys.argv[2:], start=1):
    tts(text=text, am="fastspeech2_csmsc", voc="hifigan_csmsc", lang="zh",
        device="cpu", output=f"{sys.argv[1]}/q{i}.wav")
    print("ok", i, text, flush=True)
