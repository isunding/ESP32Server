#!/bin/sh
# ============================================================================
#  IPTV 频道管理器 — 后端引擎
#  单文件：既做 CGI，也做后台扫描任务
#
#    iptv cgi               由 uhttpd 调用
#    iptv runjob <m> <s>    后台任务入口
#    iptv build             生成 m3u / txt
#    iptv stop              停止当前任务
#    iptv status            状态
# ============================================================================

ETC=/etc/iptv
RUN=/var/run/iptv
TMP=/tmp/iptv
DB=$ETC/channels.tsv
CONF=$ETC/config.sh
# 引擎实际位置（原值 /usr/libexec/iptv 不存在，导致 start 静默失败）
PREFIX=/data/iptv
SELF="$PREFIX/iptv"

# ---------------------------------------------------------------- 默认配置
IFACE=br-lan.3
MSD_PORT=12123
MSD_HOST=127.0.0.1
PUBLIC_HOST=10.0.0.11
SCAN_PORT_START=12130
SCANNERS=4
PROBE_TIMEOUT=1
PROBE_TIMEOUT_SLOW=5
MIN_BYTES=20000
PREFIXES="238.1.78 238.1.79"
BASE_PORTS="5872 4112 5928 4136"
SCAN_FROM=0
SCAN_TO=255
PLAYER_BASE=/www-iptv
MIRROR_BASE=/www
EPG_URL=''
LOGO_BASE="https://logo.catvod.com"
LEGACY_NAMES=0
# --- rtp2httpd 双引擎 ---
R2H_ENABLE=1
R2H_PORT=5140
R2H_TOKEN=""
R2H_CORS=1
# --- FCC 快速换台 ---
FCC_ADDR="117.71.18.200:15970"
FCC_TYPE=telecom
FCC_ENABLE=0
# --- RTSP 时移回看 ---
RTSP_ENABLE=0
RTSP_TMPL=""
CATCHUP_DAYS=7

[ -f "$CONF" ] && . "$CONF"

TAB=$(printf '\t')
mkdir -p "$RUN" "$TMP" 2>/dev/null

# ---------------------------------------------------------------- 工具
now_ts() { date +%s; }

# 安全写库: 非空才替换, 并保留一份备份
db_commit() {
  src="$1"
  [ -s "$src" ] || { echo "db_commit: 源文件为空, 拒绝覆盖" >&2; return 1; }

  src_rows=$(grep -c . "$src" 2>/dev/null); src_rows=${src_rows:-0}
  [ "$src_rows" -gt 0 ] || { echo "db_commit: 源文件为空行, 拒绝覆盖" >&2; return 1; }

  [ -f "$DB" ] && cp "$DB" "$DB.bak" 2>/dev/null

  try=0
  while [ "$try" -lt 3 ]; do
    try=$((try + 1))
    cp -f "$src" "$DB" 2>/dev/null
    sync 2>/dev/null
    db_rows=$(grep -c . "$DB" 2>/dev/null); db_rows=${db_rows:-0}
    # 行数一致 + 内容一致才算成功
    if [ "$db_rows" = "$src_rows" ]; then
      if cmp -s "$src" "$DB" 2>/dev/null; then
        rm -f "$DB.fail" 2>/dev/null
        return 0
      fi
    fi
    sleep 1
  done

  # 三次都失败: 记录失败标记, 让前端能提示
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] db_commit FAILED src=$src_rows db=$db_rows try=$try" >> "$DB.fail" 2>/dev/null
  echo "db_commit: 写入校验失败 $src_rows vs $db_rows" >&2
  return 1
}

is_running() {
  [ -f "$RUN/pid" ] || return 1
  p=$(cat "$RUN/pid" 2>/dev/null)
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || { rm -f "$RUN/pid" 2>/dev/null; return 1; }
  # pid 可能被别的进程复用: 校验命令行确实是 runjob
  if [ -r "/proc/$p/cmdline" ]; then
    grep -q 'runjob' "/proc/$p/cmdline" 2>/dev/null || {
      rm -f "$RUN/pid" 2>/dev/null; return 1
    }
  fi
  return 0
}

task_progress() {
  d=0
  for f in "$RUN"/done.*; do
    [ -f "$f" ] || continue
    d=$((d + $(cat "$f" 2>/dev/null | tr -dc '0-9')))
  done
  echo "$d"
}

task_found_count() {
  n=0
  for f in "$RUN"/found.*; do
    [ -f "$f" ] || continue
    c=$(grep -c . "$f" 2>/dev/null)
    n=$((n + ${c:-0}))
  done
  echo "$n"
}

cfg_write() {
  {
    echo "# IPTV 配置 — 由 Web 界面生成"
    echo "IFACE='$IFACE'"
    echo "MSD_PORT='$MSD_PORT'"
    echo "MSD_HOST='$MSD_HOST'"
    echo "PUBLIC_HOST='$PUBLIC_HOST'"
    echo "SCAN_PORT_START='$SCAN_PORT_START'"
    echo "SCANNERS='$SCANNERS'"
    echo "PROBE_TIMEOUT='$PROBE_TIMEOUT'"
    echo "PROBE_TIMEOUT_SLOW='$PROBE_TIMEOUT_SLOW'"
    echo "MIN_BYTES='$MIN_BYTES'"
    echo "PREFIXES='$PREFIXES'"
    echo "BASE_PORTS='$BASE_PORTS'"
    echo "SCAN_FROM='$SCAN_FROM'"
    echo "SCAN_TO='$SCAN_TO'"
    echo "PLAYER_BASE='$PLAYER_BASE'"
    echo "MIRROR_BASE='$MIRROR_BASE'"
    echo "EPG_URL='$EPG_URL'"
    echo "LOGO_BASE='$LOGO_BASE'"
    echo "LEGACY_NAMES='$LEGACY_NAMES'"
    echo "R2H_ENABLE='$R2H_ENABLE'"
    echo "R2H_PORT='$R2H_PORT'"
    echo "R2H_TOKEN='$R2H_TOKEN'"
    echo "R2H_CORS='$R2H_CORS'"
    echo "FCC_ADDR='$FCC_ADDR'"
    echo "FCC_TYPE='$FCC_TYPE'"
    echo "FCC_ENABLE='$FCC_ENABLE'"
    echo "RTSP_ENABLE='$RTSP_ENABLE'"
    echo "RTSP_TMPL='$RTSP_TMPL'"
    echo "CATCHUP_DAYS='$CATCHUP_DAYS'"
  } > "$CONF"
}

