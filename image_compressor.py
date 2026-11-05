#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量图片/GIF压缩工具（智能保留版 · 修复增强）
功能：
  - 静图：自定义宽度，高度等比例，自定义最大文件大小
  - GIF动图：自定义宽度，高度等比例，自定义最大文件大小
  - 满足条件直接复制原图至输出目录
  - GIF 可选：宽度和体积都已达标时跳过压缩，直接复制；取消勾选则全部压缩
  - 输出格式偏好：自动(智能保留) / 全部JPEG / 全部WebP / 全部PNG
  - 质量优先 + 稳定性修复（停止可控 / 句柄管理 / 线程安全 / 体积兜底）
依赖：pip install Pillow imageio-ffmpeg
压缩gif图片可以下载ffmpeg，压缩的质量会更好
"""
import os
import io
import sys
import math
import time
import shutil
import tempfile
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageSequence, ImageOps

# Windows下隐藏命令行窗口
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_STARTUP_INFO = None
if sys.platform == "win32":
    _STARTUP_INFO = subprocess.STARTUPINFO()
    _STARTUP_INFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUP_INFO.wShowWindow = 0  # SW_HIDE

# 优先用 imageio-ffmpeg 自带的 ffmpeg（打包进exe后无需系统安装）
try:
    import imageio_ffmpeg
    _BUNDLED_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    # 未安装，或 imageio-ffmpeg 找不到可用二进制时，都不应让程序启动失败
    _BUNDLED_FFMPEG = None


def _get_ffmpeg() -> str | None:
    """获取 ffmpeg 可执行文件路径：优先用内置的，找不到则用系统的"""
    if _BUNDLED_FFMPEG and os.path.exists(_BUNDLED_FFMPEG):
        return _BUNDLED_FFMPEG
    sys_ffmpeg = shutil.which("ffmpeg")
    return sys_ffmpeg


# ============ 默认配置 ============
DEFAULT_TARGET_WIDTH = 600          # 默认目标宽度px
DEFAULT_MAX_MB = 1.0                # 默认最大文件MB
DEFAULT_OUTPUT_FORMAT = "auto"      # auto / jpeg / webp / png
DEFAULT_SKIP_SMALL_GIF = True       # 勾选时：GIF宽度和体积都达标就跳过压缩，直接复制

MIN_TARGET_WIDTH = 10               # 宽度输入下限（防止放大到不合理）
MAX_TARGET_WIDTH = 10000            # 宽度输入上限（防止内存爆炸）
MAX_MAX_MB = 2048                   # 最大文件大小上限（2GB，防止误输入/溢出）

MAX_LOG_LINES = 2000                # 日志最大行数，防止内存无限增长

FFMPEG_TIMEOUT = 60                 # ffmpeg 单次调用超时（秒），防止异常GIF卡死

# 质量优先阶梯（在大小上限内，先试最高质量，逐步让步）
JPEG_QUALITY_START = 95
JPEG_QUALITY_STEP = 5
JPEG_QUALITY_FLOOR = 75             # auto 模式下 JPEG 不低于75
JPEG_QUALITY_HARD_FLOOR = 30        # 强制JPEG时可继续降到30
WEBP_QUALITY_START = 90
WEBP_QUALITY_STEP = 5
WEBP_QUALITY_FLOOR = 70
WEBP_QUALITY_ULTRA_FLOOR = 40       # 终极兜底，尽量压到上限以内

# ================================


def _has_alpha(img: Image.Image) -> bool:
    """判断图片是否带透明通道且存在实际透明像素"""
    try:
        if img.mode == "RGBA":
            alpha = img.getchannel("A")
            lo, hi = alpha.getextrema()
            return lo < 255
        if img.mode in ("LA", "PA"):
            alpha = img.getchannel("A")
            lo, hi = alpha.getextrema()
            return lo < 255
        if img.mode == "P" and "transparency" in img.info:
            return True
    except Exception:
        pass
    return False


def _best_buf_under(img: Image.Image, fmt: str, max_size_bytes: int,
                    q_lo: int, q_hi: int, **kwargs):
    """
    二分查找：在 [q_lo, q_hi] 内找满足 <= max_size_bytes 的【最大】quality，
    返回 (BytesIO, quality)；找不到返回 None。
    文件大小随 quality 单调，故二分减少编码次数（提速）。
    """
    lo, hi = q_lo, q_hi
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        buf = io.BytesIO()
        img.save(buf, format=fmt, quality=mid, **kwargs)
        if buf.tell() <= max_size_bytes:
            best = (buf, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _write_buf(buf: io.BytesIO, output_path: str) -> None:
    with open(output_path, "wb") as f:
        f.write(buf.getvalue())


def _gif_loop_args(loop_val) -> list:
    """把 Pillow 的 loop 值映射成 ffmpeg gif 封装器的 -loop 参数"""
    if loop_val is None:
        return ["-loop", "-1"]      # 原图没有循环扩展名 → 只播放一次
    if loop_val == 0:
        return ["-loop", "0"]       # 0 表示无限循环
    return ["-loop", str(int(loop_val))]


def _terminate_proc(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_ffmpeg(cmd: list, stop_event) -> bool:
    """运行一次 ffmpeg：支持超时和“停止”中断，返回是否成功"""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_STARTUP_INFO,
        )
    except Exception:
        return False
    deadline = time.time() + FFMPEG_TIMEOUT
    while True:
        if stop_event is not None and stop_event.is_set():
            _terminate_proc(proc)
            return False
        code = proc.poll()
        if code is not None:
            return code == 0
        if time.time() > deadline:
            _terminate_proc(proc)
            return False
        time.sleep(0.05)


def compress_static_image(img: Image.Image, out_base: str, target_width: int,
                          max_size_bytes: int, src_ext: str,
                          output_format: str = DEFAULT_OUTPUT_FORMAT) -> tuple[int, int, int, str]:
    """
    压缩静图（质量优先）：
      output_format:
        auto → 智能保留（默认）：无损优先，不达标再转有损
        jpeg → 全部转 JPEG（透明垫白底，可降到quality30）
        webp → 全部转 WebP（保留透明）
        png  → 全部转 PNG（无损；超限降为调色板PNG）
    返回: (原始宽, 输出宽, 输出高, 实际输出扩展名)
    """
    # 修复EXIF方向：手机竖图先按拍摄方向转正，避免输出横躺
    # exif_transpose 会强制完整解码，只有方向正常(1)的图才值得先 draft 省内存
    orientation = 1
    try:
        orientation = img.getexif().get(0x0112, 1) or 1
    except Exception:
        pass
    if orientation == 1:
        orig_w, orig_h = img.size
        if img.format == "JPEG" and orig_w > target_width:
            try:
                img.draft("RGB", (target_width,
                                  max(1, int(orig_h * target_width / orig_w))))
            except Exception:
                pass
    else:
        img = ImageOps.exif_transpose(img)
        orig_w, orig_h = img.size

    # 不放大：目标宽度大于原图宽度时保持原宽，避免小图被放大后体积更大、画质更差
    out_w = min(target_width, orig_w)
    out_h = max(1, int(orig_h * out_w / orig_w))
    if (img.width, img.height) != (out_w, out_h):
        img = img.resize((out_w, out_h), Image.Resampling.LANCZOS)
    has_alpha = _has_alpha(img)

    # ---------- 模式：全部转 PNG ----------
    if output_format == "png":
        if has_alpha:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        if buf.tell() <= max_size_bytes:
            _write_buf(buf, out_base + ".png")
            return orig_w, img.width, img.height, ".png"
        # 无损超限：降为调色板PNG（P模式）尝试
        # 透明图用 FASTOCTREE 量化，调色板自带 alpha，保存PNG时会写入 tRNS，
        # 不会像普通 convert("P") 那样把透明区域变成黑底
        if has_alpha:
            p_img = img.convert("RGBA").quantize(
                colors=256, method=Image.Quantize.FASTOCTREE)
        else:
            p_img = img.convert("P", palette=Image.Quantize.MEDIANCUT, colors=256)
        buf = io.BytesIO()
        p_img.save(buf, format="PNG", optimize=True)
        if buf.tell() <= max_size_bytes:
            _write_buf(buf, out_base + ".png")
            return orig_w, img.width, img.height, ".png"
        # 仍超限：退回无损PNG（体积可能略超限，日志会显示大小）
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _write_buf(buf, out_base + ".png")
        return orig_w, img.width, img.height, ".png"

    # ---------- 模式：全部转 JPEG（透明垫白底） ----------
    if output_format == "jpeg":
        if has_alpha:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img).convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        best = _best_buf_under(img, "JPEG", max_size_bytes,
                               JPEG_QUALITY_HARD_FLOOR, JPEG_QUALITY_START,
                               optimize=True, progressive=True)
        if best:
            _write_buf(best[0], out_base + ".jpg")
            return orig_w, img.width, img.height, ".jpg"
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY_HARD_FLOOR, optimize=True)
        _write_buf(buf, out_base + ".jpg")
        return orig_w, img.width, img.height, ".jpg"

    # ---------- 模式：全部转 WebP ----------
    if output_format == "webp":
        if has_alpha:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        best = _best_buf_under(img, "WEBP", max_size_bytes,
                               WEBP_QUALITY_FLOOR, WEBP_QUALITY_START, method=4)
        if best:
            _write_buf(best[0], out_base + ".webp")
            return orig_w, img.width, img.height, ".webp"
        best = _best_buf_under(img, "WEBP", max_size_bytes,
                               WEBP_QUALITY_ULTRA_FLOOR, WEBP_QUALITY_FLOOR - WEBP_QUALITY_STEP,
                               method=4)
        if best:
            _write_buf(best[0], out_base + ".webp")
            return orig_w, img.width, img.height, ".webp"
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=WEBP_QUALITY_ULTRA_FLOOR, method=4)
        _write_buf(buf, out_base + ".webp")
        return orig_w, img.width, img.height, ".webp"

    # ---------- 模式 auto：智能保留（默认） ----------
    # 透明图：先试无损PNG（质量100%且保留透明），不达标再 WebP 有损
    if has_alpha:
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        if buf.tell() <= max_size_bytes:
            _write_buf(buf, out_base + ".png")
            return orig_w, img.width, img.height, ".png"
        best = _best_buf_under(img, "WEBP", max_size_bytes,
                               WEBP_QUALITY_FLOOR, WEBP_QUALITY_START, method=4)
        if best:
            _write_buf(best[0], out_base + ".webp")
            return orig_w, img.width, img.height, ".webp"
        best = _best_buf_under(img, "WEBP", max_size_bytes,
                               WEBP_QUALITY_ULTRA_FLOOR, WEBP_QUALITY_FLOOR - WEBP_QUALITY_STEP,
                               method=4)
        if best:
            _write_buf(best[0], out_base + ".webp")
            return orig_w, img.width, img.height, ".webp"
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=WEBP_QUALITY_ULTRA_FLOOR, method=4)
        _write_buf(buf, out_base + ".webp")
        return orig_w, img.width, img.height, ".webp"

    # 不透明：优先无损保留原格式
    if img.mode != "RGB":
        img = img.convert("RGB")
    if src_ext == ".png":
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        if buf.tell() <= max_size_bytes:
            _write_buf(buf, out_base + ".png")
            return orig_w, img.width, img.height, ".png"
    elif src_ext == ".webp":
        buf = io.BytesIO()
        img.save(buf, format="WEBP", lossless=True, method=4)
        if buf.tell() <= max_size_bytes:
            _write_buf(buf, out_base + ".webp")
            return orig_w, img.width, img.height, ".webp"
    # JPEG/BMP/TIFF 不做无损尝试（无损无法有效压体积），直接走有损

    # JPEG（兼容性最好）
    best = _best_buf_under(img, "JPEG", max_size_bytes,
                           JPEG_QUALITY_FLOOR, JPEG_QUALITY_START,
                           optimize=True, progressive=True)
    if best:
        _write_buf(best[0], out_base + ".jpg")
        return orig_w, img.width, img.height, ".jpg"

    # WebP 有损兜底（同视觉质量下文件更小），尽量压到上限以内
    best = _best_buf_under(img, "WEBP", max_size_bytes,
                           WEBP_QUALITY_FLOOR, WEBP_QUALITY_START, method=4)
    if best:
        _write_buf(best[0], out_base + ".webp")
        return orig_w, img.width, img.height, ".webp"
    best = _best_buf_under(img, "WEBP", max_size_bytes,
                           WEBP_QUALITY_ULTRA_FLOOR, WEBP_QUALITY_FLOOR - WEBP_QUALITY_STEP,
                           method=4)
    if best:
        _write_buf(best[0], out_base + ".webp")
        return orig_w, img.width, img.height, ".webp"
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY_ULTRA_FLOOR, method=4)
    _write_buf(buf, out_base + ".webp")
    return orig_w, img.width, img.height, ".webp"


def compress_gif(img: Image.Image, output_path: str, input_path: str = None,
                 target_width: int = 600, max_size_bytes: int = 1024 * 1024,
                 stop_event: threading.Event = None) -> tuple[int, int, int] | None:
    """
    压缩GIF（质量优先）：等比缩放，在大小上限内优先保留原帧率和高颜色数
    优先用 ffmpeg（效果更好），ffmpeg 缺失或失败时自动降级到 Pillow 兜底
    stop_event：置位时中止处理
    返回: (原始宽, 输出宽, 输出高)；被停止时返回 None
    """
    orig_w, orig_h = img.size
    # 不放大：目标宽度大于原宽时保持原宽
    target_w = min(target_width, orig_w)
    target_h = max(1, int(orig_h * target_w / orig_w))

    # ---- 方案1：用 ffmpeg 压缩（效果更好） ----
    ffmpeg_path = _get_ffmpeg()
    if ffmpeg_path and input_path:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                palette = os.path.join(tmpdir, "palette.png")
                out_tmp = os.path.join(tmpdir, "out.gif")
                # 质量优先：帧率从高到低、颜色从多到少逐步让步，fps 不低于 10
                fps_list = [None, 15, 12, 10]
                color_list = [128, 96, 64, 48, 32, 24]
                dither = "bayer:bayer_scale=5"   # bayer 抖动减轻色带
                loop_args = _gif_loop_args(img.info.get("loop"))  # 保留原图循环次数
                for fps in fps_list:
                    for colors in color_list:
                        if stop_event is not None and stop_event.is_set():
                            return None
                        vf_scale = f"scale={target_w}:-1:flags=lanczos"
                        vf_palette = vf_scale
                        if fps:
                            vf_palette += f",fps={fps}"
                        vf_palette += f",palettegen=stats_mode=diff:max_colors={colors}"
                        cmd_pal = [
                            ffmpeg_path, "-y", "-i", input_path,
                            "-vf", vf_palette, palette
                        ]
                        if not _run_ffmpeg(cmd_pal, stop_event):
                            raise RuntimeError("ffmpeg 调色板生成失败或超时")
                        vf_use = vf_scale
                        if fps:
                            vf_use += f",fps={fps}"
                        cmd_gif = [
                            ffmpeg_path, "-y", "-i", input_path, "-i", palette,
                            "-lavfi", f"[0:v]{vf_use}[x]; [x][1:v] paletteuse=dither={dither}",
                            *loop_args,
                            out_tmp
                        ]
                        if not _run_ffmpeg(cmd_gif, stop_event):
                            raise RuntimeError("ffmpeg 编码失败或超时")
                        if os.path.getsize(out_tmp) <= max_size_bytes:
                            shutil.copy(out_tmp, output_path)
                            return orig_w, target_w, target_h
                shutil.copy(out_tmp, output_path)
                return orig_w, target_w, target_h
        except Exception:
            if stop_event is not None and stop_event.is_set():
                return None
            pass  # ffmpeg 失败 → 降级到 Pillow 兜底

    # ---- 方案2：Pillow 兜底 ----
    # 用源 GIF 的全局调色板作为目标调色板：比 WEB 安全色更接近原色，体积也更小
    src_palette_img = None
    try:
        palette_data = img.getpalette()
        if palette_data and len(palette_data) >= 48:   # 至少 16 色
            src_palette_img = Image.new("P", (1, 1))
            src_palette_img.putpalette(palette_data)
    except Exception:
        src_palette_img = None

    frames_rgb = []
    durations = []
    loop_val = img.info.get("loop")      # 没有循环信息时保持“只播一次”，不强行无限循环
    for frame in ImageSequence.Iterator(img):
        if stop_event is not None and stop_event.is_set():
            return None
        dur = frame.info.get("duration", 100)
        if not isinstance(dur, (int, float)) or dur <= 0:
            dur = 100
        # 先转 RGB 再缩放：P 模式直接 resize 不会真正插值，边缘会变成硬边
        frame_resized = frame.convert("RGB").resize(
            (target_w, target_h), Image.Resampling.LANCZOS)
        frames_rgb.append(frame_resized)
        durations.append(int(dur))

    if not frames_rgb:
        raise ValueError("GIF 中没有可读取的帧")

    avg_ms = sum(durations) / len(durations)
    src_fps = 1000.0 / avg_ms if avg_ms > 0 else 10.0

    def encode_gif(sub_frames, sub_durations, dither):
        if src_palette_img is not None:
            p_frames = [f.quantize(palette=src_palette_img, dither=dither)
                        for f in sub_frames]
        else:
            p_frames = [f.convert("P", dither=dither) for f in sub_frames]
        buf = io.BytesIO()
        save_kwargs = dict(
            format="GIF",
            save_all=True,
            append_images=p_frames[1:],
            duration=sub_durations,
            optimize=True,
        )
        if loop_val is not None:
            save_kwargs["loop"] = loop_val
        p_frames[0].save(buf, **save_kwargs)
        return buf

    def subsample(step):
        """按 step 抽帧，被丢弃帧的时长并入前一帧，保持总时长不变"""
        if step <= 1:
            return frames_rgb, durations
        out_frames, out_durations = [], []
        for i, (frame, dur) in enumerate(zip(frames_rgb, durations)):
            if i % step == 0:
                out_frames.append(frame)
                out_durations.append(dur)
            else:
                out_durations[-1] += dur
        return out_frames, out_durations

    # 画质从高到低尝试。注意：抖动会明显增大体积（同一素材实测约 5 倍），
    # 所以体积超标时优先去掉抖动；仍超标再抽帧（最低 8fps）
    attempts = [(1, Image.Dither.FLOYDSTEINBERG), (1, Image.Dither.NONE)]
    seen_steps = {1}
    for fps in (15, 12, 10, 8):
        step = max(1, int(round(src_fps / fps)))
        if step > 1 and step not in seen_steps:
            seen_steps.add(step)
            attempts.append((step, Image.Dither.NONE))

    smallest = None
    for step, dither in attempts:
        if stop_event is not None and stop_event.is_set():
            return None
        sub_frames, sub_durations = subsample(step)
        buf = encode_gif(sub_frames, sub_durations, dither)
        if smallest is None or buf.tell() < smallest.tell():
            smallest = buf
        if buf.tell() <= max_size_bytes:
            _write_buf(buf, output_path)
            return orig_w, target_w, target_h

    # 所有方案都超限：写出体积最小的结果（process_folder 会在日志里提示超限）
    _write_buf(smallest, output_path)
    return orig_w, target_w, target_h


def process_folder(input_dir: str, output_dir: str, target_width: int,
                   max_size_bytes: int, progress_cb, log_cb,
                   output_format: str = DEFAULT_OUTPUT_FORMAT,
                   stop_event: threading.Event = None,
                   skip_small_gif: bool = DEFAULT_SKIP_SMALL_GIF) -> dict:
    """
    批量处理文件夹
    progress_cb(done, total, filename)
    log_cb(msg)
    stop_event：置位时提前终止处理
    skip_small_gif：为 True 时，GIF 宽度 ≤ target_width 且体积 ≤ max_size_bytes
                    就直接复制；为 False 时所有 GIF 都重新压缩
    返回统计: {"success": n, "copy": n, "fail": n, "stopped": bool}
    """
    os.makedirs(output_dir, exist_ok=True)
    exts_static = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
    exts_gif = {".gif"}
    files = []
    for fname in os.listdir(input_dir):
        ext = os.path.splitext(fname)[1].lower()
        if ext in exts_static or ext in exts_gif:
            files.append(fname)

    # 同名不同扩展（如 a.png / a.jpg）会映射到同一个输出名而互相覆盖，
    # 这类文件在输出名里带上原扩展名区分：a_compressed_png.png / a_compressed_jpg.jpg
    base_counts = {}
    for fname in files:
        base = os.path.splitext(fname)[0].lower()
        base_counts[base] = base_counts.get(base, 0) + 1

    total = len(files)
    stats = {"success": 0, "copy": 0, "fail": 0, "stopped": False}
    fmt_name = {"auto": "自动(智能保留)", "jpeg": "全部JPEG", "webp": "全部WebP", "png": "全部PNG"}
    log_cb(f"输出格式偏好：{fmt_name.get(output_format, output_format)}")

    for i, fname in enumerate(files):
        # 停止检查（修复：停止按钮真正生效）
        if stop_event is not None and stop_event.is_set():
            stats["stopped"] = True
            log_cb("⏹ 已停止：剩余文件未处理")
            break

        ext = os.path.splitext(fname)[1].lower()
        in_path = os.path.join(input_dir, fname)
        base = os.path.splitext(fname)[0]
        if base_counts.get(base.lower(), 0) > 1:
            out_base = os.path.join(output_dir, f"{base}_compressed_{ext.lstrip('.')}")
        else:
            out_base = os.path.join(output_dir, f"{base}_compressed")

        progress_cb(i, total, fname)
        log_cb(f"处理中: {fname}")
        try:
            file_size = os.path.getsize(in_path)
            # 修复：with 管理句柄，处理完自动关闭，避免句柄泄漏
            with Image.open(in_path) as img:
                orig_w, orig_h = img.size

                # 静图：宽度已达标且未超限 → 直接复制原图，零损耗
                # 强制 JPEG/WebP/PNG 时不走复制，保证输出格式统一
                # GIF 统一交给下面的“GIF跳压缩”开关决定，不在通用规则里复制
                can_copy = orig_w <= target_width and file_size <= max_size_bytes
                if can_copy and output_format == "auto" and ext not in exts_gif:
                    out_path = out_base + ext
                    shutil.copy2(in_path, out_path)
                    stats["copy"] += 1
                    log_cb(f"  📋 {fname} 复制原图：原图宽{orig_w}px ≤ {target_width}px，"
                           f"文件{file_size / 1024:.0f}KB ≤{max_size_bytes / 1024:.0f}KB")
                    continue

                # GIF：开关勾选且宽度、体积都达标 → 跳过压缩，直接复制
                if (ext in exts_gif and skip_small_gif
                        and orig_w <= target_width
                        and file_size <= max_size_bytes):
                    out_path = out_base + ".gif"
                    shutil.copy2(in_path, out_path)
                    stats["copy"] += 1
                    log_cb(f"  🎞 {fname} 复制原图：GIF {orig_w}px/"
                           f"{file_size / 1024:.0f}KB ≤ {target_width}px/"
                           f"{max_size_bytes / 1024:.0f}KB，按设置跳过压缩")
                    continue

                # 需要压缩
                if ext in exts_gif:
                    if orig_w <= target_width:
                        log_cb(f"  ℹ {fname} 宽度{orig_w}px 已≤目标{target_width}px，"
                               f"不放大，只压缩体积")
                    out_path = out_base + ".gif"
                    gif_result = compress_gif(img, out_path, input_path=in_path,
                                              target_width=target_width,
                                              max_size_bytes=max_size_bytes,
                                              stop_event=stop_event)
                    if gif_result is None:
                        stats["stopped"] = True
                        log_cb("⏹ 已停止：当前GIF处理被中断")
                        break
                    ow, nw, nh = gif_result
                else:
                    n_frames = getattr(img, "n_frames", 1)
                    if n_frames > 1:
                        log_cb(f"  ⚠ {fname} 是多帧图片（{n_frames} 帧），当前只处理第一帧")
                    ow, nw, nh, out_ext = compress_static_image(
                        img, out_base, target_width, max_size_bytes, ext, output_format)
                    out_path = out_base + out_ext
            # with 结束，img 已关闭
            stats["success"] += 1
            out_size = os.path.getsize(out_path)
            size_kb = out_size / 1024
            out_fmt = os.path.splitext(out_path)[1]
            log_cb(f"  ✓ {fname}  {ow}px → {nw}×{nh} → {out_fmt}  ({size_kb:.0f}KB)")
            if out_size > max_size_bytes:
                log_cb(f"  ⚠ {fname} 无法压到 {max_size_bytes / 1024:.0f}KB 以内，"
                       f"已输出最小结果 {size_kb:.0f}KB")
            elif ext in exts_gif and out_size >= file_size:
                log_cb(f"  ⚠ {fname} 压缩后体积不小于原图（原图 {file_size / 1024:.0f}KB）")
        except Exception as e:
            stats["fail"] += 1
            log_cb(f"  ✗ {fname}  失败: {e}")
    if stats["stopped"]:
        # 停止时不要把进度条拉满，显示实际处理到的位置
        progress_cb(min(i + 1, total) if files else 0, total, "已停止")
    else:
        progress_cb(total, total, "完成")
    return stats


# ============ UI 界面 ============
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("批量图片/GIF压缩工具（智能保留版）")
        self.geometry("680x660")
        self.resizable(False, False)
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.target_width_var = tk.StringVar(value=str(DEFAULT_TARGET_WIDTH))
        self.max_mb_var = tk.StringVar(value=str(DEFAULT_MAX_MB))
        self.output_format_var = tk.StringVar(value=DEFAULT_OUTPUT_FORMAT)
        self.skip_small_gif_var = tk.BooleanVar(value=DEFAULT_SKIP_SMALL_GIF)
        self.running = False
        self._stop_event = threading.Event()
        self._last_progress_t = 0.0   # 进度节流时间戳
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 5}
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        # Tab1：压缩设置
        tab_main = ttk.Frame(self.notebook)
        self.notebook.add(tab_main, text="压缩设置")

        # Tab2：作者信息
        tab_about = ttk.Frame(self.notebook)
        self.notebook.add(tab_about, text="作者")
        self._build_about_tab(tab_about)

        # 输入目录
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="输入文件夹:", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.input_dir, state="readonly").pack(
            side="left", fill="x", expand=True, padx=5
        )
        ttk.Button(row, text="浏览…", command=self._pick_input).pack(side="right")

        # 输出目录
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="输出文件夹:", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.output_dir, state="readonly").pack(
            side="left", fill="x", expand=True, padx=5
        )
        ttk.Button(row, text="浏览…", command=self._pick_output).pack(side="right")

        # 目标宽度
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="目标宽度(px):", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self.target_width_var, width=10).pack(side="left", padx=5)
        ttk.Label(row, text=f"限 {MIN_TARGET_WIDTH}~{MAX_TARGET_WIDTH}").pack(side="left", padx=10)

        # 最大文件大小
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="最大文件大小(MB):", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self.max_mb_var, width=10).pack(side="left", padx=5)
        ttk.Label(row, text="例如：1.0 代表1MB").pack(side="left", padx=10)

        # 输出格式偏好
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="输出格式:", width=14).pack(side="left")
        cb = ttk.Combobox(
            row, textvariable=self.output_format_var, state="readonly", width=16,
            values=["auto", "jpeg", "webp", "png"],
        )
        cb.pack(side="left", padx=5)
        ttk.Label(row, text="auto=智能保留 · 仅作用于静图").pack(side="left", padx=10)

        # GIF：宽度和体积都达标时跳过压缩（由开关控制）
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="GIF跳压缩:", width=14).pack(side="left")
        ttk.Checkbutton(
            row,
            text="GIF宽度≤目标且体积≤上限时直接复制（取消勾选则全部压缩）",
            variable=self.skip_small_gif_var,
        ).pack(side="left", padx=5)

        # 参数说明
        row = ttk.LabelFrame(tab_main, text="处理规则")
        row.pack(fill="x", **pad)
        info = (
            f"• 图片按设置宽度缩放，高度等比例\n"
            f"• 输出文件不超过设置的最大文件大小\n"
            f"• 宽度≤目标宽度且未超限：直接复制原图\n"
            f"• 勾选GIF跳压缩：GIF宽度≤目标且体积≤上限就复制；取消勾选则全部压缩\n"
            f"• auto：PNG/WebP 无损优先，透明图保留透明；无损不达标再转JPEG/WebP\n"
            f"• 强制JPEG：透明图垫白底；强制PNG：超限降为调色板PNG\n"
            f"• GIF动画不受格式偏好影响，始终保持GIF\n"
            f"• 支持格式：JPG / PNG / BMP / WebP / GIF"
        )
        ttk.Label(row, text=info, justify="left").pack(padx=10, pady=8)

        # 开始/停止
        row = ttk.Frame(tab_main)
        row.pack(fill="x", **pad)
        self.btn_start = ttk.Button(row, text="开始处理", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="停止", state="disabled", command=self._stop)
        self.btn_stop.pack(side="left", padx=5)

        # 进度条
        self.progress = ttk.Progressbar(tab_main, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(tab_main, textvariable=self.status_var).pack(anchor="w", padx=10)

        # 日志
        row = ttk.LabelFrame(tab_main, text="处理日志")
        row.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(row, height=10, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(row, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)
        scroll.pack(side="right", fill="y", pady=5)

    def _build_about_tab(self, parent):
        content = """
