"""流水线编排：一条音频可生成多条变体（不同背景 + 不同去重参数）。

关键点：同一条音频的字幕只用 Whisper 转写 **一次**，所有变体复用，
避免每条都重复转写（长音频转写很慢）。
"""
from __future__ import annotations

import random
import re
import time
from pathlib import Path

from . import background, guide, media, overlay, subtitle
from .dedup import random_params

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}


def list_audios(audio_dir: Path) -> list[Path]:
    return [p for p in sorted(audio_dir.iterdir())
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS]


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "untitled"


def _next_start_index(output_dir: Path, safe_kw: str) -> int:
    """扫描输出目录里 '关键词 N.mp4'，返回下一个可用序号（最大 N + 1）。

    实现跨次连续编号：同一关键词再次生成时，从已有最大序号往后接着排。
    """
    max_n = 0
    if output_dir.exists():
        pat = re.compile(r"^" + re.escape(safe_kw) + r" (\d+)$")
        for p in output_dir.glob(f"{safe_kw} *.mp4"):
            m = pat.match(p.stem)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def _render_variant(cfg, audio: Path, eff_d: float, atempo: float,
                    keyword: str, guide_png: Path | None, ass_rel: str | None,
                    out_name: str, work_tag: str, log,
                    rng: random.Random, bg_dir: Path | None = None) -> dict:
    """生成一条成品：随机背景 + 随机去重 -> pass1 -> pass2(叠字+合音频) -> 封面。"""
    result = {"keyword": keyword, "audio": str(audio), "ok": False,
              "output": None, "cover": None, "error": None, "task_id": work_tag}

    # 背景素材 + 去重参数（每条变体各自随机 → 背景与观感都不同）
    src_bg = bg_dir or cfg.input_bg_dir            # 关键词专用素材夹优先,否则全局
    pool = background.list_backgrounds(src_bg)
    clips = background.select_backgrounds(pool, cfg.ffprobe, eff_d, rng)
    params = random_params(cfg, rng)
    log(f"    背景 {len(clips)} 段  (镜像={params['mirror']} zoom={params['zoom']} "
        f"fps={params['fps']} 变速={params['speed']} 锐化={params['sharpen']} "
        f"淡出={params['fade_out']}s)")

    # Pass1：拼背景 + 去重 -> base.mp4（无音）
    base_mp4 = cfg.work_dir / f"{work_tag}_base.mp4"
    p1_script = cfg.work_dir / f"{work_tag}_p1.txt"
    p1_script.write_text(background.build_base_script(cfg, clips, params),
                         encoding="utf-8")
    p1_args = []
    for c in clips:
        p1_args += ["-i", str(c)]
    p1_args += [
        "-/filter_complex", str(p1_script),
        "-map", "[outv]", "-an", "-t", f"{eff_d:.3f}",
        "-c:v", "libx264", "-preset", cfg.x264_preset,
        "-crf", str(cfg.base_crf), "-pix_fmt", "yuv420p", str(base_mp4),
    ]
    media.run_ffmpeg(cfg.ffmpeg, p1_args, cfg.root, log)

    # 水印 + Pass2：字幕/水印/引导词图 + 合音频 -> 成品
    wm_rel = overlay.prepare_watermark(cfg, work_tag, keyword)
    guide_input = "2:v" if guide_png else None  # 引导词图作为第 3 个输入
    vfilter = overlay.build_overlay_filter(cfg, eff_d, wm_rel, ass_rel, guide_input)

    # 后处理:片尾淡出 + 变速(setpts)。接在叠字之后 → 字幕/引导跟着一起变速,不脱轨。
    sp = float(params.get("speed", 1.0))
    fout = float(params.get("fade_out", 0.0))
    post = []
    if fout > 0.05:
        post.append(f"fade=t=out:st={max(0.0, eff_d - fout):.2f}:d={fout:.2f}")
    if abs(sp - 1.0) > 1e-6:
        post.append(f"setpts=PTS/{sp}")
    if post:
        i = vfilter.rfind("[outv]")
        vfilter = vfilter[:i] + "[ovraw]" + vfilter[i + len("[outv]"):]
        vfilter += ";\n[ovraw]" + ",".join(post) + "[outv]"

    total_tempo = atempo * sp                      # 音频总变速(atempo 保音高不变调)
    p2_content = vfilter
    if abs(total_tempo - 1.0) > 1e-6:
        p2_content += f";\n[1:a]atempo={total_tempo:.5f}[outa]"
    p2_script = cfg.work_dir / f"{work_tag}_p2.txt"
    p2_script.write_text(p2_content, encoding="utf-8")

    # 编码档:目标体积>0 时按体积算码率(1-pass ABR),否则用 crf(画质模式)
    # 体积用区间随机(不固定死,也算一个去重维度);去重滤镜已在画面里,不受影响
    rng_range = getattr(cfg, "target_size_mb_range", None)
    if rng_range and float(rng_range[1]) > 0:
        target_mb = rng.uniform(float(rng_range[0]), float(rng_range[1]))
    else:
        target_mb = float(getattr(cfg, "target_size_mb", 0) or 0)
    audio_k = 128
    if target_mb > 0:
        total_k = target_mb * 8192 / max(1.0, eff_d)      # 目标总码率 kbps
        vid_k = max(400, int(total_k - audio_k))
        venc = ["-b:v", f"{vid_k}k", "-maxrate", f"{int(vid_k * 1.6)}k",
                "-bufsize", f"{vid_k * 2}k"]
        log(f"    目标体积 ~{target_mb:.0f}MB -> 视频码率 {vid_k}k")
    else:
        venc = ["-crf", str(params["crf"])]

    out_mp4 = cfg.output_dir / f"{out_name}.mp4"
    p2_args = ["-i", str(base_mp4), "-i", str(audio)]
    if guide_png:
        p2_args += ["-loop", "1", "-i", str(guide_png)]  # 静态图循环，-shortest 收尾
    p2_args += [
        "-/filter_complex", str(p2_script),
        "-map", "[outv]",
        "-map", ("[outa]" if abs(total_tempo - 1.0) > 1e-6 else "1:a"),
        "-c:v", "libx264", "-preset", cfg.x264_preset, *venc,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_k}k",
        "-shortest", "-movflags", "+faststart", str(out_mp4),
    ]
    media.run_ffmpeg(cfg.ffmpeg, p2_args, cfg.root, log)
    result["output"] = str(out_mp4)
    log(f"    ✓ 成品 {out_mp4.name}")

    # 封面 PNG（= 成品第 1 帧）
    if cfg.export_cover_png:
        cover_png = cfg.output_dir / f"{out_name}_cover.png"
        media.run_ffmpeg(cfg.ffmpeg,
                         ["-i", str(out_mp4), "-frames:v", "1", "-update", "1",
                          str(cover_png)], cfg.root, log)
        result["cover"] = str(cover_png)

    result["ok"] = True
    return result


