# IPTV 配置模板
# 用法：复制为 /data/iptv/etc/config.sh，按实际网络修改。
# 本文件由 /data/iptv/iptv 与 /data/iptv/start.sh 读取。

PREFIX='/data/iptv'

# 承载 IPTV 组播的 VLAN 接口（由 start.sh 创建）
IFACE='eth1.43'

# 组播转单播服务（udpxy）监听地址与端口
MSD_PORT='12123'
MSD_HOST='127.0.0.1'

# 局域网侧地址：写进 m3u 与播放器链接，需为设备可达的地址
PUBLIC_HOST='10.0.0.1'

# 频道扫描：端口范围、并发数、探针超时与最小有效字节
SCAN_PORT_START='12130'
SCANNERS='4'
PROBE_TIMEOUT='1'
PROBE_TIMEOUT_SLOW='5'
MIN_BYTES='20000'

# 扫描用的组播前缀与基础端口，按运营商组播规划填写
PREFIXES='238.1.78 238.1.79'
BASE_PORTS='5872 4112 5928 4136'
SCAN_FROM='0'
SCAN_TO='255'

# 播放器与控制台页面
PLAYER_BASE='/data/iptv/www/iptv'
MIRROR_BASE=''

# 节目单与台标
EPG_URL=''
LOGO_BASE='https://logo.catvod.com'

# 频道名显示：0 = 使用运营商原名
LEGACY_NAMES='0'

# 网页播放引擎 rtp2httpd
R2H_ENABLE='1'
R2H_PORT='5140'
R2H_TOKEN=''
R2H_CORS='1'

# FCC 快速换台（按需开启）
FCC_ADDR=''
FCC_TYPE='telecom'
FCC_ENABLE='0'

# RTSP 回看模板与回看天数
RTSP_ENABLE='0'
RTSP_TMPL=''
CATCHUP_DAYS='7'
