try:
    from specimen_app.main import main
except ImportError as exc:
    import sys
    print(f"\n[错误] 缺少依赖库：{exc}")
    print("请运行：  pip install -r requirements.txt")
    print("或直接下载打包好的 EXE（无需安装 Python）：")
    print("  https://github.com/deyuanyang92-dev/specimens-organise/releases")
    sys.exit(1)


if __name__ == "__main__":
    main()
