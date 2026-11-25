#!/usr/bin/env bash
# journal skill 安装脚本：把「工作旅程笔记」skill 装进 Claude Code / Codex，并配置 SessionStart hook。
#
# 用法：./install.sh [--agent claude|codex|all|claude,codex] [--no-hook] [--uninstall] [--force]
#
#   --agent     装给哪些 agent。默认自动检测本机已有的（命令在 PATH 里，或配置目录已存在）
#   --no-hook   只装 skill，不配置 SessionStart hook（之前装过的 hook 会被移除）
#   --uninstall 卸载：删除 skill 和 hook 配置（各项目 .journal/ 里的笔记不受影响）
#   --force     目标位置已有一个同名但不是本 skill 的目录时，仍然覆盖
#
# 安装位置（尊重 CLAUDE_CONFIG_DIR / CODEX_HOME 环境变量）：
#   Claude Code  skill → ~/.claude/skills/journal      hook → ~/.claude/settings.json
#   Codex        skill → ~/.agents/skills/journal      hook → ~/.codex/hooks.json
#
# 可以重复运行：skill 会被新版本覆盖，hook 会被替换而不是重复添加；
# 改动配置文件前会备份为 <文件>.bak-<时间戳>。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skill"
NAME=journal
MATCHER='startup|resume|clear|compact'
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
die() { echo "错误：$*" >&2; exit 1; }

AGENTS="" HOOK=1 UNINSTALL=0 FORCE=0
while (($#)); do
  case "$1" in
    --agent) AGENTS="${2:?--agent 需要参数}"; shift 2 ;;
    --agent=*) AGENTS="${1#*=}"; shift ;;
    --no-hook) HOOK=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --force) FORCE=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "需要 python3"
[[ -f "$SRC/SKILL.md" && -f "$SRC/scripts/journal.py" ]] || die "找不到 skill 源文件：$SRC"

if [[ -z "$AGENTS" ]]; then
  detected=()
  { command -v claude >/dev/null || [[ -d "$CLAUDE_DIR" ]]; } && detected+=(claude)
  { command -v codex >/dev/null || [[ -d "$CODEX_DIR" ]]; } && detected+=(codex)
  ((${#detected[@]})) || die "没检测到 Claude Code 或 Codex，请用 --agent 指定"
  AGENTS="$(IFS=,; echo "${detected[*]}")"
fi
[[ "$AGENTS" == all ]] && AGENTS=claude,codex

skill_dir() {
  case "$1" in
    claude) echo "$CLAUDE_DIR/skills/$NAME" ;;
    codex) echo "$HOME/.agents/skills/$NAME" ;;
  esac
}
hook_file() {
  case "$1" in
    claude) echo "$CLAUDE_DIR/settings.json" ;;
    codex) echo "$CODEX_DIR/hooks.json" ;;
  esac
}
is_ours() { [[ -f "$1/SKILL.md" ]] && grep -q 'scripts/journal.py' "$1/SKILL.md"; }

install_skill() {
  local dst="$1"
  if [[ -e "$dst" || -L "$dst" ]]; then
    [[ "$(cd "$dst" 2>/dev/null && pwd -P)" != "$(cd "$SRC" && pwd -P)" ]] || die "目标 $dst 就是源目录本身"
    if ! is_ours "$dst" && ((!FORCE)); then
      die "$dst 已存在且不像是 journal skill；确认可以覆盖的话加 --force"
    fi
    rm -rf "$dst"
  fi
  mkdir -p "$dst"
  cp -R "$SRC/." "$dst/"
  # 把 SKILL.md 里的 __SKILL_DIR__ 换成实际安装路径，agent 照抄命令即可
  "$PYTHON" - "$dst" <<'PY'
import pathlib, sys
d = pathlib.Path(sys.argv[1])
p = d / "SKILL.md"
p.write_text(p.read_text(encoding="utf-8").replace("__SKILL_DIR__", str(d)), encoding="utf-8")
PY
  chmod +x "$dst/scripts/journal.py"
}

