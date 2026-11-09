#!/bin/bash
# 每天把服务器上最新的数据库备份拉到桌面。
# 服务器端 app/backup.py 每天 03:20 生成 data/backups/campus-YYYYMMDD.db。
set -u

KEY="$HOME/.ssh/id_ed25519"
HOST="root@139.129.230.207"
REMOTE="/opt/qut-campus-assistant/data/backups"
DEST="$HOME/Desktop/QUT备份"
KEEP_DAYS=30
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15)

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

mkdir -p "$DEST" || { log "无法创建 $DEST"; exit 1; }
chmod 700 "$DEST" 2>/dev/null

remote_newest() {
  "${SSH[@]}" "$HOST" "ls -1t $REMOTE/$1 2>/dev/null | head -1"
}

db_path=$(remote_newest 'campus-*.db') || { log "ssh 连接失败，本次跳过"; exit 1; }
if [ -z "$db_path" ]; then
  log "服务器上还没有备份文件"
  exit 1
fi

name=$(basename "$db_path")
if scp -q -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST:$db_path" "$DEST/$name.part"; then
  mv -f "$DEST/$name.part" "$DEST/$name"
  chmod 600 "$DEST/$name"
  log "已下载 $name（$(du -h "$DEST/$name" | cut -f1)）"
else
  rm -f "$DEST/$name.part"
  log "下载 $name 失败"
  exit 1
fi

cfg_path=$(remote_newest 'config-*.json')
if [ -n "$cfg_path" ]; then
  cfg_name=$(basename "$cfg_path")
  if scp -q -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST:$cfg_path" "$DEST/$cfg_name.part"; then
    mv -f "$DEST/$cfg_name.part" "$DEST/$cfg_name"
    chmod 600 "$DEST/$cfg_name"
  fi
fi

# 只保留最近 30 天；备份含凭据，目录与文件都收紧权限
find "$DEST" -maxdepth 1 -type f -name 'campus-*.db' -mtime +"$KEEP_DAYS" -delete 2>/dev/null
find "$DEST" -maxdepth 1 -type f -name 'config-*.json' -mtime +"$KEEP_DAYS" -delete 2>/dev/null
log "备份完成"
