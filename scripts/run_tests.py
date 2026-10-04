"""CI / 本地统一测试入口：全量 unittest + 单用例看门狗。

为什么需要：测试里一旦弹出模态对话框或死锁，unittest 会无限等待——2026-10-04 Windows CI 就这样
挂满 30 分钟，日志里只有两个点，看不出是哪个用例。这里每个用例开始时挂上
faulthandler.dump_traceback_later：超过 PER_TEST_TIMEOUT 秒没结束，就打印所有线程的调用栈并退出（失败），
从而直接定位卡住的位置。

用法：python scripts/run_tests.py [-k 关键字] [--timeout 秒]
"""
from __future__ import annotations

import argparse
import faulthandler
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _WatchdogResult(unittest.TextTestResult):
    timeout = 180.0
    slow: list[tuple[float, str]] = []

    def startTest(self, test):
        self._t0 = time.monotonic()
        sys.stderr.write(f"\n[test] {test.id()}\n")
        sys.stderr.flush()
        faulthandler.dump_traceback_later(self.timeout, exit=True)
        super().startTest(test)

    def addError(self, test, err):
        super().addError(test, err)
        self._print_now("ERROR", test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._print_now("FAIL", test, err)

    def _print_now(self, kind, test, err):
        # 立刻打印：进程若随后崩溃，汇总永远打不出来（2026-10-04 Windows CI 实况）
        sys.stderr.write(f"\n===== {kind}: {test.id()} =====\n{self._exc_info_to_string(err, test)}\n")
        sys.stderr.flush()

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        elapsed = time.monotonic() - self._t0
        if elapsed > 5:
            _WatchdogResult.slow.append((elapsed, test.id()))
        super().stopTest(test)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", dest="pattern", default=None, help="只跑名字包含该关键字的用例")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("PER_TEST_TIMEOUT", "180")))
    parser.add_argument("--module", default=None, help="只跑一个测试模块（如 tests.test_core）")
    parser.add_argument("--isolate", action="store_true",
                        help="每个测试文件一个独立进程：一个文件崩溃 / 卡死不拖垮其他文件，状态也不互相泄漏")
    args = parser.parse_args()
    if args.isolate:
        return _run_isolated(args)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, ROOT)
    _WatchdogResult.timeout = args.timeout
    loader = unittest.TestLoader()
    if args.pattern:
        loader.testNamePatterns = [f"*{args.pattern}*"]
    if args.module:
        suite = loader.loadTestsFromName(args.module)
    else:
        suite = loader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    runner = unittest.TextTestRunner(verbosity=1, resultclass=_WatchdogResult, stream=sys.stdout)
    result = runner.run(suite)
    if _WatchdogResult.slow:
        print("\n最慢的用例：")
        for elapsed, name in sorted(_WatchdogResult.slow, reverse=True)[:15]:
            print(f"  {elapsed:6.1f}s  {name}")
    return 0 if result.wasSuccessful() else 1


def _run_isolated(args) -> int:
    import glob
    import subprocess

    modules = sorted(
        "tests." + os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(ROOT, "tests", "test_*.py"))
    )
    failed: list[tuple[str, int, float]] = []
    t_all = time.monotonic()
    for mod in modules:
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "--module", mod, "--timeout", str(args.timeout)]
        if args.pattern:
            cmd += ["-k", args.pattern]
        t0 = time.monotonic()
        print(f"\n########## {mod} ##########", flush=True)
        rc = subprocess.call(cmd, cwd=ROOT)
        elapsed = time.monotonic() - t0
        print(f"########## {mod}: {'OK' if rc == 0 else f'失败 (exit {rc})'}  {elapsed:.1f}s", flush=True)
        if rc != 0:
            failed.append((mod, rc, elapsed))
    print(f"\n==== {len(modules)} 个测试文件，{len(failed)} 个失败，用时 {time.monotonic() - t_all:.0f}s ====")
    for mod, rc, elapsed in failed:
        print(f"  失败  {mod}  exit={rc}  {elapsed:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