# edit_hooks install|uninstall <配置文件> <journal.py 路径>
# Claude Code 的 settings.json 和 Codex 的 hooks.json 用的是同一套 hooks 结构：
#   {"hooks": {"SessionStart": [{"matcher": "...", "hooks": [{"type": "command", "command": "..."}]}]}}
edit_hooks() {
  "$PYTHON" - "$1" "$2" "$3" "$PYTHON" "$MATCHER" <<'PY'
import json, os, shlex, shutil, sys, time

action, path, script, python, matcher = sys.argv[1:6]
path = os.path.realpath(path)  # 配置文件是软链接（dotfiles 管理）时改它指向的真实文件
cmd = f"{shlex.quote(python)} {shlex.quote(script)} hook"

def ours(h):
    c = str(h.get("command", ""))
    return "journal.py" in c and c.rstrip().endswith(" hook")

data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError as e:
            sys.exit(f"错误：{path} 不是合法的 JSON（{e}），没有改动它")
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        sys.exit(f"错误：{path} 的结构不是预期的样子，没有改动它")
elif action == "uninstall":
    sys.exit(0)

before = json.dumps(data, sort_keys=True)
hooks = data.get("hooks", {})
groups = []
for g in hooks.get("SessionStart") or []:
    if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
        groups.append(g)
        continue
    kept = [h for h in g["hooks"] if not (isinstance(h, dict) and ours(h))]
    if len(kept) == len(g["hooks"]):
        groups.append(g)
    elif kept:  # 同组里还有别人的 hook，保留它们
        groups.append({**g, "hooks": kept})
if action == "install":
    groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd, "timeout": 10}]})

if groups:
    hooks["SessionStart"] = groups
else:
    hooks.pop("SessionStart", None)
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)

if json.dumps(data, sort_keys=True) == before:
    print(f"  hook 配置无变化：{path}")
    sys.exit(0)
if os.path.exists(path):
    bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, bak)
    print(f"  已备份：{bak}")
if not data and action == "uninstall":
    os.remove(path)
    print(f"  hook 已移除（文件已空，删除）：{path}")
    sys.exit(0)
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp-journal"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, path)
print(f"  hook {'已写入' if action == 'install' else '已移除'}：{path}")
PY
}

for agent in ${AGENTS//,/ }; do
  case "$agent" in
    claude | codex) ;;
    *) die "未知 agent：$agent（可选 claude、codex、all）" ;;
  esac
  dst="$(skill_dir "$agent")"
  cfg="$(hook_file "$agent")"
  echo "▸ $agent"

  if ((UNINSTALL)); then
    edit_hooks uninstall "$cfg" "$dst/scripts/journal.py"
    if is_ours "$dst"; then
      rm -rf "$dst"
      echo "  skill 已删除：$dst"
    elif [[ -e "$dst" ]]; then
      echo "  跳过：$dst 不像是 journal skill，没有删除"
    fi
    continue
  fi

  install_skill "$dst"
  echo "  skill 已安装：$dst"
  if ((HOOK)); then
    edit_hooks install "$cfg" "$dst/scripts/journal.py"
  else
    edit_hooks uninstall "$cfg" "$dst/scripts/journal.py"
  fi

  case "$agent" in
    claude)
      echo "  → 新开的 Claude Code 会话生效"
      ;;
    codex)
      ((HOOK)) && echo "  → 在 Codex 里运行一次 /hooks 信任这个 hook（未信任的 hook 不会执行）"
      echo "  → 若 skill 没出现在 Codex 里，重启 Codex"
      if ((HOOK)) && [[ -f "$CODEX_DIR/config.toml" ]] && grep -Eq '^[[:space:]]*\[\[?hooks[].]' "$CODEX_DIR/config.toml"; then
        echo "  ⚠ $CODEX_DIR/config.toml 里也有 [hooks]：Codex 对同一层同时用 hooks.json 和 config.toml 会给出启动警告，"
        echo "    可以把 hooks.json 里 journal 的那条挪进 config.toml"
      fi
      ;;
  esac
done
