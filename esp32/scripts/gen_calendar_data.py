# -*- coding: utf-8 -*-
"""生成 cncal.py 里的农历 / 节气数据表（只在 PC 上跑，板子不依赖本文件）。

    pip install lunar_python
    python tests/gen_calendar_data.py

数据源：`lunar_python`（寿星天文历，精确到秒的日月算法）。做法是按公历
逐日扫描 2000-2101 年，直接取每天的中文节气名 `getJieQi()` 和农历
`year/month/day`，再压成两张紧凑表：

  LUNAR[y-2000] = (春节日序, 闰月, 大小月位图)
  TERM[y-2000]  = 24 个字符，chr(48 + 节气日号)

刻意不用 `getJieQiTable()`：那个表按「冬至→大雪」的节气年组织，跨年处
会出现 DONG_ZHI / XIAO_HAN 之类的拼音键，逐日取 `getJieQi()` 能完全绕开，
拿到的直接就是中文名。

生成后会用 cncal 自己的算法对 3.7 万天做全量反向校验，不一致直接报错。
"""

import datetime
import os
import re
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from lunar_python import Solar
import cncal

Y0, Y1 = 2000, 2100
TERMS = cncal.TERMS
TERM_IDX = {n: i for i, n in enumerate(TERMS)}


def dstr(y, m, d):
    return "%04d-%02d-%02d" % (y, m, d)


def scan():
    """按公历逐日扫描，返回 {(y,m,d): (农历年, 月, 日, 是否闰, 节气名)}。"""
    rec = {}
    t0 = time.time()
    for y in range(Y0, Y1 + 2):
        # 2101 只需要 1-2 月，用来补齐 2100 农历年的腊月
        months = range(1, 13) if y <= Y1 else range(1, 3)
        for m in months:
            dim = cncal.MONTH_DAYS[m - 1] + (1 if m == 2 and cncal.is_leap(y) else 0)
            for d in range(1, dim + 1):
                l = Solar.fromYmd(y, m, d).getLunar()
                lm = l.getMonth()
                rec[(y, m, d)] = (l.getYear(), abs(lm), l.getDay(),
                                  lm < 0, l.getJieQi())
    print("扫描 %d 天，用时 %.1fs" % (len(rec), time.time() - t0))
    return rec


def build_tables(rec):
    lunar_rows = []
    term_rows = []

    for y in range(Y0, Y1 + 1):
        # --- 24 节气：只取落在本公历年的 ---
        td = [0] * 24
        for m in range(1, 13):
            dim = cncal.MONTH_DAYS[m - 1] + (1 if m == 2 and cncal.is_leap(y) else 0)
            for d in range(1, dim + 1):
                jq = rec[(y, m, d)][4]
                if jq:
                    assert jq in TERM_IDX, (y, m, d, jq)
                    i = TERM_IDX[jq]
                    assert m == i // 2 + 1, ("节气月份不符", y, m, d, jq)
                    assert td[i] == 0, ("节气重复", y, jq)
                    td[i] = d
        assert all(td), ("节气缺失", y, td)
        assert all(3 <= x <= 25 for x in td), ("节气日号异常", y, td)
        term_rows.append("".join(chr(48 + x) for x in td))

        # --- 农历年 y：从正月初一到除夕 ---
        days = sorted(k for k, v in rec.items() if v[0] == y)
        assert days, ("农历年无数据", y)
        first = days[0]
        assert rec[first][1:4] == (1, 1, False), ("春节不是正月初一", y, first)
        assert first[0] == y, ("春节落在别的公历年", y, first)
        spring = cncal.doy(y, first[1], first[2])

        # 按「初一」切月，收集 (月, 是否闰, 天数)
        months = []
        for k in days:
            ly, lm, ld, lp, _jq = rec[k]
            if ld == 1:
                months.append([lm, lp, 0, k])
            months[-1][2] += 1
        assert months[0][:2] == [1, False], (y, months[0])
        assert len(months) in (12, 13), ("农历月数异常", y, len(months))
        leaps = [x[0] for x in months if x[1]]
        assert len(leaps) <= 1, ("多个闰月", y, leaps)
        leap = leaps[0] if leaps else 0
        assert all(x[2] in (29, 30) for x in months), ("月天数异常", y, months)
        mask = sum(1 << i for i, x in enumerate(months) if x[2] == 30)
        lunar_rows.append((spring, leap, mask))

    return lunar_rows, term_rows


def verify(rec, lunar_rows, term_rows):
    """用 cncal 自己的算法全量反查，确保表与 lunar_python 完全一致。"""
    # 把新表塞进已加载的模块（不落盘也能校验）
    cncal.LUNAR = tuple(lunar_rows)
    cncal.TERM = tuple(term_rows)
    n = 0
    for k, (ly, lm, ld, lp, jq) in rec.items():
        y, m, d = k
        if not (Y0 <= y <= Y1):
            continue
        # 2000 年春节前属于农历 1999 年，超出数据表左边界，
        # 换算会返回 None；接口对这种情况显示空农历，不崩。
        if ly < Y0:
            continue
        got = cncal.solar_to_lunar(y, m, d)
        assert got, ("农历换算失败", k)
        assert (got["y"], got["m"], got["d"], got["leap"]) == (ly, lm, ld, lp), \
            ("农历不一致", k, got, (ly, lm, ld, lp))
        # 节气：与表对照
        td = term_rows[y - Y0]
        hit = ""
        for i, c in enumerate(td):
            if d == ord(c) - 48 and i // 2 + 1 == m:
                hit = TERMS[i]
                break
        assert hit == jq, ("节气不一致", k, hit, jq)
        n += 1
    print("全量反查 %d 天：农历与节气全部一致" % n)

    # 蔡勒公式对照 datetime
    for y in range(Y0, Y1 + 1, 7):
        for m, d in ((1, 1), (2, 28), (3, 1), (6, 15), (10, 1), (12, 31)):
            assert cncal.weekday(y, m, d) == datetime.date(y, m, d).weekday(), \
                ("星期算错", y, m, d)
    print("星期公式对照 datetime：一致")


def render(lunar_rows, term_rows):
    out = ["LUNAR = ("]
    for i, (a, b, c) in enumerate(lunar_rows):
        out.append("    (%d, %d, %d),  # %d" % (a, b, c, Y0 + i))
    out.append(")")
    out.append("TERM = (")
    for i, s in enumerate(term_rows):
        out.append('    "%s",  # %d' % (s, Y0 + i))
    out.append(")")
    return "\n".join(out) + "\n"


def main():
    rec = scan()
    lunar_rows, term_rows = build_tables(rec)
    verify(rec, lunar_rows, term_rows)

    p = os.path.join(ROOT, "cncal.py")
    src = open(p, encoding="utf-8").read()
    pat = r"(# >>> DATA[^\n]*\n).*?(# <<< DATA <<<)"
    m = re.search(pat, src, re.S)
    assert m, "cncal.py 里找不到 DATA 标记"
    block = ("# LUNAR[y-2000] = (春节距 1/1 的天数, 闰月[0=无], 13 位大小月位图[1=30天])\n"
             "# TERM[y-2000]  = 24 个字符，chr(48 + 该节气在当月的日号)\n" +
             render(lunar_rows, term_rows))
    src = src[:m.start(1)] + m.group(1) + block + m.group(2) + src[m.end(2):]
    open(p, "w", encoding="utf-8").write(src)
    print("已写入 %s：%d 年农历 + 节气，数据段 %d 字节"
          % (p, len(lunar_rows), len(block)))


if __name__ == "__main__":
    main()
