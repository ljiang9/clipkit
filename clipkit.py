#!/usr/bin/env python3
"""clipkit — 一句话主题 → 短视频生产套件（剪映 / CapCut 可直接用）.

设计哲学：不渲染成片，只做「可编辑的生产资料」。
一次 LLM 调用，产出钩子、口播脚本、分镜、SRT 字幕、发布包；
之后剪辑全在剪映里手工完成——零额外成本，质量自己把控。
竖屏优先（抖音 / 小红书 / YouTube Shorts）。

用法：
    python -m clipkit "主题" --style 知识科普 --duration 60 --lang zh
    python -m clipkit "主题" --dry-run        # 不花钱，先看输出格式
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
import urllib.error

VERSION = "0.1.0"

# 口播语速估计（经验值，因人而异；时间轴均为估计，剪辑时以实际配音为准）
ZH_CHARS_PER_SEC = 4.5
EN_WORDS_PER_SEC = 2.8

STYLES = ["知识科普", "产品种草", "故事", "观点评论"]
STYLE_EN = {
    "知识科普": "educational explainer",
    "产品种草": "product recommendation",
    "故事": "storytelling",
    "观点评论": "opinion commentary",
}

HOOK_ARCHETYPES = {
    "zh": ["反常识", "数字冲击", "悬念"],
    "en": ["Counter-intuitive", "Number shock", "Curiosity gap"],
}

BGM_BY_STYLE = {
    "知识科普": ("轻快科技风纯音乐，约 120BPM，无人声",
                 "Upbeat tech-style instrumental, ~120BPM, no vocals"),
    "产品种草": ("活泼种草风 BGM，节奏明快带点俏皮",
                 "Bouncy lifestyle BGM, bright and playful tempo"),
    "故事": ("电影感叙事配乐，前轻后扬，情绪递进",
              "Cinematic narrative score, builds gradually"),
    "观点评论": ("节奏感强的新闻风底噪 / 鼓点，干脆利落",
                 "Punchy news-style underscore with drums"),
}


class KitError(Exception):
    """用户可读的失败（打到 stderr，exit 非零）。"""


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def slugify(topic: str) -> str:
    """主题 → 文件夹名。纯中文主题 fallback 到 clip-<hash>。"""
    s = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    if not s:
        s = "clip-" + hashlib.md5(topic.encode("utf-8")).hexdigest()[:8]
    return s[:40]


def unique_dir(parent: str, slug: str) -> str:
    path = os.path.join(parent, slug)
    if not os.path.exists(path):
        return path
    i = 2
    while os.path.exists(f"{path}-{i}"):
        i += 1
    return f"{path}-{i}"


def estimate_seconds(narration: str, lang: str) -> float:
    """按语速估计单幕口播时长（秒）。"""
    if lang == "zh":
        units = len(narration)
        rate = ZH_CHARS_PER_SEC
    else:
        units = len(narration.split())
        rate = EN_WORDS_PER_SEC
    return max(units / rate, 1.0)


def assign_timing(scenes: list[dict], duration: int, lang: str) -> None:
    """按口播长度比例分配时间轴，等比缩放到正好填满 duration（原地写入 start/end）。"""
    raws = [estimate_seconds(s.get("narration", ""), lang) for s in scenes]
    total = sum(raws) or 1.0
    factor = duration / total
    t = 0.0
    bounds = []
    for raw in raws:
        start = t
        t += raw * factor
        bounds.append((start, t))
    # 消除浮点漂移：最后一幕精确收在 duration
    bounds[-1] = (bounds[-1][0], float(duration))
    for scene, (start, end) in zip(scenes, bounds):
        scene["start"] = round(start, 2)
        scene["end"] = round(end, 2)


def srt_timestamp(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def mmss(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m:02d}:{s:02d}"


def extract_json(text: str) -> dict:
    """防御性解析：模型可能用 ```json 包裹，剥掉再 parse。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError as exc:
        raise KitError(f"模型返回不是合法 JSON：{exc}（前 200 字符：{t[:200]}）")
    if not isinstance(data, dict):
        raise KitError("模型返回的 JSON 不是对象")
    return data


def validate_kit(data: dict) -> dict:
    for key in ("hooks", "title_options", "scenes", "description", "hashtags"):
        if key not in data:
            raise KitError(f"模型返回缺少字段：{key}")
    if len(data["hooks"]) < 3:
        raise KitError("模型返回的 hooks 不足 3 个")
    if len(data["title_options"]) < 3:
        raise KitError("模型返回的标题不足 3 个")
    if not data["scenes"]:
        raise KitError("模型返回的 scenes 为空")
    for i, sc in enumerate(data["scenes"], 1):
        if not sc.get("narration"):
            raise KitError(f"第 {i} 幕缺少 narration")
        sc.setdefault("visual_prompt_en", "")
        sc.setdefault("overlay_text", "")
        sc.setdefault("b_roll", "")
    return data


# ----------------------------------------------------------------------------
# 干跑：不调 API，生成格式骨架
# ----------------------------------------------------------------------------

def dry_run_kit(topic: str, style: str, duration: int, lang: str) -> dict:
    n = min(10, max(3, round(duration / 12)))
    arch = HOOK_ARCHETYPES[lang]
    scenes = []
    for i in range(1, n + 1):
        if lang == "zh":
            narration = (
                f"【干跑占位】这是第 {i} 幕的口播文案示例。真实运行时，"
                f"这里会是 AI 为「{topic}」生成的口播稿，可直接复制去 TTS。"
            )
            b_roll = f"【干跑占位】第 {i} 幕的 B-roll / 分镜描述示例。"
            overlay = f"占位花字{i}"
        else:
            narration = (
                f"[dry-run placeholder] Scene {i} narration example for "
                f'"{topic}". The real run fills this with AI-written voiceover.'
            )
            b_roll = f"[dry-run placeholder] B-roll / shot description for scene {i}."
            overlay = f"Caption {i}"
        scenes.append({
            "narration": narration,
            "visual_prompt_en": (
                f"[dry-run placeholder] vertical 9:16 cinematic still for scene {i}, "
                "no text in image"
            ),
            "overlay_text": overlay,
            "b_roll": b_roll,
        })
    if lang == "zh":
        hooks = [f"【干跑占位】「{topic}」钩子示例文案（{a}式）" for a in arch]
        titles = [f"【干跑占位】「{topic}」标题示例 {i}" for i in (1, 2, 3)]
        description = f"【干跑占位】「{topic}」的发布简介示例。真实运行时由 AI 生成。"
        hashtags = ["干跑示例", "clipkit", "短视频", "剪映", "AI"]
    else:
        hooks = [f'[dry-run placeholder] hook example for "{topic}" ({a})' for a in arch]
        titles = [f'[dry-run placeholder] title example {i} for "{topic}"' for i in (1, 2, 3)]
        description = f'[dry-run placeholder] post description for "{topic}".'
        hashtags = ["dryrun", "clipkit", "shortvideo", "capcut", "ai"]
    return {
        "hooks": hooks,
        "title_options": titles,
        "scenes": scenes,
        "description": description,
        "hashtags": hashtags,
    }


# ----------------------------------------------------------------------------
# 真实调用：一次 chat/completions
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a short-video scriptwriter for vertical platforms \
(Douyin / Xiaohongshu / YouTube Shorts). Target language: {lang_name}. \
Topic: {topic}. Style: {style_en}. Target total duration: {duration} seconds, \
exactly {n_scenes} scenes (about {secs_per_scene}s of narration each).

Return STRICT JSON only — no markdown fences, no commentary — with exactly this shape:
{{
  "hooks": ["<hook 1: counter-intuitive>", "<hook 2: number shock>", "<hook 3: curiosity gap>"],
  "title_options": ["<title 1>", "<title 2>", "<title 3>"],
  "scenes": [
    {{"narration": "<speakable lines for this scene>",
      "visual_prompt_en": "<single English image-generation prompt, vertical 9:16, cinematic, no text in image>",
      "overlay_text": "<short on-screen caption, at most 10 characters>",
      "b_roll": "<B-roll / shot description, in {lang_name}>"}}
  ],
  "description": "<platform post description, 1-2 sentences>",
  "hashtags": ["<tag1>", "<tag2>", "<tag3>", "<tag4>", "<tag5>"]
}}

Rules:
- narration must be speakable text only: no markdown, no emoji, no stage directions.
- Keep each scene's narration within ±30% of the target length.
- hooks[0] must be counter-intuitive, hooks[1] number-shock, hooks[2] curiosity-gap; keep each under 20 characters.
- overlay_text at most 10 characters.
- visual_prompt_en must be copy-paste ready for Midjourney.
- Write EVERYTHING (except visual_prompt_en) in {lang_name}."""


def call_llm(topic: str, style: str, duration: int, lang: str) -> dict:
    api_key = os.environ.get("CLIPKIT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise KitError(
            "未找到 API key。请先设置：export OPENAI_API_KEY=sk-...（或 CLIPKIT_API_KEY），"
            "再重新运行；也可以先用 --dry-run 免费查看输出格式。"
        )
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("CLIPKIT_MODEL", "gpt-4o-mini")

    n_scenes = min(10, max(3, round(duration / 12)))
    lang_name = "简体中文" if lang == "zh" else "English"
    system = SYSTEM_PROMPT.format(
        lang_name=lang_name,
        topic=topic,
        style_en=STYLE_EN[style],
        duration=duration,
        n_scenes=n_scenes,
        secs_per_scene=round(duration / n_scenes),
    )
    user_msg = f"主题：{topic}\n风格：{style}\n时长：{duration}秒\n语言：{lang}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.8,
        "max_tokens": 3000,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise KitError(f"API 请求失败（HTTP {exc.code}）：{detail}")
    except urllib.error.URLError as exc:
        raise KitError(f"网络请求失败：{exc.reason}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KitError(f"API 返回无法解析：{exc}")

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise KitError(f"API 返回结构异常：{json.dumps(body)[:300]}")
    return validate_kit(extract_json(content))


# ----------------------------------------------------------------------------
# 输出文件
# ----------------------------------------------------------------------------

def write_script_md(out_dir: str, kit: dict, topic: str, style: str,
                    duration: int, lang: str) -> None:
    arch = HOOK_ARCHETYPES[lang]
    rate_note = "中文约 4.5 字/秒" if lang == "zh" else "英文约 2.8 词/秒"
    lines = [
        f"# 「{topic}」短视频脚本（{duration}s · {style}）",
        "",
        f"> 时间轴按口播语速估计（{rate_note}），剪辑时以实际配音为准。",
        "",
        "## 一、钩子三选一（前 3 秒用）",
        "",
    ]
    for a, hook in zip(arch, kit["hooks"][:3]):
        lines.append(f"- 【{a}】{hook}")
    lines += ["", "## 二、分镜脚本", ""]
    for i, sc in enumerate(kit["scenes"], 1):
        lines += [
            f"### Scene {i} · {mmss(sc['start'])}–{mmss(sc['end'])}",
            "",
            f"**口播：**{sc['narration']}",
            "",
            f"**画面：**{sc['b_roll']}",
            "",
            f"**花字：**{sc['overlay_text']}",
            "",
        ]
    lines += ["## 三、纯口播稿（一键复制去 TTS）", ""]
    for sc in kit["scenes"]:
        lines += [sc["narration"], ""]
    with open(os.path.join(out_dir, "script.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def write_scenes_json(out_dir: str, kit: dict, topic: str, style: str,
                      duration: int, lang: str) -> None:
    if lang == "zh":
        timing_note = "时间轴按语速估计：中文约 4.5 字/秒；仅供参考，剪辑时以实际配音为准。"
    else:
        timing_note = "Timing estimated from speech rate (~2.8 words/sec); approximate."
    doc = {
        "topic": topic,
        "style": style,
        "duration_sec": duration,
        "lang": lang,
        "timing_note": timing_note,
        "hooks": [
            {"archetype": a, "text": h}
            for a, h in zip(HOOK_ARCHETYPES[lang], kit["hooks"][:3])
        ],
        "title_options": kit["title_options"][:3],
        "scenes": [
            {
                "index": i,
                "start": sc["start"],
                "end": sc["end"],
                "narration": sc["narration"],
                "visual_prompt_en": sc["visual_prompt_en"],
                "overlay_text": sc["overlay_text"],
                "b_roll": sc["b_roll"],
            }
            for i, sc in enumerate(kit["scenes"], 1)
        ],
        "description": kit["description"],
        "hashtags": kit["hashtags"],
    }
    with open(os.path.join(out_dir, "scenes.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_srt(out_dir: str, kit: dict) -> None:
    lines = []
    for i, sc in enumerate(kit["scenes"], 1):
        lines += [
            str(i),
            f"{srt_timestamp(sc['start'])} --> {srt_timestamp(sc['end'])}",
            sc["narration"],
            "",
        ]
    with open(os.path.join(out_dir, "subtitles.srt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def write_shotlist_md(out_dir: str, kit: dict, topic: str, style: str,
                      lang: str) -> None:
    bgm = BGM_BY_STYLE[style][0 if lang == "zh" else 1]
    lines = [
        f"# 分镜表 shotlist（{topic}）",
        "",
        "> 图片：把 English prompt 直接粘贴到即梦 / Midjourney（加 --ar 9:16）；图中不要出现文字。",
        f"> BGM 建议：{bgm}",
        "",
    ]
    for i, sc in enumerate(kit["scenes"], 1):
        lines += [
            f"## Scene {i} · {mmss(sc['start'])}–{mmss(sc['end'])}",
            "",
            f"- **分镜描述：**{sc['b_roll']}",
            "- **生图 prompt（复制即用）：**",
            "  ```",
            f"  {sc['visual_prompt_en']} --ar 9:16",
            "  ```",
            f"- **花字：**{sc['overlay_text']}",
            "",
        ]
    with open(os.path.join(out_dir, "shotlist.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def write_publish_md(out_dir: str, kit: dict, style: str, lang: str) -> None:
    bgm = BGM_BY_STYLE[style][0 if lang == "zh" else 1]
    tags = " ".join(
        t if t.startswith("#") else "#" + t for t in kit["hashtags"]
    )
    if lang == "zh":
        checklist = [
            "1. 纯口播稿 → TTS 生成配音（剪映「文本朗读」/ Minimax / 即梦）",
            "2. 配音导入剪映 → 字幕 → 自动识别字幕（用 subtitles.srt 校验断句）",
            "3. 按 shotlist.md 逐幕配图 / 视频素材，9:16 竖屏",
            "4. 花字：overlay_text 做大字报 / 强调动画",
            f"5. BGM：{bgm}，音量压到 -18dB 左右，别盖住口播",
            "6. 封面：三选一标题 + hook 里最炸的一句",
            "7. 发布文案：复制下方简介 + hashtags",
        ]
        heads = ("# 发布包 publish pack", "## 标题三选一",
                 "## 简介", "## Hashtags", "## 剪映实操 checklist")
    else:
        checklist = [
            "1. Narration → TTS voiceover (CapCut text-to-speech / ElevenLabs)",
            "2. Import voiceover to CapCut → auto captions (verify against subtitles.srt)",
            "3. Add visuals per shotlist.md, 9:16 vertical",
            "4. Overlay text: animate overlay_text as bold captions",
            f"5. BGM: {bgm}; duck to about -18dB under voiceover",
            "6. Cover: pick one title + the punchiest hook",
            "7. Post copy: description + hashtags below",
        ]
        heads = ("# Publish pack", "## Pick one title",
                 "## Description", "## Hashtags", "## CapCut checklist")
    lines = [heads[0], "", heads[1], ""]
    lines += [f"{i}. {t}" for i, t in enumerate(kit["title_options"][:3], 1)]
    lines += ["", heads[2], "", kit["description"], "", heads[3], "", tags,
              "", heads[4], ""]
    lines += [f"- [ ] {c}" for c in checklist] + [""]
    with open(os.path.join(out_dir, "publish.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def build_kit(topic: str, style: str, duration: int, lang: str,
              out_base: str, dry_run: bool) -> str:
    if dry_run:
        kit = dry_run_kit(topic, style, duration, lang)
    else:
        kit = call_llm(topic, style, duration, lang)
    assign_timing(kit["scenes"], duration, lang)

    os.makedirs(out_base, exist_ok=True)
    out_dir = unique_dir(out_base, slugify(topic))
    os.makedirs(out_dir)

    write_script_md(out_dir, kit, topic, style, duration, lang)
    write_scenes_json(out_dir, kit, topic, style, duration, lang)
    write_srt(out_dir, kit)
    write_shotlist_md(out_dir, kit, topic, style, lang)
    write_publish_md(out_dir, kit, style, lang)
    return out_dir


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="clipkit",
        description="一句话主题 → 短视频生产套件（剪映/CapCut 可直接用）。一次 LLM 调用，零额外成本。",
    )
    p.add_argument("topic", nargs="?", help="视频主题，例如 \"为什么猫总在半夜跑酷\"")
    p.add_argument("--style", choices=STYLES, default="知识科普",
                   help="视频风格（默认：知识科普）")
    p.add_argument("--duration", type=int, default=60,
                   help="目标时长（秒，默认 60）")
    p.add_argument("--lang", choices=["zh", "en"], default="zh",
                   help="输出语言（默认 zh）")
    p.add_argument("--out", default="./clips", help="输出目录（默认 ./clips）")
    p.add_argument("--dry-run", action="store_true",
                   help="不调 API，生成格式骨架，免费预览输出格式")
    p.add_argument("--version", action="version", version=f"clipkit {VERSION}")
    args = p.parse_args(argv)
    if not args.topic:
        p.error("需要提供主题，例如：python -m clipkit \"为什么猫总在半夜跑酷\"")
    if args.duration <= 0:
        p.error("--duration 必须是正整数")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        out_dir = build_kit(args.topic, args.style, args.duration,
                            args.lang, args.out, args.dry_run)
    except KitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"done: {out_dir}")
    print("files: script.md scenes.json subtitles.srt shotlist.md publish.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
