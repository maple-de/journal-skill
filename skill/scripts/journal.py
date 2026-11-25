#!/usr/bin/env python3
"""journal.py — 「工作旅程笔记」的小工具（仅用标准库）。

在项目目录下运行；journal 存放于 <项目根>/.journal/。
项目根 = 向上最近的已有 .journal/ 的目录（不越过 git 根目录；非 git 项目不越过 $HOME），
否则 git 根目录，否则当前目录。可用环境变量 JOURNAL_DIR 覆盖。

  journal.py new TITLE [--goal TEXT] [--slug SLUG]
  journal.py add TYPE [TEXT | -] [--by NAME] [-j NAME]
  journal.py focus TEXT [-j NAME]
  journal.py status [-j NAME] [-n N]
  journal.py close [-j NAME] [--note TEXT] < 回顾正文
  journal.py list [--active]
  journal.py dir
  journal.py hook          # 给 agent 的 SessionStart hook 用（Claude Code / Codex 通用）

TYPE: idea next note decision issue done deadend question flag
TEXT 为 "-" 或省略时从 stdin 读取（含引号、反引号、$ 或多行时用 heredoc）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TYPES = {
    "idea": "💡",
    "next": "➡️",
    "note": "📌",
    "decision": "🔀",
    "issue": "⚠️",
    "done": "✅",
    "deadend": "↩️",
    "question": "❓",
    "flag": "🚩",
    "end": "🏁",
}
LEGEND = (
    "> 💡想法 ➡️计划 📌注解 🔀决定 ⚠️问题 ✅进展 ↩️弯路 ❓疑问 🚩节点 🏁结束"
    " · 〔名字〕= AI 补记"
)
PLACEHOLDERS = {"- [ ] （暂无）", "- （暂无）"}
TIME_FMT = "%Y-%m-%d %H:%M"
FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)

TEMPLATE = """---
title: {title}
status: active
started: {started}
ended:
project: {project}
---

# {title}

## 目标

{goal}

## 当前

**焦点**：（待补充）

**下一步**
- [ ] （暂无）

**悬而未决**
- （暂无）

## 旅程