批量图片/GIF压缩工具（智能保留版）

作者：青阳君

智能格式策略：
• 宽度已达标且未超限 → 复制原图，零损耗
• 勾选“GIF跳压缩” → GIF宽度和体积都达标就复制；取消勾选则全部压缩
• PNG/WebP → 先尝试无损保留原格式，质量100%
• 无损无法达标 → 转 JPEG（兼容最好），仍超限再转 WebP
• 透明图 → 先试无损PNG，否则 WebP（保留透明）
• BMP/TIFF 无压缩或压缩率差，直接转 JPEG/WebP
• 可在"输出格式"下拉框强制统一为 JPEG / WebP / PNG

稳定性：
• 停止按钮即时生效，支持中断大批量处理
• 图片句柄自动关闭，避免批量处理句柄耗尽
• UI 更新回主线程调度，避免跨线程崩溃
• 输出目录与输入目录相同会拒绝执行

依赖库：
Pillow、imageio‑ffmpeg
        """
        txt = tk.Text(parent, wrap="word")
        txt.pack(fill="both", expand=True, padx=15, pady=15)
        txt.insert("end", content)
        txt.configure(state="disabled")

    def _pick_input(self):
        d = filedialog.askdirectory(title="选择输入文件夹")
        if d:
            self.input_dir.set(d)
            self.output_dir.set(os.path.join(d, "compressed_output"))

    def _pick_output(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.output_dir.set(d)

    def _log(self, msg: str):
        """线程安全：把 UI 更新调度回主线程，并限制日志行数"""
        def _do():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            # 限制日志行数，防止内存无限增长
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES:
                remove = line_count - MAX_LOG_LINES
                self.log_text.delete("1.0", f"{remove + 1}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(0, _do)

    def _progress(self, done: int, total: int, fname: str):
        """线程安全：进度更新调度回主线程；50ms内合并，降低UI事件堆积"""
        now = time.time()
        if done != total and now - self._last_progress_t < 0.05:
            return
        self._last_progress_t = now
        def _do():
            if total > 0:
                self.progress["maximum"] = total
                self.progress["value"] = done
            self.status_var.set(f"{done}/{total}  {fname}")
        self.after(0, _do)

    def _start(self):
        if self.running:
            return
        inp = self.input_dir.get()
        out = self.output_dir.get()
        if not inp or not os.path.isdir(inp):
            messagebox.showwarning("提示", "请选择有效的输入文件夹")
            return
        if not out:
            messagebox.showwarning("提示", "请选择输出文件夹")
            return
        # 修复：输入与输出相同会覆盖/累积文件，直接拒绝（Windows 下大小写不敏感）
        try:
            same_dir = os.path.samefile(inp, out)
        except OSError:
            same_dir = (os.path.normcase(os.path.abspath(out))
                        == os.path.normcase(os.path.abspath(inp)))
        if same_dir:
            messagebox.showwarning("提示", "输出文件夹不能与输入文件夹相同，"
                                           "否则会覆盖原图或无限累积 _compressed 文件。")
            return
        # 校验宽度（修复：限制范围防止放大内存爆炸）
        try:
            tw = int(self.target_width_var.get().strip())
            if tw < MIN_TARGET_WIDTH or tw > MAX_TARGET_WIDTH:
                raise ValueError()
        except ValueError:
            messagebox.showwarning(
                "参数错误",
                f"目标宽度需在 {MIN_TARGET_WIDTH}~{MAX_TARGET_WIDTH}px 之间，使用默认 {DEFAULT_TARGET_WIDTH} px")
            tw = DEFAULT_TARGET_WIDTH
            self.target_width_var.set(str(DEFAULT_TARGET_WIDTH))
        # 校验最大MB（nan/inf 也必须拒绝，否则 int() 会抛异常）
        try:
            max_mb = float(self.max_mb_var.get().strip())
            if not math.isfinite(max_mb) or not (0.01 <= max_mb <= MAX_MAX_MB):
                raise ValueError()
        except ValueError:
            messagebox.showwarning(
                "参数错误",
                f"最大文件大小需在 0.01~{MAX_MAX_MB} MB 之间，使用默认 {DEFAULT_MAX_MB} MB")
            max_mb = DEFAULT_MAX_MB
            self.max_mb_var.set(str(DEFAULT_MAX_MB))
        max_size_bytes = int(max_mb * 1024 * 1024)

        fmt = self.output_format_var.get().strip().lower()
        if fmt not in ("auto", "jpeg", "webp", "png"):
            fmt = DEFAULT_OUTPUT_FORMAT
            self.output_format_var.set(DEFAULT_OUTPUT_FORMAT)

        skip_small_gif = bool(self.skip_small_gif_var.get())

        self.running = True
        self._stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        t = threading.Thread(target=self._worker,
                             args=(inp, out, tw, max_size_bytes, fmt, skip_small_gif),
                             daemon=True)
        t.start()

    def _stop(self):
        self.running = False
        self._stop_event.set()
        self._log("⏹ 用户请求停止，正在退出…")

    def _worker(self, inp: str, out: str, target_width: int,
                max_size_bytes: int, output_format: str = DEFAULT_OUTPUT_FORMAT,
                skip_small_gif: bool = False):
        try:
            stats = process_folder(inp, out, target_width, max_size_bytes,
                                   self._progress, self._log, output_format,
                                   self._stop_event, skip_small_gif)
            self.after(0, lambda: self._done(stats))
        except Exception as err:
            # except 结束时 err 会被删除，必须先转成字符串再交给延迟回调
            message = f"{type(err).__name__}: {err}"
            self.after(0, lambda: self._failed(message))

    def _failed(self, message: str):
        """后台线程异常：恢复按钮并提示，避免界面卡死"""
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("出错")
        messagebox.showerror("错误", f"处理失败：\n{message}")

    def _done(self, stats: dict):
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        stopped_txt = "\n已提前停止（部分文件未处理）" if stats.get("stopped") else ""
        msg = (
            f"处理完成！\n\n"
            f"成功压缩: {stats['success']} 个\n"
            f"直接复制原图: {stats['copy']} 个\n"
            f"失败: {stats['fail']} 个\n"
            f"输出目录: {self.output_dir.get()}"
            f"{stopped_txt}"
        )
        messagebox.showinfo("完成", msg)
        self.status_var.set("完成")


if __name__ == "__main__":
    app = App()
    app.mainloop()
