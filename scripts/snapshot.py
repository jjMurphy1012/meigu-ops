#!/usr/bin/env python3
"""归档每日持仓/行情快照到 data/snapshots/。

快照的用途是让复盘可以回答"当时到底什么情况",而不是靠记忆。
它也是唯一能量化"现金闲置的机会成本"和"市价单滑点"的数据源。

用法:
    # 由 agent 把 MCP 返回的 JSON 通过 stdin 传入
    echo '{"portfolio": {...}, "positions": [...], "quotes": {...}}' \
        | python3 scripts/snapshot.py --stdin

    python3 scripts/snapshot.py --stdin --checkpoint 15:37
    python3 scripts/snapshot.py --list
    python3 scripts/snapshot.py --show 2026-08-17

data/snapshots/ 已 gitignore —— 快照含真实持仓,属于用户层。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from meigu_lib import DATA_DIR, WEEKDAY_ZH, day_info, now_et, today_et

SNAPSHOT_DIR = DATA_DIR / "snapshots"


def write_snapshot(payload: dict, checkpoint: str, date: dt.date) -> tuple[bool, str]:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{date.isoformat()}.json"

    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "date": date.isoformat(),
        "weekday": WEEKDAY_ZH[date.weekday()],
        "day_info": day_info(date).reason,
        "checkpoints": {},
    }

    replaced = checkpoint in existing["checkpoints"]
    existing["checkpoints"][checkpoint] = {
        "captured_at_et": now_et().strftime("%Y-%m-%d %H:%M:%S"),
        **payload,
    }
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8"
    )
    return replaced, str(path.relative_to(DATA_DIR.parent))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="归档持仓/行情快照")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON payload")
    ap.add_argument("--checkpoint", default="", help="检查点标签,如 09:12 / 15:37")
    ap.add_argument("--date", help="YYYY-MM-DD,默认今天(ET)")
    ap.add_argument("--list", action="store_true", help="列出已有快照")
    ap.add_argument("--show", help="打印指定日期的快照")
    args = ap.parse_args(argv)

    if args.list:
        if not SNAPSHOT_DIR.exists():
            print("还没有任何快照。")
            return 0
        files = sorted(SNAPSHOT_DIR.glob("*.json"))
        print(f"=== 快照 {len(files)} 天 ===")
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cps = ", ".join(sorted(data.get("checkpoints", {})))
            except json.JSONDecodeError:
                cps = "(文件损坏)"
            print(f"  {f.stem}  检查点:{cps or '无'}")
        return 0

    if args.show:
        path = SNAPSHOT_DIR / f"{args.show}.json"
        if not path.exists():
            print(f"❌ 没有 {args.show} 的快照。", file=sys.stderr)
            return 1
        print(path.read_text(encoding="utf-8"))
        return 0

    if not args.stdin:
        ap.print_help()
        return 1

    raw = sys.stdin.read().strip()
    if not raw:
        print("❌ stdin 为空。", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"❌ stdin 不是合法 JSON:{exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("❌ payload 必须是 JSON 对象(建议含 portfolio / positions / quotes 三个键)。",
              file=sys.stderr)
        return 1

    date = dt.date.fromisoformat(args.date) if args.date else today_et()
    checkpoint = args.checkpoint or now_et().strftime("%H:%M")
    replaced, rel = write_snapshot(payload, checkpoint, date)

    verb = "已覆盖" if replaced else "已写入"
    print(f"✅ {verb} {rel} 的检查点 {checkpoint}")
    print(f"   顶层键:{', '.join(sorted(payload))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
