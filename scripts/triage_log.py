"""把用户发来的错误日志变成排查起点（给维护者 / Claude 用，v0.10.44）。

支持：crash_*.log（未捕获异常）、gui_stall_*.log（界面卡死）、boot_fault.log（原生崩溃，faulthandler）、
startup_failure_*.log（启动失败）。输出：
  1. 日志来自哪个版本；**若比当前代码旧**，列出之后改过相关函数的提交——先确认是否已修复，不要重复修；
  2. 本项目代码里的调用链（第三方库帧略去），按**函数名**定位到当前源码行号（行号会随版本漂移）；
  3. 下一步清单（复现测试 → 修根因 → 同类排查 → 全量回归）。

用法：
  python scripts/triage_log.py <日志文件>            # 打印 Markdown 报告
  python scripts/triage_log.py - < 日志.txt          # 从标准输入读（直接粘贴的日志）
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 'File "x", line 12, in func'（traceback）与 'File "x", line 12 in func'（faulthandler）两种写法
_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+),? in (?P<func>\S+)')
_VERSION_RE = re.compile(r"^\s*Version:\s*v?(?P<v>[0-9][0-9A-Za-z.\-]*)", re.MULTILINE | re.IGNORECASE)
_CONTEXT_RE = re.compile(r"^\s*Context:\s*(?P<c>.+)$", re.MULTILINE | re.IGNORECASE)
_TIME_RE = re.compile(r"^\s*Time:\s*(?P<t>\S+)", re.MULTILINE | re.IGNORECASE)
_EXC_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Expired)):\s?(?P<msg>.*)$", re.MULTILINE)
_NATIVE_RE = re.compile(r"^(?:Windows )?[Ff]atal (?:exception|Python error):\s*(?P<msg>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Frame:
    file: str      # 规范化为 specimen_app/xxx.py（本项目帧）或原样（第三方）
    line: int
    func: str

    @property
    def is_app(self) -> bool:
        return self.file.startswith("specimen_app/") or self.file in ("run_app.py",)


@dataclass
class LogInfo:
    kind: str = "unknown"
    version: str = ""
    time: str = ""
    context: str = ""
    exception_type: str = ""
    exception_message: str = ""
    frames: list[Frame] = field(default_factory=list)

    @property
    def app_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.is_app]


@dataclass
class Located:
    frame: Frame
    current_line: int | None


def _normalize(path: str) -> str:
    p = path.replace("\\", "/")
    i = p.rfind("specimen_app/")
    if i >= 0:
        return p[i:]
    return p.rsplit("/", 1)[-1] if p.endswith("run_app.py") else p


def parse_log(text: str) -> LogInfo:
    info = LogInfo()
    if "GUI 线程停摆" in text or "context: gui_stall" in text:
        info.kind = "gui_stall"
    elif _NATIVE_RE.search(text):
        info.kind = "native_crash"
    elif "crash report" in text:
        info.kind = "crash"
    elif "Executable:" in text and "Traceback" in text:
        info.kind = "startup_failure"
    if m := _VERSION_RE.search(text):
        info.version = m.group("v")
    if m := _TIME_RE.search(text):
        info.time = m.group("t")
    if m := _CONTEXT_RE.search(text):
        info.context = m.group("c").strip()
    if info.kind == "native_crash":
        m = _NATIVE_RE.search(text)
        info.exception_type = "NativeCrash"
        info.exception_message = m.group("msg").strip() if m else ""
    else:
        excs = list(_EXC_RE.finditer(text))
        if excs:
            info.exception_type, info.exception_message = excs[-1].group("type"), excs[-1].group("msg").strip()
    # faulthandler 每个线程一段，"most recent call first"；只取第一段（出事的线程）并按 traceback 顺序（最外层在前）
    if info.kind == "native_crash":
        first_block = re.split(r"\n\s*\n", text.split("(most recent call first):", 1)[-1].lstrip("\n"), 1)[0]
        frames = [Frame(_normalize(m.group("file")), int(m.group("line")), m.group("func"))
                  for m in _FRAME_RE.finditer(first_block)]
        frames.reverse()
        # 反转后最内层在最后；app_frames[0] 应是出事位置 → 再按"最内层优先"给出
        info.frames = list(reversed(frames))
    else:
        info.frames = [Frame(_normalize(m.group("file")), int(m.group("line")), m.group("func"))
                       for m in _FRAME_RE.finditer(text)]
    return info


def locate_in_source(frames: list[Frame], root: Path = ROOT) -> list[Located]:
    """按函数名在当前源码里找定义行（日志里的行号属于旧版本，会漂移）。"""
    out: list[Located] = []
    cache: dict[str, list[str]] = {}
    for fr in frames:
        line_no = None
        if not fr.func.startswith("<"):
            lines = cache.get(fr.file)
            if lines is None:
                try:
                    lines = (root / fr.file).read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                cache[fr.file] = lines
            pat = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(fr.func)}\s*\(")
            candidates = [i for i, text in enumerate(lines, 1) if pat.match(text)]
            if candidates:
                # 同名函数可能有多个（Protocol / 不同类）：取离日志行号最近的（版本间漂移通常不大）
                line_no = min(candidates, key=lambda i: abs(i - fr.line))
        out.append(Located(fr, line_no))
    return out


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)[:3])


def is_older(log_version: str, current: str) -> bool:
    return bool(log_version) and _vtuple(log_version) < _vtuple(current)


def current_version(root: Path = ROOT) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "specimen_app" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def commits_touching(funcs: list[str], files: list[str], since_tag: str, root: Path = ROOT) -> list[str]:
    """since_tag 之后、改动里出现这些函数名的提交（git log -G）。git 不可用 / 无此 tag → 空。"""
    names = sorted({f for f in funcs if f and not f.startswith("<")})
    if not names or not files:
        return []
    try:
        subprocess.run(["git", "rev-parse", "--verify", "--quiet", since_tag], cwd=root, check=True,
                       capture_output=True)
        out = subprocess.run(
            ["git", "log", "--oneline", f"{since_tag}..HEAD", "-G", "|".join(re.escape(n) for n in names),
             "--", *sorted(set(files))],
            cwd=root, capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines() if line.strip()]


def build_report(text: str, root: Path = ROOT) -> str:
    info = parse_log(text)
    cur = current_version(root)
    app = info.app_frames
    located = locate_in_source(app, root)
    kind_name = {"crash": "未捕获异常（闪退）", "gui_stall": "界面卡死", "native_crash": "原生崩溃",
                 "startup_failure": "启动失败"}.get(info.kind, "未知类型")
    lines = [f"# 日志排查：{kind_name}", ""]
    lines.append(f"- 日志版本：v{info.version or '?'}　当前代码：v{cur}　时间：{info.time or '?'}　上下文：{info.context or '?'}")
    if info.exception_type:
        lines.append(f"- 异常：`{info.exception_type}: {info.exception_message}`")
    lines.append("")
    if is_older(info.version, cur):
        hits = commits_touching([f.func for f in app], [f.file for f in app], f"v{info.version}", root)
        lines.append(f"## ⚠ 这是旧版本 v{info.version} 的日志（当前 v{cur}）")
        lines.append("先确认用户实际在用的版本（帮助 → 关于）以及这份日志的时间是不是最近一次出问题；")
        lines.append("下列提交在该版本之后改过调用链上的函数，**可能已经修复**——先读它们，再决定是否需要新修：")
        lines.extend([f"- {h}" for h in hits] or ["- （没有找到改过这些函数的提交 → 问题很可能仍存在）"])
        lines.append("")
    lines.append("## 本项目调用链（外层 → 出事位置）" if info.kind != "native_crash" else "## 出事线程调用链（出事位置在最前）")
    for loc in located:
        cur_line = f"现在在 {loc.frame.file}:{loc.current_line}" if loc.current_line else "（合成帧 / 已改名）"
        lines.append(f"- `{loc.frame.func}` — 日志行 {loc.frame.line}；{cur_line}")
    if info.kind == "gui_stall" and app:
        lines.append(f"\n卡住的位置：`{app[-1].func}`（{app[-1].file}）——主线程在等 IO / 锁 / 大计算。")
    lines += [
        "",
        "## 下一步（docs/fix-from-log.md）",
        "1. 写一个**能复现**的失败测试：走同一条调用链、注入同样的异常 / 条件；先确认它失败。",
        "2. 在**正确的层**修根因（不是只在最外层 try 吞掉）；旧代码按项目规范注释保留。",
        "3. 同类排查：grep 同样的模式（同一类调用 / 同一个异常来源），一并修并补测试。",
        "4. 影响面：列出被改函数的所有调用方，逐个确认行为不变。",
        "5. 全量回归：`python scripts/run_tests.py --isolate`；推分支等 CI（Windows + Linux）全绿才合并发布。",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    text = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    print(build_report(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
