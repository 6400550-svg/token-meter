#!/bin/bash
# Claude Code 用量表 —— 卸载（SwiftBar 本身保留）
DIR=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null)
[ -n "$DIR" ] && rm -f "$DIR/tokens.1m.py"
printf "同时删除统计缓存和设置（~/.token-meter）？[y/N] "
read -r yn
if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
  rm -rf "$HOME/.token-meter"
  echo "已删除。"
fi
echo "插件已移除。SwiftBar 没有卸载，不需要的话把它从「应用程序」拖进废纸篓即可。"
printf "\n按回车键关闭…"
read -r _
