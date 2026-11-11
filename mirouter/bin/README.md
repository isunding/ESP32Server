# 二进制组件

| 文件 | 版本 | 来源 | SHA-256 |
|---|---|---|---|
| `udpxy` | 1.0-23.2（OpenWrt 官方 ipk，ARM 软浮点） | OpenWrt 18.06 `udpxy` 包解包 | `9b508f550786206c7ec37497a38fe28bae7113492a888ea9dd35e363340ad9b0` |
| `rtp2httpd` | 3.17.2 | Zig 交叉编译，目标 `arm-linux-musleabihf` | `fe26e0d22d2f9b6b5da25d4e8a652bf6a58b19684baa8d74156f63657a5daf2c` |

两者均为 32 位 ARM（EABI5，小端）静态可执行文件，运行于 Redmi AX3000（RA81）。
`udpxy` 承担组播转单播与频道扫描；`rtp2httpd` 承担网页播放器、状态面板与 EPG。

## 运行参数

```sh
# 组播转单播，源地址为 IPTV VLAN
udpxy -p 12123 -m 192.168.43.3 -c 10 -B 65536 -T -S

# 网页播放引擎
rtp2httpd -l 0.0.0.0:5140 -r eth1.43 -m 10 -B 524288 -O '*' \
  -M file:///data/iptv/www/iptv/iptv-r2h.m3u -I 0 --pid-file /var/run/rtp2httpd.pid
```

`-O '*'` 放开 CORS，电视与手机端跨域拉流需要；`-M` 指向播放列表，由 `iptv build` 生成。
两者都由 `iptv/start.sh` 按 `pidof` 判断后拉起，cron 每分钟保活。

## 为什么用 udpxy 而不是 msd_lite

`msd_lite` 依赖 `timerfd`，该内核未编入（`sys_timerfd_create` 返回 ENOSYS），启动即退出。
`udpxy` 功能等价，同时可代跑频道扫描实例。

## 许可

两个程序均遵循 GNU GPL v2，许可证全文见 `LICENSE-GPL-2.0`。

- rtp2httpd：https://github.com/stackia/rtp2httpd
- udpxy：http://www.udpxy.com/

按 GPL-2.0 要求，对应源码可从上述地址取得；如需与上表二进制完全一致的构建，
rtp2httpd 使用 Zig 交叉编译（`zig build -Dtarget=arm-linux-musleabihf`），
udpxy 使用 OpenWrt 18.06 的 `udpxy` 包源码。