{legend}
"""


# ---------- helpers ----------

def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def project_root() -> Path:
    cwd = Path.cwd().resolve()
    top = git("rev-parse", "--show-toplevel")
    top_p = Path(top).resolve() if top else None
    home = Path.home().resolve()
    chain = [cwd, *cwd.parents]
    if top_p and top_p in chain:
        chain = chain[: chain.index(top_p) + 1]
    elif home in chain and cwd != home:
        chain = chain[: chain.index(home)]
    for p in chain:
        if (p / ".journal").is_dir():
            return p
    return top_p or cwd


def journal_dir() -> Path:
    env = os.environ.get("JOURNAL_DIR")
    return Path(env).expanduser().resolve() if env else project_root() / ".journal"


def read_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    m = FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip()
    return meta


def set_meta(text: str, key: str, value: str) -> str:
    m = FM_RE.match(text)
    if not m:
        return text
    fm = m.group(1)
    if re.search(rf"^{re.escape(key)}:", fm, re.M):
        fm = re.sub(rf"^{re.escape(key)}:.*$", lambda _: f"{key}: {value}", fm, count=1, flags=re.M)
    else:
        fm += f"\n{key}: {value}"
    return f"---\n{fm}\n---\n" + text[m.end():]


def journals(d: Path) -> list[tuple[Path, dict[str, str], str]]:
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            out.append((p, read_meta(text), text))
    return out


def fmt_list(js) -> str:
    return "\n".join(f"  - {p.name}  {m.get('title', '')}  [{m.get('status', '?')}]" for p, m, _ in js)


def resolve(d: Path, name: str | None) -> Path:
    js = journals(d)
    if name:
        hits = [j for j in js if name in j[0].name or name == j[1].get("title")]
        if len(hits) == 1:
            return hits[0][0]
        if not hits:
            die(f"找不到匹配「{name}」的 journal（目录：{d}）")
        die("匹配到多个 journal，请写得更具体：\n" + fmt_list(hits))
    active = [j for j in js if j[1].get("status") == "active"]
    if len(active) == 1:
        return active[0][0]
    if not active:
        die(f"没有进行中的 journal（目录：{d}）。先用 `new` 新建一篇。")
    die("有多个进行中的 journal，请用 -j 指定其一（文件名片段即可）：\n" + fmt_list(active))
    raise AssertionError  # unreachable


def section_span(text: str, title: str) -> tuple[int, int] | None:
    m = re.search(rf"^## {re.escape(title)}[ \t]*$", text, re.M)
    if not m:
        return None
    nxt = re.search(r"^## ", text[m.end():], re.M)
    return m.start(), (m.end() + nxt.start() if nxt else len(text))


def list_items(text: str, marker: str) -> list[str]:
    """「当前」区块里 marker 下面的列表项（不含占位符）。"""
    span = section_span(text, "当前")
    if not span:
        return []
    lines = text[span[0] : span[1]].split("\n")
    idx = next((k for k, ln in enumerate(lines) if ln.strip().startswith(marker)), None)
    if idx is None:
        return []
    out = []
    for ln in lines[idx + 1 :]:
        if not ln.startswith("- "):
            break
        if ln.strip() not in PLACEHOLDERS:
            out.append(ln)
    return out


def get_text(arg: str | None) -> str:
    if arg is None or arg == "-":
        arg = "" if sys.stdin.isatty() else sys.stdin.read()
    arg = arg.strip()
    if not arg:
        die("内容为空（直接给出文本，或用 `-` 加 heredoc 从 stdin 传入）")
    return arg


def humanize(delta: dt.timedelta) -> str:
    mins = max(int(delta.total_seconds() // 60), 0)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} 天")
    if h:
        parts.append(f"{h} 小时")
    if not d and m:
        parts.append(f"{m} 分钟")
    return " ".join(parts) or "不到 1 分钟"


def short_path(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


# ---------- core edits ----------

def format_entry(emoji: str, text: str, by: str | None, t: dt.datetime) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    head = f"- {t:%H:%M} {emoji} {lines[0]}" + (f" 〔{by}〕" if by else "")
    rest = [("  " + ln) if ln.strip() else "" for ln in lines[1:]]
    return "\n".join([head, *rest])


def add_to_list(content: str, marker: str, item: str) -> str:
    """把 item 追加到「当前」区块里 marker 下面的列表末尾，顺手去掉占位符。"""
    span = section_span(content, "当前")
    if not span:
        return content
    s, e = span
    lines = content[s:e].split("\n")
    idx = next((k for k, ln in enumerate(lines) if ln.strip().startswith(marker)), None)
    if idx is None:
        return content
    j = idx + 1
    while j < len(lines) and lines[j].startswith("- "):
        j += 1
    block = [ln for ln in lines[idx + 1 : j] if ln.strip() not in PLACEHOLDERS] + [item]
    lines = lines[: idx + 1] + block + lines[j:]
    return content[:s] + "\n".join(lines) + content[e:]


def append_entry(path: Path, kind: str, text: str, by: str | None = None) -> None:
    content = path.read_text(encoding="utf-8")
    if read_meta(content).get("status") == "closed":
        die(f"{path.name} 已结束（视为历史记录）。如需补充感想，请在文末追加「## 后记」。")
    span = section_span(content, "旅程")
    if not span:
        content = content.rstrip("\n") + f"\n\n## 旅程\n\n{LEGEND}\n"
        span = section_span(content, "旅程")
    s, e = span
    t = dt.datetime.now()
    today = f"{t:%Y-%m-%d}"
    body = content[s:e].rstrip("\n")
    days = re.findall(r"^### (\d{4}-\d{2}-\d{2})", body, re.M)
    if not days or days[-1] != today:
        body += f"\n\n### {today}\n"
    body += "\n" + format_entry(TYPES[kind], text, by, t) + "\n"
    rest = content[e:]
    content = content[:s] + body + ("\n" + rest if rest.strip() else "")

    first = text.splitlines()[0].strip()
    if kind == "next":
        content = add_to_list(content, "**下一步**", f"- [ ] {first}")
    elif kind == "question":
        content = add_to_list(content, "**悬而未决**", f"- {first}")
    path.write_text(content, encoding="utf-8")


def parse_entries(content: str) -> list[tuple[str, str]]:
    span = section_span(content, "旅程")
    if not span:
        return []
    day, out = "", []
    for line in content[span[0] : span[1]].splitlines():
        m = re.match(r"^### (\d{4}-\d{2}-\d{2})", line)
        if m:
            day = m.group(1)
        elif re.match(r"^- \d{1,2}:\d{2} ", line):
            out.append([day, line[2:]])
        elif out and line.startswith("  ") and line.strip():
            out[-1][1] += "\n      " + line.strip()
    return [tuple(x) for x in out]


# ---------- commands ----------

def cmd_dir(_a) -> None:
    print(journal_dir())


def cmd_new(a) -> None:
    d = journal_dir()
    root = d.parent
    title = a.title.strip() or die("标题为空")
    slug = re.sub(r"[^\w\-]+", "-", (a.slug or title).strip().lower()).strip("-")[:60] or "journal"
    t = dt.datetime.now()
    others = [j for j in journals(d) if j[1].get("status") == "active"]

    d.mkdir(parents=True, exist_ok=True)
    base = f"{t:%Y-%m-%d}-{slug}"
    path, i = d / f"{base}.md", 2
    while path.exists():
        path, i = d / f"{base}-{i}.md", i + 1
    path.write_text(
        TEMPLATE.format(
            title=title,
            started=f"{t:{TIME_FMT}}",
            project=short_path(root),
            goal=(a.goal or "").strip() or "（待补充）",
            legend=LEGEND,
        ),
        encoding="utf-8",
    )
    append_entry(path, "flag", f"开始：{title}")
    print(path)

    if others:
        print("NOTE: 另有进行中的 journal，之后的 add/focus/status/close 需用 -j 指定：\n" + fmt_list(others))
    if git("rev-parse", "--is-inside-work-tree", cwd=root) == "true":
        rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        ignored = git("check-ignore", rel, cwd=root)
        tracked = git("ls-files", ".journal", cwd=root)
        if not ignored and not tracked:
            print(
                "NOTE: .journal/ 在 git 仓库里，但既没被忽略也没被提交。问用户一次："
                "提交进仓库 / 只在本地忽略（写入 .git/info/exclude）/ 加进 .gitignore。"
            )


def cmd_add(a) -> None:
    path = resolve(journal_dir(), a.journal)
    append_entry(path, a.type, get_text(a.text), (a.by or "").strip() or None)
    print(f"已记 {TYPES[a.type]} → {path.name}")


def cmd_focus(a) -> None:
    path = resolve(journal_dir(), a.journal)
    text = get_text(a.text).splitlines()[0]
    content = path.read_text(encoding="utf-8")
    span = section_span(content, "当前")
    if not span:
        die("这篇 journal 没有「当前」区块（可能已结束）")
    s, e = span
    sec, n = re.subn(r"^\*\*焦点\*\*：.*$", lambda _: f"**焦点**：{text}", content[s:e], count=1, flags=re.M)
    if not n:
        sec = re.sub(r"^## 当前[ \t]*\n", lambda m: f"{m.group(0)}\n**焦点**：{text}\n", sec, count=1, flags=re.M)
    path.write_text(content[:s] + sec + content[e:], encoding="utf-8")
    print(f"焦点 → {text}")


def cmd_status(a) -> None:
    path = resolve(journal_dir(), a.journal)
    content = path.read_text(encoding="utf-8")
    meta = read_meta(content)
    print(f"# {meta.get('title', path.stem)}  [{meta.get('status', '?')}]")
    print(f"文件：{path}")
    when = f"开始：{meta.get('started', '?')}"
    if meta.get("ended"):
        when += f"   结束：{meta['ended']}"
    print(when)
    for title in ("目标", "当前", "回顾"):
        span = section_span(content, title)
        if span:
            print("\n" + content[span[0] : span[1]].strip())
    entries = parse_entries(content)
    if entries:
        shown = entries[-a.n :] if a.n > 0 else entries
        print(f"\n## 最近 {len(shown)} 条旅程（共 {len(entries)} 条）")
        for day, text in shown:
            print(f"{day} {text}")


def cmd_close(a) -> None:
    path = resolve(journal_dir(), a.journal)
    content = path.read_text(encoding="utf-8")
    meta = read_meta(content)
    if meta.get("status") == "closed":
        die(f"{path.name} 已经结束了")
    retro = "" if sys.stdin.isatty() else sys.stdin.read()
    retro = re.sub(r"\A\s*## 回顾[ \t]*\n", "", retro).strip()
    if not retro:
        die("请通过 stdin（heredoc）提供回顾正文")

    t = dt.datetime.now()
    try:
        dur = humanize(t - dt.datetime.strptime(meta.get("started", ""), TIME_FMT))
    except ValueError:
        dur = "?"
    block = f"## 回顾\n\n**历时**：{meta.get('started', '?')} → {t:{TIME_FMT}}（{dur}）\n\n{retro}\n\n"

    span = section_span(content, "当前") or section_span(content, "回顾")
    if span:
        content = content[: span[0]] + block + content[span[1] :]
    else:
        jspan = section_span(content, "旅程")
        pos = jspan[0] if jspan else len(content)
        content = content[:pos] + block + content[pos:]
    path.write_text(content, encoding="utf-8")

    append_entry(path, "end", (a.note or "结束").strip())
    content = path.read_text(encoding="utf-8")
    content = set_meta(content, "status", "closed")
    content = set_meta(content, "ended", f"{t:{TIME_FMT}}")
    path.write_text(content, encoding="utf-8")
    print(f"已结束 🏁 {path}\n历时：{dur}")


def cmd_list(a) -> None:
    d = journal_dir()
    js = journals(d)
    if a.active:
        js = [j for j in js if j[1].get("status") == "active"]
    if not js:
        print(f"（{d} 下还没有{'进行中的 ' if a.active else ''} journal）")
        return
    print(f"目录：{d}")
    js.sort(key=lambda j: (j[1].get("started", ""), j[0].name), reverse=True)
    for p, m, text in js:
        mark = "●" if m.get("status") == "active" else "○"
        start = m.get("started", "")[:10] or "?"
        end = (m.get("ended") or "")[:10] or "进行中"
        print(f"{mark} {start} → {end}  {m.get('title', p.stem)}  ({p.name}, {len(parse_entries(text))} 条)")


def cmd_hook(_a) -> None:
    """SessionStart hook：当前项目有进行中的 journal 时输出一段提示，否则什么都不输出。

    Claude Code 和 Codex 都会把 hook 的 JSON 输入写到 stdin（含 cwd），并把 stdout 的纯文本
    加进模型上下文。这个命令无论如何都以 0 退出，坏掉的 journal 不应该影响开会话。
    """
    try:
        data = {}
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {}
        cwd = data.get("cwd") if isinstance(data, dict) else None
        if cwd and Path(cwd).is_dir():
            os.chdir(cwd)
        active = [j for j in journals(journal_dir()) if j[1].get("status") == "active"]
        if not active:
            return
        here = Path.cwd()
        out = [f"[journal] 当前项目有 {len(active)} 篇进行中的工作旅程笔记（journal skill）："]
        for p, m, text in active:
            rel = os.path.relpath(p, here) if p.is_relative_to(here) else short_path(p)
            fm = re.search(r"^\*\*焦点\*\*：(.*)$", text, re.M)
            nexts = [ln[6:] for ln in list_items(text, "**下一步**") if ln.startswith("- [ ] ")]
            qs = list_items(text, "**悬而未决**")
            entries = parse_entries(text)
            out.append(f"- {m.get('title', p.stem)}（{rel}，开始于 {m.get('started', '?')}）")
            if fm and fm.group(1).strip() != "（待补充）":
                out.append(f"  焦点：{fm.group(1).strip()}")
            if nexts:
                more = f" 等 {len(nexts)} 条" if len(nexts) > 3 else ""
                out.append(f"  下一步：{'；'.join(nexts[:3])}{more}")
            if qs:
                out.append(f"  悬而未决：{len(qs)} 条")
            if entries:
                day, last = entries[-1]
                last = last.splitlines()[0]
                out.append(f"  最近一条：{day} {last[:80]}{'…' if len(last) > 80 else ''}")
        out.append(
            "用户提到想法、下一步、注解、决定或弯路时，按 journal skill 记录；"
            "用户要接着干或问做到哪了，先运行它的 status。不必主动汇报这条提示。"
        )
        print("\n".join(out))
    except Exception:
        return


def main() -> None:
    ap = argparse.ArgumentParser(description="工作旅程笔记")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dir", help="打印 journal 目录")
    p.set_defaults(fn=cmd_dir)

    p = sub.add_parser("new", help="新建 journal")
    p.add_argument("title")
    p.add_argument("--goal")
    p.add_argument("--slug", help="文件名用的短名（默认由标题生成）")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("add", help="追加一条旅程")
    p.add_argument("type", choices=[k for k in TYPES if k != "end"])
    p.add_argument("text", nargs="?")
    p.add_argument("--by", metavar="NAME", help="AI 补记时填自己的名字（如 Claude、Codex），条目会带上〔NAME〕")
    p.add_argument("-j", "--journal")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("focus", help="改写「当前 · 焦点」")
    p.add_argument("text", nargs="?")
    p.add_argument("-j", "--journal")
    p.set_defaults(fn=cmd_focus)

    p = sub.add_parser("status", help="查看目标/当前/最近旅程")
    p.add_argument("-j", "--journal")
    p.add_argument("-n", type=int, default=10, help="显示最近几条旅程（0 = 全部）")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("close", help="写入回顾并结束（回顾正文从 stdin 读）")
    p.add_argument("-j", "--journal")
    p.add_argument("--note", help="🏁 条目的文字，默认「结束」")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("list", help="列出 journal")
    p.add_argument("--active", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("hook", help="SessionStart hook：输出进行中 journal 的提示（Claude Code / Codex 通用）")
    p.set_defaults(fn=cmd_hook)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
