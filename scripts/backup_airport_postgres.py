"""Create a private, checked custom archive of the dedicated airport database."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.postgres_backup import create_postgres_backup


def main():
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="新归档文件路径，父目录须已存在")
    args = parser.parse_args()
    try:
        report = asyncio.run(create_postgres_backup(
            os.getenv("DATABASE_URL", ""), args.output,
        ))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Do not expose driver/tool errors or connection strings.
        print(f"机场 PostgreSQL 备份失败：{type(exc).__name__}。请检查专属连接、工具和目录权限。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
