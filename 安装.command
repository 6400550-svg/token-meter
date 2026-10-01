#!/bin/bash
# Claude Code 用量表 —— 一键安装（双击运行，或在终端里 bash 安装.command）
cd "$(dirname "$0")" || exit 1

STATE="$HOME/.token-meter"
DEFAULT_DIR="$STATE/plugins"
PLUGIN="tokens.1m.py"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m%s\033[0m\n\n按回车键关闭…" "$1"; read -r _; exit 1; }

echo "========================================"
echo "   Claude Code 用量表 · 安装"
echo "========================================"

# ---------- 1. python3 ----------
say "① 检查 Python 3"
PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [ -x "$p" ] && PY="$p" && break
done
# 系统自带的 /usr/bin/python3 只有装了命令行工具才能用，否则会弹安装窗口
if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1; then PY=/usr/bin/python3; fi
if [ -z "$PY" ]; then
  xcode-select --install >/dev/null 2>&1
  fail "没找到 Python 3。已弹出「命令行开发者工具」安装窗口，装完后再双击一次本安装程序。"
fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail "Python 版本太旧（需要 3.8 以上）：$("$PY" --version)"
echo "   用 $PY（$("$PY" --version 2>&1)）"

# ---------- 2. SwiftBar ----------
say "② 检查 SwiftBar（菜单栏插件工具）"
if [ -d /Applications/SwiftBar.app ] || [ -d "$HOME/Applications/SwiftBar.app" ]; then
  echo "   已安装"
elif command -v brew >/dev/null 2>&1; then
  echo "   用 Homebrew 安装 SwiftBar…"
  brew install --cask swiftbar || fail "SwiftBar 安装失败。可以去 https://swiftbar.app 手动下载，装好后再运行本程序。"
else
  open "https://github.com/swiftbar/SwiftBar/releases/latest"
  fail "没装 SwiftBar。已打开下载页：下载 SwiftBar.zip，解压后把 SwiftBar 拖进「应用程序」，再双击一次本安装程序。"
fi

# ---------- 3. 个人设置 ----------
say "③ 你的套餐"
echo "   1) Pro      \$20/月"
echo "   2) Max 5x   \$100/月"
echo "   3) Max 20x  \$200/月"
echo "   回车 = 跳过（不显示套餐价值对比）"
printf "   选择 [1/2/3]："
read -r choice
case "$choice" in
  1) PLAN=pro ;;
  2) PLAN=max5x ;;
  3) PLAN=max20x ;;
  *) PLAN="" ;;
esac

say "④ 每周额度的重置时间"
echo "   在 Claude 桌面 App → 设置 → 用量 里能看到「每周额度」什么时候重置。"
echo "   填上以后，菜单里的「本周累计」会和官方的周期对齐；回车跳过则按最近 7 天算。"
printf "   星期几重置？（1=周一 … 7=周日）："
read -r wday
WHEN=""
if [[ "$wday" =~ ^[1-7]$ ]]; then
  printf "   几点？（24 小时制，如 15:00）："
  read -r hm
  if [[ "$hm" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]]; then WHEN="$wday $hm"; else echo "   时间格式不对，先跳过，之后可以重新运行安装。"; fi
fi

mkdir -p "$STATE"
"$PY" - "$STATE/config.json" "$PLAN" "$WHEN" <<'EOF'
import json, sys
from datetime import datetime, timedelta, timezone
path, plan, when = sys.argv[1:4]
cfg = {}
if plan:
    cfg["plan"] = plan
if when:
    wday, hm = when.split()
    h, m = map(int, hm.split(":"))
    now = datetime.now().astimezone()
    t = (now + timedelta(days=(int(wday) - 1 - now.weekday()) % 7)).replace(hour=h, minute=m, second=0, microsecond=0)
    cfg["weekly_reset_utc"] = t.astimezone(timezone.utc).isoformat()
with open(path, "w") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
EOF
echo "   已保存到 $STATE/config.json"

# ---------- 4. 放插件 ----------
say "⑤ 安装插件"
osascript -e 'quit app "SwiftBar"' >/dev/null 2>&1
sleep 1
DIR=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null)
if [ -z "$DIR" ] || [ ! -d "$DIR" ]; then
  DIR="$DEFAULT_DIR"
  mkdir -p "$DIR"
  defaults write com.ameba.SwiftBar PluginDirectory "$DIR"
fi
# 用找到的 python3 替换第一行，SwiftBar 的环境里不一定有 PATH
{ echo "#!$PY"; tail -n +2 "$PLUGIN"; } > "$DIR/$PLUGIN.tmp" || fail "写入 $DIR 失败"
chmod +x "$DIR/$PLUGIN.tmp"
xattr -c "$DIR/$PLUGIN.tmp" 2>/dev/null
mv "$DIR/$PLUGIN.tmp" "$DIR/$PLUGIN"
echo "   插件放在 $DIR"

say "⑥ 首次统计（历史记录多的话要几秒）"
"$PY" "$DIR/$PLUGIN" | head -1 | cut -d'|' -f1 | sed 's/^/   菜单栏将显示：/'

open -a SwiftBar
echo
echo "========================================"
echo " 装好了！看屏幕右上角菜单栏，点开就是详细用量。"
echo " 每分钟自动刷新。"
echo "========================================"
printf "\n按回车键关闭…"
read -r _
