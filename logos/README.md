# 媒体 logo（不随仓库分发）

各家媒体的 logo 是**受商标/版权保护的第三方素材**，本项目不打包分发，
本目录在 `.gitignore` 中被排除（仅保留这份说明）。

**不放 logo 也能正常用** —— 程序检测到缺图时会自动降级为文字标识
（例如显示「CNN」「新华社」的圆角色块），布局和功能都不受影响。

## 想显示 logo 怎么做

1. 自行获取有权使用的图片，放进本目录，例如 `BBC.png`；
2. 在 `newsagg/models.py` 的 `SOURCE_LOGOS` 里登记映射：

   ```python
   SOURCE_LOGOS = {
       "bbc": "BBC.png",
       "xinhua": "XINHUASHE.png",
       # 源 id 见 config/sources.yaml
   }
   ```

3. 重新渲染：`python -m newsagg.render`

渲染时会自动把图片压到 96px 并输出到 `output/logos/`，
宽高比原样保留，无需你手动裁剪。

## 提醒

请自行确认你对所用图片有相应权利。公开转载他人商标可能带来法律风险，
这也是本项目默认不附带 logo 的原因。
