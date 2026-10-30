# Live audio input

把上游生成的语音段写入 `data/audio/` 即可开始直播。支持 `.wav`、`.mp3`、
`.m4a`、`.aac`、`.ogg`、`.flac`。

文件会按修改时间/文件名顺序处理；写入完成后需保持大小稳定
`live.file_stable_seconds` 秒才会被转码。服务端不会删除这里的源文件。
