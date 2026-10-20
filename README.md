# DeepSeek 用量图（token-chart）

**[下载](../../releases/latest)**
- `token-chart-win-exe.zip` —— Windows 双击就能跑，不用装 Python（推荐）
- `token-chart-src.zip` —— 源码版，有 Python 用这个


把 DeepSeek 平台导出的用量 CSV 合并、按天去重、按月拆成 `data/YYYY-MM.csv`，
再生成一张单文件网页 `token-usage.html`（数据写在页面里，双击就能看、能转发）。

## 用起来

1. 到 https://platform.deepseek.com/usage 导出用量，会得到 `usage_data_YYYY-MM-DD_YYYY-MM-DD.zip`（或同名文件夹）
2. 把导出的东西丢进本目录（或本目录下的 `data/`）
3. 双击 **重新生成并打开.bat**
   - 目录里有 `token-chart.exe` 就直接用它（不需要装 Python）
   - 没有就退回 `python build.py`
4. 生成出来的 `token-usage.html` 双击打开

## 页面里能做什么

- 时间范围：最近 30 天 / 本月 / 上月 / 3 个月 / 半年 / 一年 / 今年
- 模型（flash / v4-pro）和 API（q-bot / Astar …）都能单独勾选；取消哪个，就从"当前所选"里减掉
- 顶部两行：**全部历史**（不受任何勾选影响）和**当前所选**
- 右上角「设置」记账：充值、赠送各记一笔，自动算剩余余额、赠送已用/剩余

## 目录

```
build.py                生成脚本
重新生成并打开.bat       双击即可
打包exe.bat             重新打 exe（需要 Python + pyinstaller）
data/YYYY-MM.csv        按月拆好的数据（程序自己维护）
data/ledger.txt         充值和赠送的记账
token-usage.html        生成的页面
```

## 说明

- 数据来源只认 `data/` 目录；导出文件只是用来导入的原料，导完可以删
- 导出文件重复（同一天导了两次）不会翻倍：同一段日期以最新那份为准
- 记账用 `python build.py --serve` 打开时自动写回 `data/ledger.txt`；
  直接双击打开则存在浏览器里，点「导出 ledger.txt」可手动保存

## 打包 exe

```bat
pip install pyinstaller
python -m PyInstaller --onefile --name token-chart build.py
```

或直接双击 `打包exe.bat`。
