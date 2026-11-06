# 本地聊天语音

主聊天与聊天室默认使用手机本地流式识别。设置 → 语音转文字 → 本机聊天语音识别，可立即切回云端，仅影响这台手机；通话与唤醒沿用原配置。取消不上传；发送仍上传 WAV 语音附件用于回听，附带本地最终文本，不再请求云端转写。失败不自动切云端。

模型：官方 [small bilingual Zipformer 2023-02-16](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html)，INT8 encoder/joiner、FP32 decoder，60,142,871 字节。运行库：[sherpa-onnx 1.13.8 AAR](https://github.com/k2-fsa/sherpa-onnx/releases/tag/v1.13.8)，Apache-2.0，附许可证于 assets/asr。模型文件和运行库按固定 SHA256 校验，二进制不进 Git。

构建前执行 `python scripts/prepare-local-asr.py`（首次下载官方完整模型归档约 458 MB，只保留所需约 60 MB；后续复用校验通过的文件），再运行 `gradlew assembleDebug -PtargetAbi=arm64-v8a`（本次 vivo）。省略 targetAbi 会同时打包所有 CPU 类型，APK 更大；仅 JNI 与 ONNX runtime 为必需原生库。本地已有试用模型可传 `--trial-model <模型目录>` 避免重复下载。

CPU 单线程；模型首次按住时加载，等待期间缓存音频。松手补尾音并排空解码；空闲 30 秒释放模型。单条上限 120 秒；后台与页面退出取消会话；会话 ID 隔离过期回调。中英文单独说较好，中英混杂较弱。

2026-10-24 vivo X300 Pro 独立试用进程测量：120.082 秒内 CPU 15.028 秒（单核 12.51%），结束 PSS 202,172 KiB（约 197 MiB）。这是试用程序整进程占用，并非集成进小家后的精确增量。电池温度约 27–28°C；USB 充电，未测电池掉电率或长时间热表现。
