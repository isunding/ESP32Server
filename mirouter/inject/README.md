# 原厂管理页注入

原厂管理页（`/www`）为单页应用，入口按钮通过替换两个静态文件注入：
把 `patch.js` 追加到原厂文件末尾，再由 nginx 把对应 URL 指向替换后的副本。

| 原厂文件 | 替换为 | nginx 规则 |
|---|---|---|
| `/www/js/common.js` | `/data/iptv/www/inject/common.js` | `location = /js/common.js` |
| `/www/static/js/vendor.<hash>.js` | `/data/iptv/www/inject/vendor.<hash>.js` | `location = /static/js/vendor.<hash>.js` |

`patch.js` 加载后在页面右下角插入「路由控制台」入口，指向 `/manage/`；
`MARK` 判重保证重复追加不会产生多个按钮，SPA 与旧版布局各有一条插入路径。

## 生成副本

`data/iptv/start.sh` 每次执行都检查 vendor 包：副本不存在或小于原厂文件时，
重新拼接 `原厂文件 + patch.js`。`common.js` 不自动重建，固件升级后需手动执行：

```sh
cat /www/js/common.js /data/iptv/www/inject/patch.js > /data/iptv/www/inject/common.js
chmod 644 /data/iptv/www/inject/common.js
```

## nginx 规则

规则由 `start.sh` 幂等追加到 `/etc/nginx/miwifi-webinitrd.conf`：缺哪条补哪条，
写入后先执行 `nginx -t`，通过才 `nginx -s reload`，失败则还原备份。

```nginx
location = /js/common.js {
    alias /data/iptv/www/inject/common.js;
    default_type application/javascript;
    expires epoch;
}

location = /static/js/vendor.e8c617554e7536d3838e.js {
    alias /data/iptv/www/inject/vendor.e8c617554e7536d3838e.js;
    default_type application/javascript;
    expires epoch;
}
```

`vendor.<hash>.js` 的哈希随固件版本变化，`start.sh` 以 `ls /www/static/js/vendor.*.js`
取当前文件名，规则按该文件名生成。升级固件后确认规则中的文件名与实际一致。

## 说明

原厂 `common.js`、`vendor.*.js` 属于设备固件，仓库不收录，只保留注入片段 `patch.js`；
副本在设备上由原厂文件拼装生成。
