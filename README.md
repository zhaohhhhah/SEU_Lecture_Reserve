东南大学人文与科学素养系列讲座自动抢票脚本（大面积基于前人代码，优化手机短信验证码登陆）
## 功能特点
- 全自动预约：从登录到预约全程无需人工操作
- 验证码识别：自动识别图形验证码（成功率>90%）
- 校区优先：优先抢指定校区的讲座
- 短信验证码支持：支持25 Fall起出现的SEU的2FA
- 通过简单逻辑支持登陆界面网页元素识别
- 智能过滤：自动跳过时间冲突的讲座/支持同时抢线上线下讲座/自定义讲座类型白名单

## 快速开始
### 安装准备
1. 安装 Python 3.10
2. 下载代码包（欢迎Star ⭐）：[点击下载](https://github.com/ApricityWang/SEU_Lecture_Reserve.git)
3. 解压后进入文件夹
4. 配置浏览器环境：[Chrome浏览器 和 Chromedriver](https://googlechromelabs.github.io/chrome-for-testing/#dev)
5. 安装依赖
   ```shell
   pip install -r requirements.txt
   ```
6. 配置账户信息和偏好：复制配置模板并填写
   ```shell
   cp config.yaml.example config.yaml
   ```
   在 `config.yaml` 里填上 `card_number`（一卡通号）、`password`，以及接收通知的
   `qq_email` / `qq_auth_code`（QQ 邮箱 SMTP 授权码）。`config.yaml` 已加入 `.gitignore`，
   不会被提交；只写想改的项也可以，其余用 `main.py` 里的默认值。
7. 敏感配置也可以用环境变量覆盖（优先于 `config.yaml`）：
   `SEU_CARD_NUMBER`、`SEU_PASSWORD`、`QQ_EMAIL_ADDRESS`、`QQ_EMAIL_AUTH_CODE`
## 注意事项
1. 首次运行请自行下载配置浏览器驱动，注意版本对应（约需1分钟）
2. 浏览器未认证或掉认证时将登录跳转等待时间设定为较长时间(例如60s)，完成短信验证码认证后即可修改为正常值
3. 程序运行时会自动打开浏览器窗口~~，请勿操作浏览器~~
4. 讲座抢票成功后程序会循环刷新，直至多次反馈无可预约讲座
5. 验证码识别模块基于ddddocr实现，当前已采集部分验证码样本，可自行训练专属模型以进一步提升识别准确率

## 常见问题
Q：为什么提示 ？ A： 可能是当前没有可预约的讲座，或网络连接不稳定。

Q：如何查看是否预约成功？ A： 程序成功后会提示，也可登录 [东南大学素质讲座预约系统](https://ehall.seu.edu.cn/) 确认。

Q：会被系统检测到吗？ A： 本程序模拟人工操作，但请勿高频次运行（建议间隔≥5分钟）。

Q：为什么看不到浏览器窗口？ A： 默认后台运行，如需查看操作过程，打开 `main.py`，在 `chrome_options.add_argument("--headless")` 前添加 `#` 号。

# 免责声明
本程序仅供学习 Python 自动化技术使用，开发者不对使用后果负责。使用即表示您同意：
- 遵守东南大学相关管理规定。
- 不进行任何破坏性操作。
- 不将本程序用于商业用途。