# ---------------------------------------------------------------- 扫描器实例
# 用 udpxy 实例代跑 msd_lite (本机内核缺 timerfd, msd_lite 无法运行)
start_scanners() {
  : > "$RUN/scanner.pids"
  i=0
  while [ "$i" -lt "$SCANNERS" ]; do
    p=$((SCAN_PORT_START + i))
    SCAN_BIND=$(ip -4 -o addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
    [ -n "$SCAN_BIND" ] || SCAN_BIND="$MSD_HOST"
    /data/iptv/bin/udpxy -p "$p" -m "$SCAN_BIND" -c 2 -B 65536 >/dev/null 2>&1 &
    echo $! >> "$RUN/scanner.pids"
    i=$((i + 1))
  done
  sleep 2
}

stop_scanners() {
  [ -f "$RUN/scanner.pids" ] || return 0
  while read -r pid; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done < "$RUN/scanner.pids"
  rm -f "$RUN/scanner.pids"
}

# 探测: 输出 "字节数 用时秒数"
# 4K 码流起播慢, 只用 1 秒会误判成失效 -> 先快探, 不足再用 SLOW 超时重试
probe() {
  r=$(curl -s -o /dev/null -w '%{size_download} %{time_total}' \
       --max-time "$PROBE_TIMEOUT" "http://127.0.0.1:$1/rtp/$2:$3" 2>/dev/null)
  b=${r%% *}; t=${r##* }
  case "$b" in ''|*[!0-9]*) b=0 ;; esac
  if [ "$b" -le "$MIN_BYTES" ] && [ "${PROBE_TIMEOUT_SLOW:-0}" -gt 0 ]; then
    r2=$(curl -s -o /dev/null -w '%{size_download} %{time_total}' \
         --max-time "$PROBE_TIMEOUT_SLOW" "http://127.0.0.1:$1/rtp/$2:$3" 2>/dev/null)
    b2=${r2%% *}; t2=${r2##* }
    case "$b2" in ''|*[!0-9]*) b2=0 ;; esac
    if [ "$b2" -gt "$b" ]; then b=$b2; t=$t2; fi
  fi
  echo "$b ${t:-0}"
}

# ---------------------------------------------------------------- 候选生成
gen_discover_candidates() {
  for pre in $PREFIXES; do
    for base in $BASE_PORTS; do
      i=$SCAN_FROM
      while [ "$i" -le "$SCAN_TO" ]; do
        port=$((base + 8 * i))
        if [ "$port" -ge 2 ] && [ "$port" -le 65534 ]; then
          echo "$pre.$i $port"
        fi
        i=$((i + 1))
      done
    done
  done
}

gen_verify_candidates() {
  scope="${1:-all}"
  [ -f "$DB" ] || return 0
  awk -F'\t' -v s="$scope" '
    function extra(   n, a, i, hp) {
      if ($9 == "") return
      n = split($9, a, "|")
      for (i = 1; i <= n; i++) {
        if (a[i] ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$/) {
          split(a[i], hp, ":")
          print hp[1] " " hp[2]
        }
      }
    }
    $3 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $4 ~ /^[0-9]+$/ {
      # 备用源总是要一起探测, 否则无法判断该提升谁
      if (s == "all")   { print $3 " " $4; extra(); next }
      if (s == "stale" && $5 != "1") { print $3 " " $4; extra(); next }
      if (s == "live"  && $5 == "1") { print $3 " " $4; extra(); next }
    }' "$DB"
}

# ---------------------------------------------------------------- 任务
run_job() {
  mode="$1"
  scope="${2:-all}"

  # 被停止时也要把已扫到的结果写回数据库
  FINISHED=0
  on_exit() {
    [ "$FINISHED" = "1" ] && return 0
    FINISHED=1
    echo "[$(date +%H:%M:%S)] 收到退出信号, 保存已扫结果" >> "$RUN/log" 2>/dev/null
    stop_scanners 2>/dev/null
    finish_job 2>/dev/null
    rm -f "$RUN/pid" 2>/dev/null
    echo stopped > "$RUN/state" 2>/dev/null
  }
  trap on_exit INT TERM HUP QUIT

  rm -f "$RUN/done."* "$RUN/found."* "$RUN/probed."* "$RUN/worker.pids" "$RUN/stop" "$RUN/log" 2>/dev/null
  echo "$mode" > "$RUN/mode"
  echo "$$"  > "$RUN/pid"
  echo 0     > "$RUN/total"
  echo running > "$RUN/state"

  if [ "$mode" = "discover" ]; then
    gen_discover_candidates > "$TMP/candidates.txt"
  else
    gen_verify_candidates "$scope" > "$TMP/candidates.txt"
  fi

  total=$(grep -c . "$TMP/candidates.txt" 2>/dev/null)
  total=${total:-0}
  echo "$total" > "$RUN/total"
  echo "[$(date +%H:%M:%S)] 任务=$mode 候选=$total 扫描器=$SCANNERS" >> "$RUN/log"

  if [ "$total" -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] 无候选，结束" >> "$RUN/log"
    echo done > "$RUN/state"
    rm -f "$RUN/pid"
    return 0
  fi

  n=$SCANNERS
  rm -f "$TMP"/chunk*
  awk -v n="$n" -v d="$TMP" '{ print > (d "/chunk" (NR % n)) }' "$TMP/candidates.txt"

  start_scanners

  w=0
  WPIDS=""
  while [ "$w" -lt "$n" ]; do
    port=$((SCAN_PORT_START + w))
    worker "$w" "$port" &
    WPIDS="$WPIDS $!"
    echo $! >> "$RUN/worker.pids"
    w=$((w + 1))
  done

  # 只等 worker，绝不能裸 wait（会连扫描器实例一起等）
  for p in $WPIDS; do wait "$p" 2>/dev/null; done
  stop_scanners
  finish_job
  FINISHED=1
  rm -f "$RUN/pid" 2>/dev/null
}

worker() {
  idx="$1"; port="$2"
  f="$TMP/chunk$idx"
  [ -f "$f" ] || { echo 0 > "$RUN/done.$idx"; return 0; }
  cnt=0
  while read -r ip uport; do
    [ -z "$ip" ] && continue
    [ -f "$RUN/stop" ] && break
    res=$(probe "$port" "$ip" "$uport")
    b=${res%% *}
    sec=${res##* }
    cnt=$((cnt + 1))
    echo "$cnt" > "$RUN/done.$idx"
    printf '%s:%s\n' "$ip" "$uport" >> "$RUN/probed.$idx"
    if [ "${b:-0}" -gt "$MIN_BYTES" ]; then
      printf '%s\t%s\t%s\t%s\n' "$ip" "$uport" "$b" "${sec:-1}" >> "$RUN/found.$idx"
    fi
  done < "$f"
}

finish_job() {
  mode=$(cat "$RUN/mode" 2>/dev/null)
  mode=${mode:-verify}
  ts=$(now_ts)

  cat "$RUN"/found.* > "$TMP/found-all.tsv" 2>/dev/null
  : > "$TMP/found-all.tsv"
  for f in "$RUN"/found.*; do
    [ -f "$f" ] && cat "$f" >> "$TMP/found-all.tsv"
  done

  : > "$TMP/live.txt"
  if [ -s "$TMP/found-all.tsv" ]; then
    awk -F'\t' 'NF>=2 {print $1":"$2}' "$TMP/found-all.tsv" | sort -u > "$TMP/live.txt"
  fi

  # 码率估算: 用实际耗时算, 避免两段式探测时算错
  : > "$TMP/kbps.txt"
  if [ -s "$TMP/found-all.tsv" ]; then
    awk -F'\t' -v t="$PROBE_TIMEOUT" '
      NF >= 3 && $3 > 0 {
        s = ($4 + 0)
        if (s <= 0.05) s = t
        printf "%s:%s %d\n", $1, $2, ($3 * 8 / s / 1000)
      }
    ' "$TMP/found-all.tsv" | sort -u > "$TMP/kbps.txt"
  fi

  : > "$TMP/probed.txt"
  for f in "$RUN"/probed.*; do
    [ -f "$f" ] && cat "$f" >> "$TMP/probed.txt"
  done
  sort -u "$TMP/probed.txt" -o "$TMP/probed.txt" 2>/dev/null

  : > "$TMP/db.new"
  if [ -f "$DB" ]; then
    awk -F'\t' -v OFS='\t' -v live="$TMP/live.txt" -v probed="$TMP/probed.txt" -v kb="$TMP/kbps.txt" -v ts="$ts" '
      BEGIN {
        while ((getline l < live) > 0) islive[l] = 1
        while ((getline l < probed) > 0) wasprobed[l] = 1
        while ((getline l < kb) > 0) { split(l, a, " "); kbps[a[1]] = a[2] }
      }
      NF >= 4 {
        key = $3 ":" $4
        if (key in islive) {
          $5 = "1"; $6 = ts
          if (key in kbps) $7 = kbps[key]
        }
        else if (key in wasprobed) { $5 = "0"; $6 = ts }

        # 主源失效但备用源里有存活的 -> 把第一个存活的提升为主源
        # (典型场景: 4K 源失效, 但高清备用源可用)
        if ($5 == "0" && $9 != "") {
          n = split($9, ex, "|")
          keep = ""; np = ""
          for (i = 1; i <= n; i++) {
            e = ex[i]
            if (e == "") continue
            if (np == "" && (e in islive)) { np = e; continue }
            keep = keep (keep == "" ? "" : "|") e
          }
          if (np != "") {
            keep = keep (keep == "" ? "" : "|") key   # 旧主源降为备用
            ipn = np; sub(/:.*/, "", ipn)
            ptn = np; sub(/^.*:/, "", ptn)
            $3 = ipn; $4 = ptn
            $5 = "1"; $6 = ts
            if (np in kbps) $7 = kbps[np]
            $9 = keep
          }
        }
        print
      }' "$DB" > "$TMP/db.new"

    if [ "$mode" = "discover" ] && [ -s "$TMP/found-all.tsv" ]; then
      awk -F'\t' '
        NR == FNR { ex[$3":"$4] = 1; next }
        NF >= 2 { key = $1":"$2; if (!(key in ex)) print key }
      ' "$DB" "$TMP/found-all.tsv" | sort -u > "$TMP/new-addrs.txt"
      n=0
      while read -r addr; do
        [ -z "$addr" ] && continue
        ip=${addr%%:*}
        pt=${addr##*:}
        kbv=$(awk -v k="$addr" '$1==k{print $2}' "$TMP/kbps.txt" 2>/dev/null | head -1)
        n=$((n + 1))
        printf '新发现-%s\t自动发现\t%s\t%s\t1\t%s\t%s\n' "$pt" "$ip" "$pt" "$ts" "${kbv:-0}" >> "$TMP/db.new"
      done < "$TMP/new-addrs.txt"
      echo "[$(date +%H:%M:%S)] 发现新增 $n 个频道" >> "$RUN/log"
    fi
    echo "[$(date +%H:%M:%S)] db_commit: DB=$DB exists=$([ -f "$DB" ] && echo yes || echo no) dbnew=$(wc -l < "$TMP/db.new" 2>/dev/null)" >> "$RUN/log"
    if db_commit "$TMP/db.new"; then
      echo "[$(date +%H:%M:%S)] db_commit OK live=$(grep -c '	1	' "$DB" 2>/dev/null)" >> "$RUN/log"
    else
      echo "[$(date +%H:%M:%S)] db_commit FAILED" >> "$RUN/log"
    fi
  else
    echo "[$(date +%H:%M:%S)] db_commit skipped: DB 不存在" >> "$RUN/log"
  fi

  dn=$(task_progress)
  fn=$(task_found_count)
  echo "[$(date +%H:%M:%S)] 完成: 检测 $dn / 存活 $fn" >> "$RUN/log"

  echo done > "$RUN/state"
  rm -f "$RUN/pid"
  build_playlists
}

stop_job() {
  touch "$RUN/stop" 2>/dev/null
  if [ -f "$RUN/worker.pids" ]; then
    while read -r pid; do kill "$pid" 2>/dev/null; done < "$RUN/worker.pids"
    rm -f "$RUN/worker.pids"
  fi

  # 先发 TERM 让主进程的 trap 执行 finish_job
  # 关键: 一旦检测到它开始收尾(saved 标记)，立刻释放 pid，允许马上重启
  if [ -f "$RUN/pid" ]; then
    mp=$(cat "$RUN/pid" 2>/dev/null)
    if [ -n "$mp" ] && kill -0 "$mp" 2>/dev/null; then
      kill -TERM "$mp" 2>/dev/null
      w=0
      while [ "$w" -lt 25 ] && kill -0 "$mp" 2>/dev/null; do
        sleep 1; w=$((w + 1))
        # 收尾完成就退出等待
        if grep -q 'db_commit OK\|db_commit FAILED' "$RUN/log" 2>/dev/null; then
          break
        fi
      done
      kill -9 "$mp" 2>/dev/null
    fi
  fi
  # 立刻释放 pid，让下一次 start 能通过 is_running 检查
  rm -f "$RUN/pid" 2>/dev/null

  stop_scanners
  echo stopped > "$RUN/state"
  echo "[$(date +%H:%M:%S)] 任务被手动停止" >> "$RUN/log"
}

# ---------------------------------------------------------------- EPG 映射
# EPG 映射不在盒子上计算。
#
# 原因: 盒子是单核 580MHz / 120MB 内存, 在本地用 awk 解 2.6MB 的 epg.xml 会 OOM 把系统
#       拖死 (2026-10-17 已实际发生, 只能断电重启)。
#
# 现在的做法: 在电脑上解析 EPG 得到 频道 -> (EPG id, display-name), 补进数据库第 11/12 列,
#             只把十几 KB 的结果文件传到盒子, 然后执行 iptv build。
#             盒子全程不做任何大文件解析。
#
# 播放器匹配顺序 (见 web-ui/src/lib/epg-parser.ts):
#     tvg-id -> tvg-name -> 频道名
# EPG 数据的 key 同时包含 <channel id> 和每个 <display-name>,
# 所以 m3u 里写上 tvg-id="<id>" 即可命中, 无需在盒子上做任何名字归一。


# ---------------------------------------------------------------- 播放列表
# 按 seq (第 8 列) 排序输出数据库; 没有 seq 的按原始顺序排最后
db_sorted() {
  [ -f "$DB" ] || return 0
  awk -F'\t' '{ s = ($8 == "" ? 999999 : $8 + 0); printf "%09d%s%09d%s%s\n", s, "\t", NR, "\t", $0 }' "$DB" \
    | sort -t"$TAB" -k1,1n -k2,2n | cut -f3-
}

build_playlists() {
  [ -f "$DB" ] || return 0
  mkdir -p "$PLAYER_BASE" 2>/dev/null
  db_sorted > "$TMP/db.sorted"

  # FCC 查询串
  if [ "$FCC_ENABLE" = "1" ] && [ -n "$FCC_ADDR" ]; then
    if [ -n "$FCC_TYPE" ] && [ "$FCC_TYPE" != "telecom" ]; then
      FCCQ="?fcc=$FCC_ADDR&fcc-type=$FCC_TYPE"
    else
      FCCQ="?fcc=$FCC_ADDR"
    fi
  else
    FCCQ=""
  fi

  # 时移回看模板 (仅在启用且填写时生效)
  if [ "$RTSP_ENABLE" = "1" ]; then CU="$RTSP_TMPL"; else CU=""; fi

  # 生成统一「条目表」: rank / isbackup / order / group / name / ip / port
  # 分组规则 (每个源按自身清晰度归组):
  #   央视频道  4K -> 央视超高清 ; 标清 -> 央视标清 ; 其它 -> 央视频道
  #   卫视频道  4K -> 卫视超高清 ; 标清 -> 卫视标清 ; 其它 -> 卫视频道
  #   安徽频道  标清 -> 地方标清 ; 其它 -> 地方频道
  # 命名规则: 不出现「备用」字样, 改用清晰度/帧率区分
  #   4K 源            -> "<频道名> (50p)" / "<频道名> (25p)"
  #   非 4K 的备用源    -> "<频道名> 高清" / "<频道名> 标清"
  #   同名同标签有多条  -> 追加序号 1/2/3
  awk -F'\t' -v OFS='\t' '
    function base(c,   b) { b = (c == "" ? "未分组" : c); if (b == "安徽频道") b = "地方频道"; return b }
    function bgrp(c, q,   b) {
      b = base(c)
      if (c == "央视频道") {
        if (q ~ /^4K/)   return "央视超高清"
        if (q == "标清") return "央视标清"
        return "央视频道"
      }
      if (c == "卫视频道") {
        if (q ~ /^4K/)   return "卫视超高清"
        if (q == "标清") return "卫视标清"
        return "卫视频道"
      }
      if (c == "安徽频道" && q == "标清") return "地方标清"
      return b
    }
    function tagof(q) {
      if (q == "4K50") return "(50p)"
      if (q ~ /^4K/)   return "(25p)"
      if (q == "标清") return "标清"
      if (q == "高清") return "高清"
      return ""
    }
    BEGIN {
      rank["央视频道"]=1; rank["央视超高清"]=2; rank["卫视超高清"]=3
      rank["卫视频道"]=4; rank["数字频道"]=5; rank["地方频道"]=6
      rank["央视标清"]=7; rank["卫视标清"]=8; rank["地方标清"]=9
      rank["其他频道"]=10; rank["自动发现"]=11; rank["未分组"]=12
    }
    NF >= 4 && $4 ~ /^[0-9]+$/ {
      if ($5 == "0") next
      nm = ($1 == "" ? "频道-" $4 : $1)
      NS = 1
      SP[nm, 1] = $3; ST[nm, 1] = $4; SQ[nm, 1] = $10
      if ($9 != "") {
        n = split($9, ex, "|")
        for (i = 1; i <= n; i++) {
          if (ex[i] == "") continue
          a = ex[i]; q = ""
          p = index(a, "@")
          if (p > 0) { q = substr(a, p + 1); a = substr(a, 1, p - 1) }
          split(a, hh, ":")
          NS++; SP[nm, NS] = hh[1]; ST[nm, NS] = hh[2]; SQ[nm, NS] = q
        }
      }
      NSRC[nm] = NS; CAT[nm] = $2; SEQ[nm] = (($8 == "" || $8 == "0") ? 999999 : $8 + 0)
      TID[nm] = $11; TNAME[nm] = $12
      if (!(nm in seen)) { seen[nm] = 1; ORD[++nn] = nm }
      for (k = 2; k <= NS; k++) { t = tagof(SQ[nm, k]); if (t != "") TC[nm SUBSEP t]++ }
      next
    }
    END {
      for (oi = 1; oi <= nn; oi++) {
        nm = ORD[oi]; ns = NSRC[nm]; cat = CAT[nm]; s = SEQ[nm]
        for (k = 1; k <= ns; k++) {
          q = SQ[nm, k]; t = tagof(q)
          if (k == 1) {
            disp = nm
            if (q ~ /^4K/) disp = nm " " t          # 主源是 4K 时带上帧率
          } else {
            if (t == "") t = "源"
            if (TC[nm SUBSEP t] > 1) { TC2[nm SUBSEP t]++; disp = nm " " t TC2[nm SUBSEP t] }
            else disp = nm " " t
          }
          g = bgrp(cat, q); r = (g in rank) ? rank[g] : 50
          printf "%03d\t%d\t%09d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", r, (k == 1 ? 0 : 1), s * 1000 + k, g, disp, SP[nm, k], ST[nm, k], TID[nm], TNAME[nm], nm
          # 央视超高清 的条目在「央视频道」里也保留一份 (允许重复)
          if (g == "央视超高清")
            printf "%03d\t%d\t%09d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", rank["央视频道"], (k == 1 ? 0 : 1), s * 1000 + k, "央视频道", disp, SP[nm, k], ST[nm, k], TID[nm], TNAME[nm], nm
        }
      }
    }' "$TMP/db.sorted" | sort -t"$TAB" -k1,1n -k2,2n -k3,3n > "$TMP/entries.tsv"

  # 1) udpxy 转单播
  {
    printf '#EXTM3U x-tvg-url="%s"\n' "$EPG_URL"
    awk -F'\t' -v h="$PUBLIC_HOST" -v p="$MSD_PORT" -v lg="$LOGO_BASE" '
      function short(n,   a) { if (split(n, a, /[ ]+/) > 1) return a[1]; return n }
      { extra = ""
        ida = ($8 != "" ? " tvg-id=\"" $8 "\"" : "")
        tvm = ($9 != "" ? $9 : short($5))
        nice = ($10 != "" ? $10 : $5)
        if (nice != $5) extra = extra " display-name=\"" $5 "\""
        printf "#EXTINF:-1%s tvg-name=\"%s\" tvg-logo=\"%s/%s.png\" group-title=\"%s\"%s,%s\n", ida, tvm, lg, short($5), $4, extra, nice
        printf "http://%s:%s/rtp/%s:%s\n", h, p, $6, $7 }' "$TMP/entries.tsv"
  } > "$PLAYER_BASE/iptv.m3u"

  # 2) 原始组播 —— 喂给 rtp2httpd 的 external-m3u (Web 播放器的频道来源)
  #    tvg-id / tvg-name 是播放器匹配 EPG 的依据, 必须带上
  {
    printf '#EXTM3U x-tvg-url="%s"\n' "$EPG_URL"
    awk -F'\t' -v fccq="$FCCQ" -v cu="$CU" '
      { extra = ""
        if ($2 == "0" && cu != "") { c = cu; gsub(/\{name\}/, $5, c); extra = " catchup=\"default\" catchup-source=\"" c "\"" }
        ida = ($8 != "" ? " tvg-id=\"" $8 "\"" : "")
        nma = ($9 != "" ? " tvg-name=\"" $9 "\"" : "")
        nice = ($10 != "" ? $10 : $5)
        if (nice != $5) extra = extra " display-name=\"" $5 "\""
        printf "#EXTINF:-1%s%s group-title=\"%s\"%s,%s\n", ida, nma, $4, extra, nice
        printf "rtp://%s:%s%s\n", $6, $7, fccq }' "$TMP/entries.tsv"
  } > "$PLAYER_BASE/iptv-rtp.m3u"

  # 3) rtp2httpd 转单播 (端口 5140)
  {
    printf '#EXTM3U x-tvg-url="%s"\n' "$EPG_URL"
    awk -F'\t' -v h="$PUBLIC_HOST" -v p="$R2H_PORT" -v lg="$LOGO_BASE" -v fccq="$FCCQ" -v cu="$CU" '
      function short(n,   a) { if (split(n, a, /[ ]+/) > 1) return a[1]; return n }
      { extra = ""
        if ($2 == "0" && cu != "") { c = cu; gsub(/\{name\}/, $5, c); extra = " catchup=\"default\" catchup-source=\"" c "\"" }
        ida = ($8 != "" ? " tvg-id=\"" $8 "\"" : "")
        tvm = ($9 != "" ? $9 : short($5))
        nice = ($10 != "" ? $10 : $5)
        if (nice != $5) extra = extra " display-name=\"" $5 "\""
        printf "#EXTINF:-1%s tvg-name=\"%s\" tvg-logo=\"%s/%s.png\" group-title=\"%s\"%s,%s\n", ida, tvm, lg, short($5), $4, extra, nice
        printf "http://%s:%s/rtp/%s:%s%s\n", h, p, $6, $7, fccq }' "$TMP/entries.tsv"
  } > "$PLAYER_BASE/iptv-r2h.m3u"

  # 4) TVBox / DIYP 格式 (udpxy)
  awk -F'\t' -v h="$PUBLIC_HOST" -v p="$MSD_PORT" '
    { if ($4 != last) { printf "%s,#genre#\n", $4; last = $4 }
      printf "%s,http://%s:%s/rtp/%s:%s\n", $5, h, p, $6, $7 }' "$TMP/entries.tsv" > "$PLAYER_BASE/iptv.txt"

  # 镜像到第二个 docroot
  if [ -n "$MIRROR_BASE" ] && [ "$MIRROR_BASE" != "$PLAYER_BASE" ]; then
    mkdir -p "$MIRROR_BASE" 2>/dev/null
    for f in iptv.m3u iptv-rtp.m3u iptv-r2h.m3u iptv.txt; do
      [ -f "$PLAYER_BASE/$f" ] && cp "$PLAYER_BASE/$f" "$MIRROR_BASE/$f" 2>/dev/null
    done
  fi

  # 让 rtp2httpd 重新加载外部 M3U (它按需读取, 这里触发一次即可)
  [ "$R2H_ENABLE" = "1" ] && r2h_reload >/dev/null 2>&1

  if [ "$LEGACY_NAMES" = "1" ]; then
    for d in "$PLAYER_BASE" "$MIRROR_BASE"; do
      [ -d "$d" ] || continue
      [ -f "$PLAYER_BASE/iptv.m3u" ]     && cp "$PLAYER_BASE/iptv.m3u"     "$d/new.m3u"     2>/dev/null
      [ -f "$PLAYER_BASE/iptv.txt" ]     && cp "$PLAYER_BASE/iptv.txt"     "$d/new.txt"     2>/dev/null
      [ -f "$PLAYER_BASE/iptv-rtp.m3u" ] && cp "$PLAYER_BASE/iptv-rtp.m3u" "$d/anycast.m3u" 2>/dev/null
    done
  fi
}

# ---------------------------------------------------------------- rtp2httpd
r2h_running() {
  [ "$R2H_ENABLE" = "1" ] || return 1
  netstat -lnt 2>/dev/null | grep -q ":$R2H_PORT "
}

r2h_reload() {
  pid=$(cat /var/run/rtp2httpd_*.pid 2>/dev/null | head -1)
  [ -n "$pid" ] && kill -HUP "$pid" 2>/dev/null
  return 0
}

# 探测 FCC: 借道 rtp2httpd 请求一个已知频道, 读 R2H-FCC-Status 响应头
fcc_probe() {
  addr="$1"; type="$2"; testch="$3"
  [ -n "$addr" ] || return 1
  [ -n "$testch" ] || testch="238.1.78.166:7200"
  [ -n "$type" ] || type="telecom"
  r2h_running || { echo "r2h_off"; return 1; }
  hdr=$(curl -s -o /dev/null -D - --max-time 6 \
        "http://127.0.0.1:$R2H_PORT/rtp/$testch?fcc=$addr&fcc-type=$type" 2>/dev/null)
  st=$(printf '%s' "$hdr" | grep -i '^R2H-FCC-Status:' | tr -d '\r' | awk '{print $2}')
  ty=$(printf '%s' "$hdr" | grep -i '^R2H-FCC-Type:'   | tr -d '\r' | awk '{print $2}')
  if [ "$st" = "active" ]; then
    echo "active $ty"
  elif [ "$st" = "fallback" ]; then
    echo "fallback $ty"
  else
    echo "unknown"
  fi
}

# ---------------------------------------------------------------- 播放列表解析
# stdin 读入 m3u / TVBox txt，输出 TSV: name group ip port
parse_playlist() {
  awk '
    function emit(nm, grp, url,   best, rest, cand, o, ip, pt) {
      gsub(/\r/, "", nm); gsub(/\r/, "", grp)
      gsub(/\t/, " ", nm); gsub(/\t/, " ", grp)
      if (nm == "") nm = "未命名"
      # 取 URL 中最后一个 ip:port，且必须是组播地址(224-239)
      best = ""; rest = url
      while (match(rest, /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+/)) {
        cand = substr(rest, RSTART, RLENGTH)
        split(cand, o, "[.]")
        if ((o[1] + 0) >= 224 && (o[1] + 0) <= 239) best = cand
        rest = substr(rest, RSTART + RLENGTH)
      }
      if (best == "") return
      ip = best; sub(/:.*/, "", ip)
      pt = best; sub(/^.*:/, "", pt)
      if (ip == "" || pt == "") return
      printf "%s\t%s\t%s\t%s\n", nm, grp, ip, pt
    }
    /^#EXTINF/ {
      nm = $0; sub(/^.*,/, "", nm)
      grp = ""
      if (match($0, /group-title="[^"]*"/)) grp = substr($0, RSTART + 13, RLENGTH - 14)
      if ((getline url) > 0) emit(nm, grp, url)
      next
    }
    /^[ \t]*#/ { next }
    /^[ \t]*$/ { next }
    /,#genre#/ {
      # TVBox / DIYP 格式的分组行: 央视,#genre#
      g = $0; sub(/,.*/, "", g); gsub(/\r/, "", g)
      curgrp = g
      next
    }
    {
      line = $0; gsub(/\r/, "", line)
      if (index(line, ",") == 0) next
      nm = line;  sub(/,.*/, "", nm)
      url = line; sub(/^[^,]*,/, "", url)
      if (url ~ /^[a-zA-Z]+:\/\//) emit(nm, curgrp, url)
    }
  '
}

# ---------------------------------------------------------------- CGI
cgi() {
  qs="$QUERY_STRING"

  getp() {
    printf '%s' "$qs" | tr '&' '\n' | grep "^$1=" | head -1 | cut -d= -f2-
  }

  read_body() {
    len="${CONTENT_LENGTH:-0}"
    case "$len" in ''|*[!0-9]*) len=0 ;; esac
    if [ "$len" -gt 0 ]; then dd bs=1 count="$len" 2>/dev/null; fi
  }

  hdr() {
    printf 'Content-Type: %s; charset=utf-8\r\n' "${1:-application/json}"
    printf 'Cache-Control: no-store\r\n\r\n'
  }

  a=$(getp a)

  case "$a" in

  state)
    hdr
    running=0; is_running && running=1
    base=0; live=0; dead=0; unk=0
    if [ -f "$DB" ]; then
      base=$(grep -c . "$DB" 2>/dev/null); base=${base:-0}
      set -- $(awk -F'\t' '
        { if ($5=="1") l++; else if ($5=="0") d++; else u++ }
        END { printf "%d %d %d", l+0, d+0, u+0 }' "$DB")
      live=$1; dead=$2; unk=$3
    fi
    total=$(cat "$RUN/total" 2>/dev/null); total=${total:-0}
    dbfail=""
    [ -s "$DB.fail" ] && dbfail=$(tail -1 "$DB.fail" 2>/dev/null | sed 's/"/\"/g')
    dn=$(task_progress)
    fn=$(task_found_count)
    mode=$(cat "$RUN/mode" 2>/dev/null)
    log=$(tail -4 "$RUN/log" 2>/dev/null | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' '|' | tr -d '\r')
    printf '{"ok":1,"running":%s,"mode":"%s","total":%s,"done":%s,"found":%s,' \
      "$running" "$mode" "$total" "$dn" "$fn"
    printf '"counts":{"all":%s,"live":%s,"dead":%s,"unknown":%s},"log":"%s",' \
      "$base" "$live" "$dead" "$unk" "$log"
    printf '"cfg":{"iface":"%s","msd_port":"%s","public_host":"%s","scanners":"%s","probe_timeout":"%s",' \
      "$IFACE" "$MSD_PORT" "$PUBLIC_HOST" "$SCANNERS" "$PROBE_TIMEOUT"
    printf '"min_bytes":"%s","prefixes":"%s","base_ports":"%s","scan_from":"%s","scan_to":"%s",' \
      "$MIN_BYTES" "$PREFIXES" "$BASE_PORTS" "$SCAN_FROM" "$SCAN_TO"
    printf '"epg_url":"%s","logo_base":"%s","legacy":"%s",' \
      "$EPG_URL" "$LOGO_BASE" "$LEGACY_NAMES"
    printf '"r2h_enable":"%s","r2h_port":"%s","r2h_token":"%s","fcc_addr":"%s","fcc_type":"%s",' \
      "$R2H_ENABLE" "$R2H_PORT" "$R2H_TOKEN" "$FCC_ADDR" "$FCC_TYPE"
    printf '"fcc_enable":"%s","rtsp_enable":"%s","rtsp_tmpl":"%s"}}\n' \
      "$FCC_ENABLE" "$RTSP_ENABLE" "$RTSP_TMPL"
    ;;

  list)
    hdr
    if [ -f "$DB" ]; then
      # 必须用 db_sorted: 数据库文件的行顺序可能被打乱, 真正顺序由第 8 列 seq 决定
      db_sorted > "$TMP/db.sorted" 2>/dev/null
      awk -F'\t' '
        function esc(s) { gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); gsub(/\r/,"",s); gsub(/\t/," ",s); return s }
        BEGIN { printf "{\"ok\":1,\"items\":["; first=1 }
        NF >= 4 && $4 ~ /^[0-9]+$/ {
          if (!first) printf ","
          first=0
          printf "[\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]",
                 esc($1), esc($2), $3, $4, ($5==""?"-1":$5), ($6==""?"0":$6), ($7==""?"0":$7),
                 ($8==""?"0":$8), esc($9)
        }
        END { printf "]}\n" }
      ' "$TMP/db.sorted"
    else
      printf '{"ok":1,"items":[]}\n'
    fi
    ;;

  start)
    mode=$(getp mode); [ -z "$mode" ] && mode=verify
    scope=$(getp scope); [ -z "$scope" ] && scope=all
    hdr
    if is_running; then
      printf '{"ok":0,"msg":"已有任务正在运行"}\n'
    else
      rm -f "$RUN/stop" "$RUN/state" "$RUN/pid"
      started=0
      if command -v start-stop-daemon >/dev/null 2>&1; then
        start-stop-daemon -S -b -q -m -p "$RUN/pid" -x /bin/sh -- \
          "$SELF" runjob "$mode" "$scope" 2>/dev/null && started=1
      fi
      sleep 1
      if [ "$started" = "0" ] || ! is_running; then
        # 兜底：直接后台启动，并自己写 pid
        ( /bin/sh "$SELF" runjob "$mode" "$scope" >/dev/null 2>&1 & echo $! > "$RUN/pid" )
        sleep 2
      fi
      if is_running; then
        printf '{"ok":1}\n'
      else
        printf '{"ok":0,"msg":"\u542f\u52a8\u5931\u8d25\uff08\u5f15\u64ce\u8def\u5f84 %s\uff09"}\n' "$SELF"
      fi
    fi
    ;;

  stop)
    hdr
    stop_job
    printf '{"ok":1}\n'
    ;;

  gen)
    hdr
    build_playlists
    printf '{"ok":1}\n'
    ;;

  r2hinfo)
    hdr
    r=0; r2h_running && r=1
    epgc=0; chc=0
    if [ "$r" = "1" ]; then
      epgc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$R2H_PORT/epg.xml" 2>/dev/null)
      chc=$(curl -s --max-time 8 "http://127.0.0.1:$R2H_PORT/playlist.m3u" 2>/dev/null | grep -c '^#EXTINF')
    fi
    printf '{"ok":1,"running":%s,"port":"%s","epg_http":%s,"channels":%s,"player":"http://%s:%s/player","status":"http://%s:%s/status","epg":"http://%s:%s/epg.xml"}\n' \
      "$r" "$R2H_PORT" "${epgc:-0}" "${chc:-0}" "$PUBLIC_HOST" "$R2H_PORT" "$PUBLIC_HOST" "$R2H_PORT" "$PUBLIC_HOST" "$R2H_PORT"
    ;;

  fccprobe)
    addr=$(getp addr); typ=$(getp type); ch=$(getp ch)
    hdr
    res=$(fcc_probe "$addr" "$typ" "$ch")
    st=${res%% *}
    ty=${res##* }
    printf '{"ok":1,"addr":"%s","status":"%s","fcc_type":"%s"}\n' "$addr" "$st" "$ty"
    ;;

  r2happly)
    hdr
    S='rtp2httpd.@rtp2httpd[0]'
    if command -v uci >/dev/null 2>&1 && uci -q get rtp2httpd >/dev/null 2>&1; then
      if [ "$R2H_ENABLE" = "1" ]; then
        uci set $S.disabled='0'
        uci set $S.advanced_interface_settings='1'
        uci set $S.upstream_interface_multicast="$IFACE"
        uci -q delete $S.upstream_interface
        uci set $S.maxclients='20'
        uci set $S.external_m3u="file://$PLAYER_BASE/iptv-rtp.m3u"
        uci set $S.status_page_path='/status'
        uci set $S.player_page_path='/player'
        uci set $S.cors_allow_origin='*'
        [ -n "$R2H_PORT" ] && { uci set $S.port="$R2H_PORT"; uci -q delete $S.listen; }
        if [ -n "$R2H_TOKEN" ]; then uci set $S.r2h_token="$R2H_TOKEN"; else uci -q delete $S.r2h_token; fi
      else
        uci set $S.disabled='1'
      fi
      uci commit rtp2httpd
      /etc/init.d/rtp2httpd restart >/dev/null 2>&1
      sleep 2
      build_playlists
    fi
    r=0; r2h_running && r=1
    printf '{"ok":1,"running":%s,"port":"%s"}\n' "$r" "$R2H_PORT"
    ;;

  sort)
    hdr
    read_body > "$TMP/order.txt"
    awk -F'\t' -v OFS='\t' -v ord="$TMP/order.txt" '
      BEGIN { while ((getline l < ord) > 0) { gsub(/\r/, "", l); if (l != "") { n++; seq[l] = n } } }
      NF >= 4 { key = $3":"$4; if (key in seq) $8 = seq[key]; print }
    ' "$DB" > "$TMP/db.new"
    db_commit "$TMP/db.new"
    build_playlists
    printf '{"ok":1,"ordered":%d}\n' "${n:-0}"
    ;;

  src)
    hdr
    read_body > "$TMP/src.tsv"
    awk -F'\t' -v OFS='\t' -v ed="$TMP/src.tsv" '
      BEGIN {
        while ((getline l < ed) > 0) {
          gsub(/\r/, "", l); split(l, a, "\t")
          k = a[2]":"a[3]
          if (a[1] == "set") v[k] = a[4]
        }
      }
      NF >= 4 { key = $3":"$4; if (key in v) $9 = v[key]; print }
    ' "$DB" > "$TMP/db.new"
    db_commit "$TMP/db.new"
    build_playlists
    printf '{"ok":1}\n'
    ;;

  export)
    fmt=$(getp fmt); [ -z "$fmt" ] && fmt=m3u
    build_playlists
    case "$fmt" in
      rtp) f="$PLAYER_BASE/iptv-rtp.m3u"; ct="audio/x-mpegurl"; fn="iptv-rtp.m3u" ;;
      txt) f="$PLAYER_BASE/iptv.txt";     ct="text/plain";      fn="iptv.txt" ;;
      tsv) f="$DB";                       ct="text/plain";      fn="channels.tsv" ;;
      *)   f="$PLAYER_BASE/iptv.m3u";     ct="audio/x-mpegurl"; fn="iptv.m3u" ;;
    esac
    printf 'Content-Type: %s; charset=utf-8\r\n' "$ct"
    printf 'Content-Disposition: attachment; filename="%s"\r\n\r\n' "$fn"
    cat "$f" 2>/dev/null
    ;;

  import)
    merge=$(getp merge); [ -z "$merge" ] && merge=1
    hdr
    read_body > "$TMP/import.raw"
    parse_playlist < "$TMP/import.raw" > "$TMP/import.tsv"
    imp=$(grep -c . "$TMP/import.tsv" 2>/dev/null); imp=${imp:-0}
    ts=$(now_ts)
    if [ "$merge" = "1" ] && [ -f "$DB" ]; then
      awk -F'\t' -v OFS='\t' -v imp="$TMP/import.tsv" -v ts="$ts" '
        BEGIN {
          while ((getline l < imp) > 0) {
            split(l, a, "\t"); new[a[3]":"a[4]] = a[1] "\t" a[2]
          }
        }
        NF >= 4 {
          key = $3":"$4
          if (key in new) {
            split(new[key], b, "\t")
            if (b[1] != "") $1 = b[1]
            if (b[2] != "") $2 = b[2]
            $5 = "-1"; $6 = ts
            delete new[key]
          }
          print
        }
        END {
          for (k in new) {
            split(k, a, ":")
            split(new[k], b, "\t")
            printf "%s\t%s\t%s\t%s\t-1\t%s\t0\n", b[1], b[2], a[1], a[2], ts
          }
        }' "$DB" > "$TMP/db.new"
    else
      awk -F'\t' -v OFS='\t' -v ts="$ts" '
        { printf "%s\t%s\t%s\t%s\t-1\t%s\t0\n", $1, ($2==""?"未分组":$2), $3, $4, ts }' \
        "$TMP/import.tsv" > "$TMP/db.new"
    fi
    db_commit "$TMP/db.new"
    build_playlists
    printf '{"ok":1,"parsed":%s}\n' "$imp"
    ;;

  edit)
    hdr
    read_body > "$TMP/edit.tsv"
    ts=$(now_ts)
    awk -F'\t' -v OFS='\t' -v ed="$TMP/edit.tsv" '
      BEGIN {
        while ((getline l < ed) > 0) {
          split(l, a, "\t")
          k = a[4]":"a[5]
          if (a[1] == "del") { del[k] = 1; continue }
          if (a[1] == "set") { nm[k] = a[2]; grp[k] = a[3] }
        }
      }
      NF >= 4 {
        key = $3":"$4
        if (key in del) next
        if (key in nm) {
          if (nm[key]  != "") $1 = nm[key]
          if (grp[key] != "") $2 = grp[key]
        }
        print
      }' "$DB" > "$TMP/db.new"
    db_commit "$TMP/db.new"
    build_playlists
    printf '{"ok":1}\n'
    ;;

  add)
    hdr
    read_body > "$TMP/add.tsv"
    ts=$(now_ts)
    awk -F'\t' -v OFS='\t' -v ts="$ts" '
      NF >= 4 { printf "%s\t%s\t%s\t%s\t-1\t%s\t0\n", $1, ($2==""?"未分组":$2), $3, $4, ts }' \
      "$TMP/add.tsv" >> "$DB"
    build_playlists
    printf '{"ok":1}\n'
    ;;

  savecfg)
    hdr
    read_body > "$TMP/cfg.tsv"
    # 注意: 最后一行可能没有换行符, read 会返回非 0, 用 || [ -n "$k" ] 兜住
    while IFS="$TAB" read -r k v || [ -n "$k" ]; do
      case "$k" in
        SCANNERS|SCAN_PORT_START|SCAN_FROM|SCAN_TO|MIN_BYTES|R2H_PORT|CATCHUP_DAYS)
          case "$v" in ''|*[!0-9]*) continue ;; esac ;;
        PROBE_TIMEOUT|PROBE_TIMEOUT_SLOW)
          case "$v" in ''|*[!0-9.]*) continue ;; esac ;;
      esac
      v=$(printf '%s' "$v" | tr -d "'\"\\" | tr -d '\r')
      case "$k" in
        IFACE|MSD_PORT|MSD_HOST|PUBLIC_HOST|SCAN_PORT_START|SCANNERS|PROBE_TIMEOUT|PROBE_TIMEOUT_SLOW|\
        MIN_BYTES|PREFIXES|BASE_PORTS|SCAN_FROM|SCAN_TO|EPG_URL|LOGO_BASE|LEGACY_NAMES|\
        R2H_ENABLE|R2H_PORT|R2H_TOKEN|R2H_CORS|FCC_ADDR|FCC_TYPE|FCC_ENABLE|\
        RTSP_ENABLE|RTSP_TMPL|CATCHUP_DAYS)
          eval "$k='$v'" ;;
      esac
    done < "$TMP/cfg.tsv"
    cfg_write
    printf '{"ok":1}\n'
    ;;

  *)
    hdr
    printf '{"ok":0,"msg":"unknown action: %s"}\n' "$a"
    ;;
  esac
}

