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
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, ROOT)
    _WatchdogResult.timeout = args.timeout
    loader = unittest.TestLoader()
    if args.pattern:
        loader.testNamePatterns = [f"*{args.pattern}*"]
    suite = loader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    runner = unittest.TextTestRunner(verbosity=1, resultclass=_WatchdogResult, stream=sys.stdout)
    result = runner.run(suite)
    if _WatchdogResult.slow:
        print("\n最慢的用例：")
        for elapsed, name in sorted(_WatchdogResult.slow, reverse=True)[:15]:
            print(f"  {elapsed:6.1f}s  {name}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