def process_audio(cfg, audio: Path, log, rng: random.Random | None = None,
                  variants: int = 1, keep_work: bool = False,
                  keyword: str | None = None, app: str | None = None,
                  bg_dir: str | Path | None = None) -> list[dict]:
    """处理一条音频，生成 variants 条成品（背景/去重各不相同，字幕复用一次转写）。

    keyword/app 可显式指定(改文音频用):否则 keyword=文件名、app=全局 cfg.app_name。
    """
    rng = rng or random.Random()
    keyword = keyword or audio.stem  # 文件名(去扩展名) = 关键词
    bg_path = Path(bg_dir) if bg_dir else None
    if bg_path and not bg_path.exists():
        log(f"[提示] 关键词专用素材夹不存在,改用全局素材: {bg_path}")
        bg_path = None
    ts = time.strftime("%Y%m%d_%H%M%S")
    audio_id = _safe_name(keyword) + "_" + ts
    variants = max(1, int(variants))

    log(f"▶ 处理「{keyword}」  音频: {audio.name}  →  生成 {variants} 条")

    # 时长（考虑可选音频变速）
    raw_d = media.probe_duration(cfg.ffprobe, audio)
    atempo = float(cfg.audio_atempo or 1.0)
    eff_d = raw_d / atempo if abs(atempo - 1.0) > 1e-6 else raw_d
    log(f"  音频时长 {raw_d:.1f}s"
        + (f" -> 变速后 {eff_d:.1f}s" if atempo != 1.0 else ""))

    # 字幕：整条音频只转写一次，所有变体复用
    ass_path = cfg.work_dir / f"{audio_id}.ass"
    ass = subtitle.transcribe_to_ass(cfg, audio, ass_path, log)
    ass_rel = str(ass.relative_to(cfg.root)).replace("\\", "/") if ass else None
    has_sub = bool(ass)

    # 引导词图：整条音频只渲染一次（同关键词/App/图标），所有变体复用
    guide_png = cfg.work_dir / f"{audio_id}_guide.png"
    try:
        guide.render_guide_png(cfg, keyword, guide_png, log, app=app)
    except Exception as e:
        log(f"[警告] 引导词图渲染失败，本条不带引导词: {e}")
        guide_png = None

    results: list[dict] = []
    try:
        # 成品命名：关键词 + 序号，跨次连续（从输出目录里已有最大序号往后接着排）
        safe_kw = _safe_name(keyword)
        base_idx = _next_start_index(cfg.output_dir, safe_kw)
        for i in range(1, variants + 1):
            out_name = f"{safe_kw} {base_idx + i - 1}"
            work_tag = f"{audio_id}_v{i}"  # 中间文件仍带时间戳，避免并发冲突
            if variants > 1:
                log(f"  [第 {i}/{variants} 条]")
            try:
                res = _render_variant(cfg, audio, eff_d, atempo, keyword,
                                      guide_png, ass_rel, out_name, work_tag,
                                      log, rng, bg_dir=bg_path)
            except Exception as e:  # 单条失败不影响其它条
                log(f"    ✗ 第 {i} 条失败: {e}")
                res = {"keyword": keyword, "audio": str(audio), "ok": False,
                       "output": None, "cover": None, "error": str(e),
                       "task_id": work_tag}
            res["subtitle"] = has_sub
            res["variant"] = i
            res["audio_id"] = audio_id
            results.append(res)
    finally:
        if not keep_work:
            for p in cfg.work_dir.glob(f"{audio_id}*"):
                try:
                    p.unlink()
                except OSError:
                    pass
    return results
