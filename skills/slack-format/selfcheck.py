#!/usr/bin/env python3
"""selfcheck: offline build+validate of every slack_format element. No posting.

Exits 0 if every helper produces a block that passes validate() and a full
showcase message builds within Slack's limits. Run after any environment
change to confirm the library still works end to end.
"""
import sys

import slack_format as sf


def main() -> int:
    checks = {
        "header": sf.header("H1"),
        "h2": sf.h2("H2"),
        "markdown": sf.markdown("# md\n- a\n- b"),
        "section": sf.section("para *bold*"),
        "fields": sf.fields([("a", "1"), ("b", "2")]),
        "bullets": sf.bullets(["x", "y"]),
        "numbered": sf.numbered(["x", "y"]),
        "divider": sf.divider(),
        "table": sf.table(["A", "B"], [["1", "2"], ["3", "4"]]),
        "md_table": sf.md_table(["A", "B"], [["1", "2"]]),
        "mono_table": sf.mono_table(["A", "B"], [["1", "2"]]),
        "context": sf.context("note"),
        "buttons": sf.buttons([("Go", "https://x.com")]),
        "image": sf.image("https://x.com/i.png", "alt"),
        "card": sf.card("T", "B", button=("Open", "https://x.com")),
        "carousel": sf.carousel([{"title": "c1", "body": "b1"}]),
    }
    failures = []
    for name, block in checks.items():
        blocks = block if isinstance(block, list) else [block]
        issues = sf.validate(blocks)
        status = "ok" if not issues else "ISSUES: " + "; ".join(issues)
        print(f"  [{'ok' if not issues else 'x'}] {name:<12} {status if issues else ''}".rstrip())
        if issues:
            failures.append(name)

    # full message under limits
    msg = sf.Message("selfcheck")
    for b in checks.values():
        msg.add(b)
    try:
        payload = msg.build(strict=True)
        print(f"  [ok] full message: {len(payload['blocks'])} blocks, validation passed")
    except sf.SlackFormatError as exc:
        print(f"  [x] full message build failed: {exc}")
        failures.append("full-message")

    if failures:
        print(f"\nFAILED: {failures}", file=sys.stderr)
        return 1
    print("\nAll slack_format elements build + validate. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
