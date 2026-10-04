# clipkit ✂️

一句话主题 → 短视频**生产套件**：钩子、口播脚本、分镜、SRT 字幕、发布包，一次生成，直接丢进剪映开干。

## 为什么不用 MoneyPrinterTurbo？

| | MoneyPrinterTurbo | clipkit |
|---|---|---|
| 产物 | 直接渲染成片 MP4 | 可编辑的生产套件（脚本 / SRT / 分镜 / 发布包） |
| 成本 | 依赖付费 TTS + 生图 API，按量烧钱 | **一次 LLM 调用**，之后零额外成本 |
| 可编辑性 | 成片很难改 | 全文本，随便改 |
| 依赖 | 装一堆包、配多个 key | **零依赖**，Python 标准库 only |
| 适合 | 全自动挂机 | 想自己把控质量的创作者 |

核心小巧思：**不跟 AI 抢剪辑活**。AI 最擅长的是写文案、想钩子、排分镜；拼接、调字幕、选 BGM 这些，人用剪映 10 分钟搞定还更好看。clipkit 只做 AI 擅长的那一半。

## 安装

零依赖，Python 3.10+：

```bash
git clone https://github.com/ljiang9/clipkit && cd clipkit
```

## 快速开始

先免费看输出格式（不调 API，一分钱不花）：

```bash
python -m clipkit "为什么猫总在半夜跑酷" --dry-run
```

正式生成（需要 `OPENAI_API_KEY`，兼容任何 OpenAI 格式的接口）：

```bash
export OPENAI_API_KEY=sk-...
python -m clipkit "为什么猫总在半夜跑酷" --style 知识科普 --duration 60
# done: ./clips/clip-a1b2c3d4
```

常用参数：

```
--style 知识科普|产品种草|故事|观点评论   视频风格（默认 知识科普）
--duration 60                             目标时长秒数（默认 60）
--lang zh|en                              输出语言（默认 zh）
--out ./clips                             输出目录（默认 ./clips）
--dry-run                                 不调 API，生成格式骨架
```

环境变量：`OPENAI_API_KEY`（或 `CLIPKIT_API_KEY`）、`OPENAI_BASE_URL`（默认 `https://api.openai.com/v1`）、`CLIPKIT_MODEL`（默认 `gpt-4o-mini`）。

## 输出文件说明

每次生成一个文件夹 `clips/<slug>/`，里面 5 个文件：

| 文件 | 干嘛的 |
|---|---|
| `script.md` | 3 个钩子（三选一）+ 逐幕脚本（含时间轴）+ 纯口播稿（一键复制去 TTS） |
| `scenes.json` | 机器可读的分镜数据（start/end 秒、文案、prompt…） |
| `subtitles.srt` | 标准 SRT 字幕，时间轴按语速估计 |
| `shotlist.md` | 每幕：中文分镜描述 + English 生图 prompt（即梦/Midjourney 复制即用）+ 花字 + BGM 建议 |
| `publish.md` | 3 个标题 + 简介 + hashtags + 剪映实操 checklist |

时间轴按语速估计（中文约 4.5 字/秒，英文约 2.8 词/秒），是估计值——剪辑时以实际配音为准。

## 剪映实操流程

1. `script.md` 里的**纯口播稿** → 丢进 TTS（剪映「文本朗读」/ Minimax / 即梦）生成配音
2. 配音导入剪映 → 字幕 → 自动识别字幕（拿 `subtitles.srt` 校验断句）
3. 按 `shotlist.md` 逐幕配图/视频素材，9:16 竖屏
4. 花字：`overlay_text` 做大字报/强调动画
5. BGM 按建议配，音量压到 -18dB 别盖口播
6. 封面：三选一标题 + hook 里最炸的一句
7. 发布：复制简介 + hashtags，完事

`examples/sample-dry-run/` 里有一份现成的干跑输出，不用运行也能看格式。

## License

MIT © 2026 ljiang9