# ---------------------------------------------------------------- 入口
case "$1" in
  cgi)      cgi ;;
  runjob)   run_job "$2" "$3" ;;
  build)    build_playlists ;;
  epgmap)
    # 映射已在电脑上算好并写入数据库第 11/12 列, 这里只重建播放列表
    build_playlists
    printf 'epg-mapped: %s\n' "$(awk -F'\t' '$11 != ""' "$DB" 2>/dev/null | grep -c .)"
    ;;
  resort)
    # 重排数据库: 先按分组归类, 组内保持原 seq 顺序, 最后把 seq 重编为 1..N
    db_sorted > "$TMP/db.re" 2>/dev/null
    if [ -s "$TMP/db.re" ]; then
      awk -F'\t' '
        BEGIN {
          rank["央视频道"]=1; rank["超高清"]=2; rank["卫视频道"]=3; rank["央视付费"]=4;
          rank["安徽频道"]=5; rank["安徽地方"]=5; rank["地方卫视"]=6; rank["数字频道"]=7;
          rank["数字电视"]=7; rank["其他频道"]=8; rank["轮播"]=9; rank["自动发现"]=99
        }
        { g = $2; r = (g in rank) ? rank[g] : 50
          printf "%03d\t%06d\t%s\n", r, NR, $0 }' "$TMP/db.re" \
        | sort -t"$TAB" -k1,1n -k2,2n | cut -f3- > "$TMP/db.re3"
      awk -F'\t' -v OFS='\t' '{ $8 = NR; print }' "$TMP/db.re3" > "$TMP/db.re2"
      db_commit "$TMP/db.re2"
      build_playlists
      echo "resorted: $(grep -c . "$DB") rows"
    else
      echo "resort: empty, skipped" >&2
      exit 1
    fi
    ;;
  stop)     stop_job ;;
  seed)
    f="$2"
    [ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }
    ts=$(now_ts)
    parse_playlist < "$f" \
      | awk -F'\t' 'NF>=4 && !seen[$3":"$4]++' \
      | awk -F'\t' -v OFS='\t' -v ts="$ts" '
          { printf "%s\t%s\t%s\t%s\t-1\t%s\t0\n", $1, ($2==""?"未分组":$2), $3, $4, ts }' > "$TMP/seed.tsv"
    db_commit "$TMP/seed.tsv"
    n=$(grep -c . "$DB" 2>/dev/null); n=${n:-0}
    build_playlists
    echo "已导入 $n 个频道"
    ;;
  status)
    r=0; is_running && r=1
    echo "running=$r mode=$(cat "$RUN/mode" 2>/dev/null) total=$(cat "$RUN/total" 2>/dev/null) done=$(task_progress) found=$(task_found_count)"
    ;;
  *)
    echo "usage: iptv {cgi|runjob <mode> <scope>|build|stop|status}" >&2
    exit 1
    ;;
esac
