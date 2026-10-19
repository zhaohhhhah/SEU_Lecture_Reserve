"""
东南大学素质讲座预约抢票脚本
主要功能：自动登录系统、扫描符合要求的讲座并进行预约
"""

# ==================== 第三方库导入 ====================
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
import time
import base64
import ddddocr
import re
from datetime import datetime
import os
import platform
import smtplib
import ssl
from email.mime.text import MIMEText

# ==================== 日志时间戳（毫秒精度） & 抢票计时 ====================
# 把内置 print 包一层，给**每一行日志**加 HH:MM:SS.mmm 前缀，方便事后回报“抢到用时”。
# 关键：只在“真的要打印”时才取时间；抢票热循环里没有 print 就**一点额外开销都没有**
# （不往循环里塞任何计时函数）。取时间用 time.time()（走 vDSO，约几十 ns）+ strftime。
_builtin_print = print


def print(*args, **kwargs):  # noqa: A001 —— 有意覆盖，全模块统一加时间戳
    if not args:
        return _builtin_print(**kwargs)
    _t = time.time()
    _ts = time.strftime("%H:%M:%S", time.localtime(_t)) + ".%03d" % int((_t % 1) * 1000)
    _builtin_print(_ts, *args, **kwargs)


# 抢票计时：以“预约开放那一刻”为起点量“抢到用时”。
# 用 perf_counter（单调时钟）算差值，不受 NTP 微调影响；起点/终点各只取一次，不进热循环。
_GRAB_T0 = None


def _mark_grab_start(open_ts):
    """进入 19:00 抢票循环前调用。把 perf_counter 基准**倒推**到 open_ts（本机时钟的
    19:00:00.000），这样“抢到用时”是从开放那一刻算起，不受随后 while 轮询最多 20ms 的过冲影响。"""
    global _GRAB_T0
    _GRAB_T0 = time.perf_counter() - (time.time() - open_ts)
    _ms = int(open_ts % 1 * 1000)
    print("⏱ 计时起点 = 19:00:00（本机时钟 %s.%03d）"
          % (datetime.fromtimestamp(open_ts).strftime("%H:%M:%S"), _ms))


def _grab_elapsed_str():
    """自抢票起点起的用时（毫秒精度，形如 '1.234 秒'）；未开始计时则返回空串。"""
    if _GRAB_T0 is None:
        return ""
    return "%.3f 秒" % (time.perf_counter() - _GRAB_T0)


# ==================== 全局配置项 ====================
# 真实配置放在脚本同目录的 config.yaml（已加入 .gitignore，不会提交）。
# 首次使用：把 config.yaml.example 复制为 config.yaml，填上账号密码。
# 这里只保留非敏感默认值，配置缺失时也能运行（只是不会有登录凭据）。
CONFIG = {
    # 登录凭证（在 config.yaml 里填写）
    "card_number": "",  # 一卡通号
    "password": "",     # 登录密码

    # 通知邮箱（QQ）。也可用环境变量 QQ_EMAIL_ADDRESS / QQ_EMAIL_AUTH_CODE 覆盖
    "qq_email": "",      # 收通知的 QQ 邮箱
    "qq_auth_code": "",  # QQ 邮箱 SMTP 授权码（不是 QQ 登录密码）

    # 讲座筛选条件
    "preferred_lectures": [],
    "exclude_keywords": ["测试", "test"],   # 标题含这些词的一律跳过（不分大小写）
    "required_categories": [
        "人文与科学素养系列讲座_心理健康",
        "人文与科学素养系列讲座_法律",
        "人文与科学素养系列讲座-艺术类",
        "人文与科学素养系列讲座_其他",
        '“SEU咖啡间"系列沙龙活动'
    ],
    "preferred_campus": ["九龙湖校区", "四牌楼校区"],  # 优先选择的校区
    "enable_offline": True,   # 是否抢线下讲座
    "enable_online": False,   # 是否抢线上讲座

    # 回流票监控（抢票窗口结束后，不停进出详情页检测是否有新开放）
    "watch_backflow": True,          # 是否开启回流票监控
    "backflow_dense_seconds": 60,    # 密集监控时长(秒)
    "backflow_interval": 3,          # 密集阶段的间隔(秒)
    "backflow_slow_interval": 60,    # 密集阶段之后的间隔(秒)（当前配置下用不到）
    "backflow_total_seconds": 60,    # 回流监控总时长上限(秒)：到点就收工，不整晚挂着

    # ==================== 时间参数配置 ====================
    "timeout_settings": {
        "page_load": 0.5,        # 页面加载基础等待时间
        "element_wait": 3,       # 元素查找最大等待时间
        "login_redirect": 120,   # 登录跳转等待时间（需要短信验证时可调大）
        "scan_interval": 0.05,   # 扫描间隔时间
        "retry_delay": 0.05,     # 重试延迟时间
        "action_confirm": 0.2,   # 操作确认等待时间
    }
}

# 环境变量优先于 config.yaml（容器部署时可注入，避免配置文件里出现明文）
_ENV_OVERRIDES = {
    "card_number": "SEU_CARD_NUMBER",
    "password": "SEU_PASSWORD",
    "qq_email": "QQ_EMAIL_ADDRESS",
    "qq_auth_code": "QQ_EMAIL_AUTH_CODE",
}


def _load_config():
    """读取同目录 config.yaml，覆盖默认配置；嵌套字典按层级合并。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    if os.path.exists(path):
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            if not isinstance(user_cfg, dict):
                raise ValueError("config.yaml 顶层必须是键值映射")
            for key, value in user_cfg.items():
                if isinstance(value, dict) and isinstance(CONFIG.get(key), dict):
                    CONFIG[key].update(value)
                else:
                    CONFIG[key] = value
        except Exception as e:
            print(f"⚠ 读取 config.yaml 失败，使用默认配置: {e}")
    else:
        print("⚠ 未找到 config.yaml，请复制 config.yaml.example 为 config.yaml 并填写账号密码")
    for key, env_name in _ENV_OVERRIDES.items():
        if os.environ.get(env_name):
            CONFIG[key] = os.environ[env_name]


_load_config()

# ==================== 邮件通知（QQ 邮箱） ====================
def send_qq_mail(subject, body, attempts=3):
    """通过 QQ 邮箱 SMTP 发通知；凭据优先读环境变量，否则用默认值。
    容器出站链路偶尔会 Network is unreachable（实测碰到过），所以失败重试几次——
    邮件是抢票结果唯一的通知渠道，不能因为一次抖动就丢了。返回是否最终成功。"""
    addr = os.environ.get("QQ_EMAIL_ADDRESS") or CONFIG.get("qq_email") or ""
    code = os.environ.get("QQ_EMAIL_AUTH_CODE") or CONFIG.get("qq_auth_code") or ""
    if not addr or not code:
        print("📧 未配置 QQ 邮箱（qq_email / qq_auth_code），跳过发信")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = addr
    for i in range(1, attempts + 1):
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ctx, timeout=15) as s:
                s.login(addr, code)
                s.sendmail(addr, [addr], msg.as_string())
            print(f"📧 已发邮件通知给 {addr}: {subject}")
            return True
        except Exception as e:
            print(f"📧 邮件发送失败({i}/{attempts}): {e}")
            if i < attempts:
                time.sleep(2 * i)
    return False

# 登录成功通知去重标志
_LOGIN_NOTIFIED = False
# “疑似抢到但没能确认”只通知一次，避免在循环里狂发
_AMBIG_NOTIFIED = False

def notify_ambiguous(info, where="抢票"):
    """点了预约、验证码也提交了，但页面没给明确结果时，发一次“疑似成功、请人工确认”。
    宁可让用户自己上系统确认，也不能沉默——更不能误报成“成功”让流程提前退出。"""
    global _AMBIG_NOTIFIED
    if _AMBIG_NOTIFIED:
        return
    _AMBIG_NOTIFIED = True
    try:
        send_qq_mail(f"【SEU抢票】⚠ 疑似预约成功，请确认（{where}）",
                     f"已在「{info['title']}」提交预约，但没拿到明确的成功提示。\n"
                     f"时间：{_fmt_time(info['start_time'])} ~ {_fmt_time(info['end_time'])}\n"
                     f"校区：{info['campus']}\n\n"
                     f"请上东大讲座系统确认是否约上。脚本会继续监控回流票。")
    except Exception as e:
        print(f"邮件异常: {e}")

# ==================== 全局工具初始化 ====================
# ddddocr 在“启动时”就把模型加载好（这里的构造 = 加载模型），19:00 抢票那一刻
# 只做一次纯推理，不加载模型。实测：构造 ~0.06s、单次识别稳定 ~9ms。
ocr = ddddocr.DdddOcr(show_ad=False)
try:
    # 再拿一张空白图做一次“热身”，把 onnxruntime 首次推理的图优化开销也提前掉
    # （实测首次 ~48ms、之后稳定 ~9ms）——抢票时少这一次抖动。
    import io as _io
    from PIL import Image as _Image
    _buf = _io.BytesIO()
    _Image.new("RGB", (100, 32), (255, 255, 255)).save(_buf, format="PNG")
    ocr.classification(_buf.getvalue())
except Exception as _e:
    print(f"⚠ OCR 热身失败（不影响使用）: {_e}")

chrome_options = Options()
# Chromium/Chrome 可执行文件：Debian 容器里是 /usr/bin/chromium；Windows/macOS 上是 Chrome。
# 自动探测（找不到就不设，交给 selenium 按默认/PATH 去认）。
for _chrome_bin in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                    "/usr/bin/google-chrome", "/opt/google/chrome/chrome"):
    if os.path.exists(_chrome_bin):
        chrome_options.binary_location = _chrome_bin
        break
# chrome_options.add_argument("--headless")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920x1080")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

# ==================== 自动检测系统并配置 ChromeDriver ====================
def setup_driver():
    """根据操作系统自动配置 ChromeDriver"""
    system = platform.system().lower()

    # 检查当前目录下是否有 chromedriver
    possible_paths = []

    if system == "windows":
        possible_paths = [
            "./chromedriver.exe",
            "./chromedriver-win64/chromedriver.exe",
            "./chromedriver-windows/chromedriver.exe",
            "chromedriver.exe"
        ]
    elif system == "darwin":  # macOS
        possible_paths = [
            "./chromedriver",
            "./chromedriver-mac-arm64/chromedriver",
            "./chromedriver-mac-x64/chromedriver"
        ]
    else:  # linux
        possible_paths = [
            "./chromedriver",
            "./chromedriver-linux64/chromedriver"
        ]

    # 查找可用的 ChromeDriver
    chromedriver_path = None
    for path in possible_paths:
        if os.path.exists(path):
            chromedriver_path = path
            print(f"✅ 找到 ChromeDriver: {path}")
            break

    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
        return webdriver.Chrome(service=service, options=chrome_options)
    else:
        print("❌ 未找到 ChromeDriver，尝试自动下载或使用系统路径...")
        # 如果没找到，尝试使用系统 PATH 中的 ChromeDriver
        try:
            return webdriver.Chrome(options=chrome_options)
        except Exception as e:
            print(f"❌ 自动配置失败: {str(e)}")
            print("\n📝 请手动下载 ChromeDriver：")
            print("1. 访问 https://chromedriver.chromium.org/")
            print("2. 下载与您Chrome版本匹配的ChromeDriver")
            print("3. 解压后将chromedriver.exe放在脚本同目录下")
            exit(1)

# 初始化 driver：最多试 3 次；都失败就**先写 ACTIVE/PID 标记再退出**——
# 因为这些标记本来是在 __main__ 里写的，而 driver 建不出来时 __main__ 根本不会执行，
# 看门狗（要求 ACTIVE 存在）就永远没有重启依据，整晚会静默死掉。
# 注意：这里在模块顶层，mark_state/_state_path 还没定义，只能直接写文件。
driver = None
for _attempt in range(1, 4):
    try:
        driver = setup_driver()
        break
    except (SystemExit, Exception) as _e:
        print(f"⚠ 第 {_attempt}/3 次启动浏览器失败: {_e}")
        if _attempt < 3:
            time.sleep(5)
if driver is None:
    try:
        _d = os.path.dirname(os.path.abspath(__file__))
        for _n, _c in ((".seu_active", str(int(time.time()))), (".seu_pid", str(os.getpid()))):
            with open(os.path.join(_d, _n), "w") as _f:
                _f.write(_c)
    except Exception:
        pass
    print("❌ 浏览器无法启动，退出（已留 ACTIVE 标记，看门狗会重拉）")
    raise SystemExit(1)

# 防止页面/JS 卡死：限制单次加载与脚本执行时间，超时会抛异常由主循环兜住
try:
    driver.set_page_load_timeout(20)
    driver.set_script_timeout(20)
except Exception as _e:
    print(f"⚠ 设置 driver 超时失败: {_e}")

# ==================== 核心功能函数 ====================
def _notify_login_problem(reason):
    """登录出问题时的提醒——用状态文件做“一天/一轮只提醒一次”。
    否则看门狗每 5 分钟重拉、每次都发一封，会刷屏。"""
    try:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seu_login_notify")
        if os.path.exists(_p):
            return
        with open(_p, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass
    try:
        send_qq_mail("【SEU抢票】需要你处理（登录/验证）", reason)
    except Exception as e:
        print(f"邮件异常: {e}")


def login(redirect_wait=None):
    """登录（失败自动重试 3 次再放弃）。
    原来只试一次：18:50 撞校园网登录高峰、页面渲染慢一点就整晚没戏，
    而且三个失败分支连邮件都不发（静默失败）。"""
    for _i in range(1, 4):
        if _login_once(redirect_wait):
            return True
        print(f"⚠ 登录第 {_i}/3 次未通过")
        if _i < 3:
            time.sleep(3)
    _notify_login_problem("登录连续 3 次未通过，可能需要短信验证码或密码有变动。"
                          "请打开 http://10.201.114.88:3000（账号/密码：admin）在其浏览器里手动完成登录。")
    return False


def _login_once(redirect_wait=None):
    """执行一次系统登录操作 - 通用版本。redirect_wait 可覆盖登录后的等待秒数（抢票时用短值）"""
    try:
        print("🌐 正在加载登录页面...")
        driver.get("https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do#/hdyy")
        time.sleep(CONFIG["timeout_settings"]["page_load"])  # 页面加载等待

        # 打印调试信息
        print(f"📄 当前页面标题: {driver.title}")
        print(f"🔗 当前页面URL: {driver.current_url}")

        # 尝试多种可能的选择器
        selectors_to_try = [
            # 统一认证页面选择器
            (By.ID, "username"),
            (By.NAME, "username"),
            (By.CSS_SELECTOR, "input[name='username']"),

            # 原登录页面选择器
            (By.CSS_SELECTOR, ".input-username-pc input"),
            (By.CSS_SELECTOR, "input[placeholder*='号']"),
            (By.CSS_SELECTOR, "input[type='text']"),
        ]

        username_input = None
        for by, selector in selectors_to_try:
            try:
                username_input = WebDriverWait(driver, CONFIG["timeout_settings"]["element_wait"]).until(
                    EC.presence_of_element_located((by, selector))
                )
                print(f"✅ 找到用户名输入框: {by}={selector}")
                break
            except:
                continue

        if not username_input:
            print("❌ 无法找到用户名输入框")
            # 保存页面源码和截图以便调试
            with open("debug_page_source.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            driver.save_screenshot("debug_login.png")
            print("💾 已保存页面源码和截图到 debug_page_source.html 和 debug_login.png")
            _notify_login_problem("登录页加载异常：找不到用户名输入框（校园网慢或页面改版）。"
                                  "请打开 http://10.201.114.88:3000（账号/密码：admin）查看并手动登录。")
            return False

        # 清空并输入用户名
        username_input.clear()
        username_input.send_keys(CONFIG["card_number"])

        # 类似地查找密码输入框
        password_selectors = [
            (By.ID, "password"),
            (By.NAME, "password"),
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, ".input-password-pc input"),
        ]

        password_input = None
        for by, selector in password_selectors:
            try:
                password_input = WebDriverWait(driver, CONFIG["timeout_settings"]["element_wait"]).until(
                    EC.presence_of_element_located((by, selector))
                )
                print(f"✅ 找到密码输入框: {by}={selector}")
                break
            except:
                continue

        if not password_input:
            print("❌ 无法找到密码输入框")
            _notify_login_problem("登录页加载异常：找不到密码输入框（校园网慢或页面改版）。")
            return False

        password_input.send_keys(CONFIG["password"])

        # 查找登录按钮
        button_selectors = [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CLASS_NAME, "login-button-pc"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button"),
        ]

        login_button = None
        for by, selector in button_selectors:
            try:
                login_button = WebDriverWait(driver, CONFIG["timeout_settings"]["element_wait"]).until(
                    EC.element_to_be_clickable((by, selector))
                )
                print(f"✅ 找到登录按钮: {by}={selector}")
                break
            except:
                continue

        if not login_button:
            print("❌ 无法找到登录按钮")
            _notify_login_problem("登录页加载异常：找不到登录按钮（校园网慢或页面改版）。")
            return False

        login_button.click()
        print("✅ 登录信息已提交，等待页面跳转...")
        _wait = CONFIG["timeout_settings"]["login_redirect"] if redirect_wait is None else redirect_wait
        # 原来这里是固定 time.sleep(_wait)：默认 120s，登录成功也要白等 2 分钟，把 18:50 的
        # 预检查邮件拖到 18:52；登录失败重试 3 次就是 6 分钟白等。改成轮询 URL——一离开
        # 认证页立刻返回；需要人工短信验证时，最多仍等 _wait 秒。
        _deadline = time.time() + _wait
        while time.time() < _deadline:
            try:
                _u = driver.current_url
            except Exception:
                _u = ""
            if _u and ("auth.seu.edu.cn" not in _u) and ("login" not in _u):
                break
            time.sleep(0.5)

        # 检查登录结果
        current_url = driver.current_url
        if "auth.seu.edu.cn" in current_url or "login" in current_url:
            print("❌ 登录失败，请检查账号密码")
            _notify_login_problem("登录未通过，可能需要短信验证码或密码有变动。"
                                  "请打开 http://10.201.114.88:3000（账号/密码：admin）在其浏览器里手动完成登录。")
            return False
        else:
            print("✅ 登录成功！")
            global _LOGIN_NOTIFIED
            if not _LOGIN_NOTIFIED:
                _LOGIN_NOTIFIED = True
                send_qq_mail("【SEU抢票】已登录就位", "已成功登录东大讲座系统，开始扫描待命；抢到会再通知你。")
            return True

    except Exception as e:
        print(f"❌ 登录过程中出错: {str(e)}")
        # 保存页面源码和截图以便调试
        with open("error_page_source.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot("error_login.png")
        print("💾 已保存错误页面源码和截图到 error_page_source.html 和 error_login.png")
        return False

def parse_lecture(element):
    """
    解析讲座元素信息

    Args:
        element: WebElement 讲座页面元素

    Returns:
        dict: 包含解析后的讲座信息，格式为：
            {
                "category": 讲座类别,
                "title": 讲座标题,
                "campus": 校区/线上,
                "start_time": 开始时间,
                "end_time": 结束时间,
                "element": 原始元素,
                "status": 预约状态
            }
    """
    try:
        # 解析基础信息
        category = element.find_element(By.CSS_SELECTOR, ".hdxq-hdlx .mint-text").text
        title = element.find_element(By.CSS_SELECTOR, ".activity-name .mint-text").text
        location = element.find_element(By.CSS_SELECTOR, "div[title='item.JZDD']").text

        # 解析时间信息
        time_text = element.find_elements(By.CSS_SELECTOR, ".activity-text .mint-text")[1].text
        time_matches = re.findall(r"\d{4}/\d{2}/\d{2}/\d{2}:\d{2}:\d{2}", time_text)
        start_time = datetime.strptime(time_matches[0], "%Y/%m/%d/%H:%M:%S")
        end_time = datetime.strptime(time_matches[1], "%Y/%m/%d/%H:%M:%S")

        # 解析“预约起止时间”（开放时间 / 截止时间）
        try:
            _ot = element.find_element(By.CSS_SELECTOR, ".activity-time").text
            _ts = re.findall(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", _ot)
            open_time = _ts[0] if _ts else ""
            close_time = _ts[1] if len(_ts) > 1 else ""
        except:
            open_time = ""
            close_time = ""

        return {
            "category": category,
            "title": title,
            "campus": _extract_campus(location),
            "location": location,
            "start_time": start_time,
            "end_time": end_time,
            "open_time": open_time,
            "close_time": close_time,
            "element": element,
            "status": element.find_element(By.CSS_SELECTOR, "button").text.strip()
        }
    except Exception as e:
        print(f"❌ 讲座解析失败: {str(e)}")
        return None

def _extract_campus(location):
    """
    校区信息提取辅助函数

    Args:
        location: str 原始位置信息

    Returns:
        str: 提取后的校区/会议信息
    """
    campus_match = re.match(r'^(.+?校区)', location)
    return campus_match.group(1) if campus_match else location

def _lecture_tier(lecture):
    """按 CONFIG["priority"] 的顺序返回档位（0 = 最想抢）。不在表里的排最后。
    每条规则可写 campus（校区，允许写成“四牌楼校区”，也匹配地点里含这几个字）
    和 mode（online/offline）。两个键都省略 = 通配。"""
    is_online = "线上" in lecture["title"]
    mode = "online" if is_online else "offline"
    rules = CONFIG.get("priority") or []
    location = lecture.get("location", "")
    # 线上场次的校区是“腾讯会议”，真正归属看同名线下场次（scan_cards 补的 host_campus）
    campus = lecture.get("host_campus") or lecture["campus"]
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            continue
        _mode = r.get("mode")
        if _mode and _mode != mode:
            continue
        _campus = r.get("campus")
        if _campus and (_campus not in campus and _campus not in location):
            continue
        return i
    return len(rules)


def _norm_title(t):
    """去掉标题开头的【线上】/【线下】，用来把同一场讲座的线上/线下两条记录对上"""
    return re.sub(r"^【(线上|线下)】\s*", "", (t or "").strip())

def _sort_priority(lecture):
    """
    讲座排序优先级规则（升序排列）
    1. CONFIG["priority"] 的档位：四牌楼+线上 > 九龙湖+线下 > 其余
    2. 必需类别优先（preferred_lectures 命中时类别不参与）
    3. 预约开放时间早的优先——今晚谁先开抢谁优先；
       原来按讲座 start_time 排，会把下下周才开放预约的排成第一目标，今晚只能空转。
    """
    required_priority = 0 if lecture["category"] in CONFIG["required_categories"] else 1
    open_t = _parse_hd_time(lecture.get("open_time")) or lecture["start_time"]
    return (_lecture_tier(lecture), required_priority, open_t, lecture["start_time"])

def _fmt_time(t, default="?"):
    """parse_lecture 里 start_time/end_time 是 datetime，邮件里格式化成人看的样子"""
    try:
        return t.strftime("%Y/%m/%d %H:%M")
    except Exception:
        return str(t) if t else default

def _parse_hd_time(s):
    """把预约窗口 "YYYY/MM/DD HH:MM" 解析成 datetime；解析不了返回 None"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y/%m/%d %H:%M")
    except Exception:
        return None

def _grabs_tonight(lecture, now=None):
    """今晚 19:00 这场是否抢得到：
    - 预约窗口已经截止 → 抢不了
    - 明天或以后才开放预约 → 今晚点了也是「未开放」，空转一整晚
    解析不到就当作能抢（宁松勿漏：格式一变宁可多试，也别把该抢的漏掉）。"""
    now = now or datetime.now()
    close_t = _parse_hd_time(lecture.get("close_time"))
    open_t = _parse_hd_time(lecture.get("open_time"))
    if open_t is not None and open_t.date() > now.date():
        return False                      # 明天及以后才开放
    if close_t is not None:
        return close_t > now              # 有截止时间：还没截止就能抢
    if open_t is not None:
        # 只知道开放时间、不知道截止时间：必须今天开放才算今晚能抢。
        # （曾经有场“系统测试（请勿预约）”，开放时间是一年前、截止时间解析不出来，
        #   按失败放行会把它当成今晚目标）
        return open_t.date() == now.date()
    return True                           # 两个都解析不到才失败放行

# ==================== 看门狗状态标记 ====================
# .seu_active  存在 = 这一轮还没正常收尾（崩了就该被 guard 拉起）
# .seu_done    存在 = 本轮已正常收尾（成功/无讲座/正常结束，guard 别管）
# .seu_beat    心跳时间戳（epoch），供 guard 判断“进程活着但卡死”
# .seu_restarts 已被 guard 拉起的次数（防无限重启）
_STATE_DIR = os.path.dirname(os.path.abspath(__file__))

def _state_path(name):
    return os.path.join(_STATE_DIR, name)

def mark_state(name, content=""):
    try:
        with open(_state_path(name), "w") as f:
            f.write(content)
    except Exception as e:
        print(f"⚠ 写状态标记 {name} 失败: {e}")

def clear_state(*names):
    for n in names:
        try:
            p = _state_path(n)
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def heartbeat():
    """主循环里定期打点，看门狗据此判断进程是否卡死"""
    mark_state(".seu_beat", str(int(time.time())))

# ==================== 定时/筛选辅助 ====================
def wait_until(hour, minute, label=""):
    """阻塞到当天的 hour:minute:00。
    等待期间持续打心跳——否则 18:00 看门狗闸门打开时，会把“心跳还是几小时前”的
    正常等待进程误判成卡死并杀掉重拉。"""
    while True:
        heartbeat()
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (target - now).total_seconds()
        if delta <= 0:
            print(f"⏰ 到达 {hour:02d}:{minute:02d} {label}")
            return
        time.sleep(min(delta, 10))

def _excluded_keyword(lecture):
    """标题命中 CONFIG["exclude_keywords"] 就返回命中的词，否则返回空串。
    用来跳过“测试”之类的占位 / 内部讲座（不分大小写）。"""
    title = (lecture.get("title") or "").lower()
    for kw in (CONFIG.get("exclude_keywords") or []):
        kw = str(kw).strip()
        if kw and kw.lower() in title:
            return kw
    return ""

def is_candidate(lecture):
    """按条件筛选候选讲座（忽略按钮状态；用于“今天有没有可抢的”）"""
    if _excluded_keyword(lecture):
        return False
    if CONFIG["preferred_lectures"]:
        return lecture["title"] in CONFIG["preferred_lectures"]
    if lecture["category"] not in CONFIG["required_categories"]:
        return False
    is_online = "线上" in lecture["title"]
    if is_online and not CONFIG["enable_online"]:
        return False
    if (not is_online) and not CONFIG["enable_offline"]:
        return False
    # 校区白名单为空 = 不限校区（优先级靠 CONFIG["priority"] 排，见 _lecture_tier）
    _campus_allow = CONFIG.get("preferred_campus") or []
    if (not is_online) and _campus_allow and lecture["campus"] not in _campus_allow:
        return False
    return True

def scan_cards():
    """refresh 后解析页面上所有讲座卡片（不要求按钮状态）"""
    try:
        driver.refresh()
    except Exception as e:
        print(f"⚠ 刷新失败（可能超时）: {e}")
        return []
    try:
        WebDriverWait(driver, CONFIG["timeout_settings"]["element_wait"]).until(
            EC.presence_of_element_located((By.CLASS_NAME, "activity-container")))
    except:
        return []
    out = []
    for el in driver.find_elements(By.CLASS_NAME, "activity-container"):
        lec = parse_lecture(el)
        if lec:
            out.append(lec)

    # 线上场次没有校区（地点写的是“腾讯会议”），用**同名线下场次**的校区补上，
    # 这样“四牌楼线上/九龙湖线上”才排得了序。
    # 例：【线上】【艺术】希望、焦虑… ↔ 【线下】【艺术】希望、焦虑…（四牌楼校区）
    _host_campus = {}
    for c in out:
        if "线上" not in c["title"]:
            _host_campus[_norm_title(c["title"])] = c["campus"]
    for c in out:
        if "线上" in c["title"]:
            c["host_campus"] = _host_campus.get(_norm_title(c["title"]), "")
    return out

LIST_URL = "https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do#/hdyy"

def go_list():
    """回到讲座列表页"""
    try:
        driver.get(LIST_URL)
    except Exception as e:
        print(f"⚠ 打开列表页失败（可能超时）: {e}")
    time.sleep(CONFIG["timeout_settings"]["page_load"])

def enter_detail(titles):
    """从列表点卡片主体进入标题匹配的详情页。
    titles 按优先级给出（list）：严格按传入顺序挑第一个存在的，而不是按页面 DOM 顺序，
    这样“抢哪一场”和邮件里写的目标才一致。成功返回进入的标题，否则 None。"""
    wanted = list(titles)
    wanted_set = set(wanted)
    if "#/hdyy" not in driver.current_url:
        go_list()
    found = {}
    for el in driver.find_elements(By.CLASS_NAME, "activity-container"):
        try:
            lec = parse_lecture(el)
        except:
            continue
        if lec and lec["title"] in wanted_set and lec["title"] not in found:
            found[lec["title"]] = el
    for t in wanted:
        el = found.get(t)
        if el is None:
            continue
        try:
            el.find_element(By.CSS_SELECTOR, ".activity-content").click()
        except Exception:
            continue
        try:
            WebDriverWait(driver, CONFIG["timeout_settings"]["element_wait"]).until(
                lambda d: "#/hdxq" in d.current_url)
        except:
            pass
        if "#/hdxq" in driver.current_url:
            return t
    return None

def current_wid():
    """从当前详情页 URL 里取出 WID（\w 不含连字符，WID 里若有 - 会被截断）"""
    m = re.search(r"WID=([\w\-]+)", driver.current_url)
    return m.group(1) if m else None

def fast_reload_detail(wid, wait=3):
    """用 SPA 内部路由跳转刷新详情页数据（实测 ~0.065s，比整页重载 ~0.6s 快约 9 倍）。
    先回列表并确认详情页已卸载，再回详情、等按钮渲染出来，确保读到的是新数据。"""
    if not wid:
        return False
    # 回列表，确认已离开详情页（详情页的大按钮消失）
    driver.execute_script("location.hash = '#/hdyy'")
    try:
        WebDriverWait(driver, 1).until(
            lambda d: "#/hdxq" not in d.current_url
            and not d.find_elements(By.CSS_SELECTOR, "button.ydd-button-large"))
    except:
        pass
    # 回详情，等按钮文字渲染出来
    driver.execute_script("location.hash = '#/hdxq?WID=%s'" % wid)
    try:
        WebDriverWait(driver, wait).until(
            lambda d: "#/hdxq" in d.current_url
            and any(b.text.strip() for b in d.find_elements(By.CSS_SELECTOR, "button.ydd-button-large")))
        return True
    except:
        return False

def click_detail_button():
    """在活动详情页点击文本为“预约”的大按钮；点到返回 True"""
    for b in driver.find_elements(By.CSS_SELECTOR, "button"):
        try:
            if b.text.strip() == "预约" and b.is_displayed() and b.is_enabled():
                b.click()
                return True
        except:
            continue
    return False

def _has_book_button():
    """详情页是否还有可点的「预约」大按钮（用来判断是不是已经约上了）"""
    try:
        for b in driver.find_elements(By.CSS_SELECTOR, "button.ydd-button-large"):
            txt = (b.text or "").strip()
            if b.is_displayed() and b.is_enabled() and "预约" in txt and "取消" not in txt:
                return True
    except Exception:
        pass
    return False


def _is_booked():
    """详情页大按钮变成「取消预约」= 这场已经在我们名下（约上了）。
    这是**最可靠的“成功”信号**——只有约上了才可能出现“取消预约”。
    原来完全没有这个判断：一旦约上，按钮从「预约」变「取消预约」，
    click_detail_button()（只认精确的"预约"）就再也点不动，
    循环会一直空转到超时、也不发成功邮件。"""
    try:
        for b in driver.find_elements(By.CSS_SELECTOR, "button.ydd-button-large"):
            if b.is_displayed() and "取消预约" in (b.text or ""):
                return True
    except Exception:
        pass
    return False


def _announce_success(info, subject="【SEU抢票】预约成功", shot=None, extra=""):
    """抢到就立刻发邮件（不等待、不攒批），并存一张截图作凭证"""
    if shot is None:
        shot = _state_path("shot_success.png")
    try:
        driver.save_screenshot(shot)
    except Exception:
        pass
    _body = (f"已预约：{info['title']}\n时间：{_fmt_time(info['start_time'])} ~ {_fmt_time(info['end_time'])}"
             f"\n校区：{info['campus']}")
    if extra:
        _body += "\n" + extra
    try:
        send_qq_mail(subject, _body)
    except Exception as e:
        print(f"邮件异常: {e}")


def _vcode_visible():
    """验证码输入框是否还显示着——用来判断“提交结果到了没有”：
    弹窗换成结果提示（成功/失败）时，验证码输入框会消失。"""
    try:
        return any(e.is_displayed() for e in driver.find_elements(By.ID, "vcodeInput"))
    except Exception:
        return False


def _popup_text():
    """当前可见的弹窗/提示文字（mint-ui msgbox / toast / jqalert 都收）"""
    parts = []
    for sel in (".mint-msgbox-message", ".mint-msgbox", ".mint-toast-content", "#jqalert", ".jqalert"):
        for e in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if e.is_displayed() and (e.text or "").strip():
                    parts.append(e.text.strip())
            except Exception:
                pass
    return " ".join(parts)


# ==================== 浏览器存活 / 自愈 ====================
# 浏览器窗口被手动关掉、或 chromedriver 进程崩掉时，driver 上的任何调用都会抛
# "no such window" / "invalid session id" / "web view not found" 之类的错。原来这种
# 异常被主循环的 except 原样吞掉 → 一整晚只打印报错、不发通知、也不重拉。
# 实测 2026-10-14 19:00 那次：用户关掉窗口后，run.log 空转了 1300+ 行同样的异常。
_DEAD_SESSION_MARKERS = (
    "no such window", "invalid session id", "target window already closed",
    "chrome not reachable", "disconnected", "not connected to devtools",
    "session deleted", "chrome instance exited", "web view not found",
)


def _is_dead_session(exc):
    s = str(exc).lower()
    return any(m in s for m in _DEAD_SESSION_MARKERS)


def _dead_probe():
    """探一次 driver 是否还活着。返回 True=活着 / False=已死 / None=瞬时错误（不作数）。
    - 会话类错误（no such window / invalid session id …）→ 直接判死；
    - 连接类错误（Connection refused / NewConnectionError：chromedriver 进程没了）→ 归为
      瞬时，交给 _browser_alive 再确认一次，避免偶发抖动把好浏览器误杀。"""
    try:
        driver.current_url
        return True
    except Exception as e:
        return False if _is_dead_session(e) else None


def _browser_alive():
    """driver 还能不能用（窗口是否还活着）。current_url 是最轻的探针。
    只有明确判死才返回 False——瞬时错误再确认一次，避免在抢票中途把好好的浏览器杀掉重建。"""
    r = _dead_probe()
    if r is None:
        time.sleep(0.5)
        r = _dead_probe()
    return bool(r)


def _recover_driver():
    """重建 webdriver（浏览器窗口被关/进程崩了之后）。成功返回 True。"""
    global driver
    try:
        driver.quit()
    except Exception:
        pass
    for _a in range(1, 4):
        try:
            driver = setup_driver()
            try:
                driver.set_page_load_timeout(20)
                driver.set_script_timeout(20)
            except Exception:
                pass
            print(f"🔄 浏览器已重建（第 {_a} 次尝试）")
            return True
        except Exception as e:
            print(f"⚠ 重建浏览器失败 {_a}/3: {e}")
            if _a < 3:
                time.sleep(5)
    return False


def _notify_browser_dead(reason):
    """浏览器彻底救不回来时的通知，一轮只发一次（状态文件去重，防看门狗每 5 分钟刷屏）。"""
    try:
        _p = _state_path(".seu_browser_notify")
        if os.path.exists(_p):
            return
        with open(_p, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass
    try:
        send_qq_mail("【SEU抢票】⚠ 浏览器已关闭/崩溃，抢票中断",
                     reason + "\n\n如果是你手动关掉的：请勿关闭脚本自己打开的那个浏览器窗口，"
                     "关掉后脚本无法继续（看门狗会尝试自动重拉，但成功率有限）。")
    except Exception as e:
        print(f"邮件异常: {e}")


def _revive_or_die(where):
    """浏览器死了就尝试自愈：重建 + 重登，整轮试 2 次。救不回来就发通知并退出，
    保持 .seu_active（_finished 仍为 False），交给看门狗重拉。"""
    for _try in (1, 2):
        print(f"💀 {where}：浏览器窗口已关闭或驱动已断开，尝试重建（第 {_try}/2 次）...")
        if _recover_driver() and login(redirect_wait=5):
            return True
        time.sleep(3)
    _notify_browser_dead(f"{where}时浏览器无法启动（窗口被关 / 驱动崩溃）。")
    print("❌ 浏览器无法重建，退出（保留 ACTIVE，交给看门狗）")
    raise SystemExit(1)


def do_captcha():
    """详情页预约弹窗的验证码流程。
    返回：True=确认预约成功；None=已提交但没拿到明确结果（疑似成功，需人工确认）；False=明确失败可重试。
    **刻意不做**“整页 page_source 里含‘成功’就判成功”——详情页静态文案里就可能出现“成功”二字
    （比如“预约成功后请按时参加”），那样会在没抢到时就误报成功、并让整个流程提前 return 退出。"""
    wait = WebDriverWait(driver, CONFIG["timeout_settings"]["element_wait"] // 2)
    vcode_img = wait.until(EC.presence_of_element_located((By.ID, "vcodeImg")))
    img_data = base64.b64decode(vcode_img.get_attribute("src").split("base64,")[1])
    captcha_text = ocr.classification(img_data)
    print(f"  验证码识别结果: {captcha_text}")
    vcode_input = wait.until(EC.presence_of_element_located((By.ID, "vcodeInput")))
    vcode_input.clear()
    vcode_input.send_keys(captcha_text)
    wait.until(EC.element_to_be_clickable((By.ID, "jqalert_yes_btn"))).click()

    # 点完“确定”后等结果。原来固定 sleep(action_confirm=0.2s) 才读结果——19:00 人多、
    # 服务器慢时响应根本还没回来，就会把“已经提交成功”的预约误判成失败，然后继续狂点。
    # 改成轮询：① 一出“预约成功”立刻返回；② 验证码输入框消失（弹窗已换成结果提示）
    # 就停止等待，交给下面的逻辑判失败/存疑。正常情况零点几秒，不拖慢快路径。
    popup = ""
    _deadline = time.time() + 3.0
    while time.time() < _deadline:
        popup = _popup_text()
        if "预约成功" in popup:
            return True
        if not _vcode_visible():
            break
        time.sleep(0.1)
    if popup:
        print(f"  弹窗提示: {popup[:80]}")
    try:
        page = driver.page_source or ""
    except Exception:
        page = ""
    # ① 明确成功：只认“预约成功”这个具体说法
    if ("预约成功" in popup) or ("预约成功" in page):
        return True
    # ①b 按钮已变「取消预约」= 铁定约上了（比弹窗文案可靠，别被残留的“验证码”字样带偏）
    if _is_booked():
        return True
    # ② 「预约」按钮已不可点 = 已经约上（成功时弹窗文案未必刚好是“预约成功”，也可能
    #    直接跳走）。这比关键字可靠，所以放在关键字前面判，免得被残留的“验证码”字样
    #    误判成失败、然后一直空转。→ 领上层发“疑似成功，请确认”。
    if not _has_book_button():
        return None
    # ③ 明确失败：验证码错 / 名额满 / 未开始 等 → 关提示，上层重试
    if any(k in popup for k in ("验证码", "失败", "错误", "已满", "已结束", "未开始", "不在预约")):
        for cls in ("mint-msgbox-btn", "jqalert_yes_btn"):
            try:
                driver.find_element(By.CLASS_NAME, cls).click()
                break
            except Exception:
                pass
        time.sleep(CONFIG["timeout_settings"]["retry_delay"])
        return False
    return False

def backflow_watch(targets, until_ts):
    """回流票监控：停在详情页、不停重载它，检测是否又有开放（有人退票）。
    前 backflow_dense_seconds 秒密集重载，之后每 backflow_slow_interval 秒重载一次；
    总时长由上层用 backflow_total_seconds 截断（默认只盯 60s 就收工）。"""
    titles = list(targets)
    dense_interval = CONFIG.get("backflow_interval", 3)
    slow_interval = CONFIG.get("backflow_slow_interval", 60)
    dense_seconds = CONFIG.get("backflow_dense_seconds", 60)
    start = time.time()
    cur = enter_detail(titles)  # 先停在目标详情页
    misses = 0
    while time.time() < until_ts:
        heartbeat()
        # 浏览器被关掉/崩了：自愈；救不回来就发通知并退出交给看门狗
        if not _browser_alive():
            _revive_or_die("回流监控")
            cur = enter_detail(titles)
            misses = 0
            continue
        try:
            # 刷新详情页状态（路由跳转 ~0.2s；失败则整页重载兜底）
            ok = fast_reload_detail(current_wid())
            if not ok:
                driver.refresh()
                try:
                    WebDriverWait(driver, 2).until(
                        lambda d: any(b.text.strip() for b in d.find_elements(By.CSS_SELECTOR, "button.ydd-button-large")))
                    ok = True
                except:
                    ok = False
            if ok:
                misses = 0
            else:
                misses += 1
                if misses >= 3:  # 详情页异常（被移除/掉登录）就回列表重新定位
                    if "auth.seu.edu.cn" in driver.current_url or "login" in driver.current_url:
                        print("🔁 登录失效，重新登录...")
                        login(redirect_wait=5)
                    cur = enter_detail(titles)
                    misses = 0
                    continue
            info = targets.get(cur, next(iter(targets.values())))
            # 「取消预约」= 已经约上了 → 报成功并停
            if _is_booked():
                print("  🎉 回流监控：详情页显示「取消预约」→ 已约上，退出")
                _announce_success(info, "【SEU抢票】回流票预约成功")
                return True
            if click_detail_button():
                print("  ♻️ 检测到开放，尝试预约...")
                res = do_captcha()
                if res is True:
                    _announce_success(info, "【SEU抢票】回流票预约成功")
                    return True
                elif res is None:
                    notify_ambiguous(info, where="回流票")
                    print("  ⚠ 疑似成功，继续监控（请人工确认）")
        except Exception as e:
            print(f"‼ 回流监控异常: {str(e).splitlines()[0]}")
            try:
                cur = enter_detail(titles)
            except:
                pass
        dense = (time.time() - start) < dense_seconds
        time.sleep(dense_interval if dense else slow_interval)
    return False

# ==================== 主流程控制 ====================
def precheck_and_notify():
    """18:50 预检查：扫描今天的讲座、按优先级排序、发通知邮件。
    返回 (targets, first)：有讲座时 targets={标题:讲座}(已按优先级插入，第一个即第一目标)、first=第一目标；
    没有符合条件的讲座则发“今天不抢”邮件并返回 (None, None)。"""
    print("🔎 18:50 预检查今天的讲座...")
    cards = scan_cards()
    # 先按条件筛，再按“今晚抢不抢得到”核日期：预约未截止、且不是明天以后才开放。
    # 不核日期的后果：列表里有 4 场时，会把下下周才开预约的排成第一目标，
    # 19:00 点进去按钮一直是「未开放」，空转一整晚，真正开放的那场反而没抢。
    _now = datetime.now()
    for c in cards:
        _kw = _excluded_keyword(c)
        if _kw:
            print(f"⏭ 跳过（标题含“{_kw}”）: {c['title'][:40]}")
    _matched = [c for c in cards if is_candidate(c)]
    for c in _matched:
        if not _grabs_tonight(c, _now):
            print(f"⏭ 跳过（不在今晚开放窗口）: {c['title'][:36]}｜预约 {c.get('open_time') or '?'} ~ {c.get('close_time') or '?'}")
    cands = sorted([c for c in _matched if _grabs_tonight(c, _now)], key=_sort_priority)
    for _i, c in enumerate(cands):
        _mode = "线上" if "线上" in c["title"] else "线下"
        _cp = c.get("host_campus") or c["campus"]
        _mark = "🎯 第一目标" if _i == 0 else "   备选"
        print(f"  {_mark}｜第{_lecture_tier(c) + 1}档 {_mode}/{_cp}｜{c['title'][:40]}")
    if not cands:
        if _matched:
            detail = "\n".join(f"· {c['title']}　预约 {c.get('open_time') or '?'} ~ {c.get('close_time') or '?'}"
                               for c in _matched)
            print(f"有 {len(_matched)} 场符合条件，但都不在今晚的开放窗口，跳过今天")
            send_qq_mail("【SEU抢票】今天不抢讲座",
                         f"18:50 预检查：有 {len(_matched)} 场符合筛选条件，但预约都已截止、"
                         f"或要等到明天以后才开放，今晚抢不了，跳过。\n\n{detail}")
        else:
            print("今天没有符合条件的讲座，跳过今天")
            send_qq_mail("【SEU抢票】今天不抢讲座",
                         "18:50 预检查：今天没有符合条件的讲座，跳过今天，不抢。")
        return None, None
    targets = {c["title"]: c for c in cands}  # 已按优先级插入，第一个即第一目标
    first = cands[0]
    lines = [
        "今晚要抢的讲座（18:50 预检查）：",
        "",
        f"【第一目标】{first['title']}",
        f"  类别：{first.get('category', '?')}",
        f"  形式：{'线上' if '线上' in first['title'] else '线下'}（第 {_lecture_tier(first) + 1} 档）",
        f"  校区：{first.get('host_campus') or first.get('campus', '?')}",
        f"  讲座时间：{_fmt_time(first.get('start_time'))} ~ {_fmt_time(first.get('end_time'))}",
        f"  预约窗口：{first.get('open_time', '?')} ~ {first.get('close_time', '?')}",
    ]
    if len(cands) > 1:
        lines.append("")
        lines.append(f"【备选 {len(cands) - 1} 场】按优先级：")
        for i, c in enumerate(cands[1:], 1):
            _mode = "线上" if "线上" in c["title"] else "线下"
            lines.append(f"  {i}) 第{_lecture_tier(c) + 1}档 {_mode}/{c.get('host_campus') or c.get('campus', '?')}"
                         f"｜{c['title']}｜开放 {c.get('open_time', '?')}")
    lines += ["", "19:00 进入详情页点“预约”。抢到 / 没抢到 / 转回流监控都会再通知你。"]
    send_qq_mail(f"【SEU抢票】今晚目标：{first['title'][:24]}", "\n".join(lines))
    print(f"✅ 发现 {len(cands)} 场，第一目标: {first['title']}")
    return targets, first


def main_process():
    """主流程：18:50 预检查 → 18:59 定位 → 19:00 进详情页点“预约”+验证码"""
    # 1) 预检查：今天有没有符合条件的讲座（忽略按钮状态）
    targets, first = precheck_and_notify()
    if not targets:
        return

    # 2) 18:59 刷新，确认目标仍在列表里
    wait_until(18, 59, "刷新定位")
    cards = scan_cards()
    keep = {c["title"]: c for c in cards if c["title"] in targets}
    if keep:
        targets = keep
    print(f"🎯 18:59 目标: {list(targets)}")
    # 提前一点点进入详情页待命：19:00 一到就只剩“点按钮”这一步
    print(f"📄 已提前进入详情页待命: {enter_detail(list(targets))}")

    # 3) 19:00 准点：已提前在详情页；按钮不会自己变，必须不停刷新详情页
    titles = list(targets)
    open_ts = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0).timestamp()
    # 19:00 准时开始（不提前起手）
    while time.time() < open_ts:
        time.sleep(0.02)
    _mark_grab_start(open_ts)  # 计时起点倒推到 19:00:00.000
    print("🛒 19:00 准时开抢（不停刷新详情页）...")
    end = open_ts + 270  # 最多抢 4.5 分钟（cron 兜底停止）
    current = next(iter(targets), None)  # 已提前进入详情页
    stuck = 0  # 连续刷新失败的次数，用于退避（人多/响应慢时别狂轰）
    while time.time() < end:
        heartbeat()
        # 浏览器被关掉/崩了：原地自愈（重建 + 重登）；救不回来就发通知并退出交给看门狗。
        # 没有这段的话，窗口一关就只会一直抛 "no such window"，空转到超时也不通知。
        if not _browser_alive():
            _revive_or_die("19:00 抢票")
            current = enter_detail(titles)
            continue
        try:
            # 不在详情页就先进目标详情页
            if "#/hdxq" not in driver.current_url:
                current = enter_detail(titles)
                if not current:
                    stuck += 1
                    time.sleep(min(0.05 * stuck, 2.0))
                    continue
            # 刷新详情页状态——用 SPA 路由跳转（~0.07s），比整页重载（~0.6s）快约 9 倍
            if not fast_reload_detail(current_wid()):
                # 刷不出来（响应慢/出错）：退避；连退 3 次再兜底整页重载一次
                stuck += 1
                if stuck >= 3:
                    try:
                        driver.refresh()
                        WebDriverWait(driver, 3).until(
                            lambda d: any(b.text.strip() for b in d.find_elements(By.CSS_SELECTOR, "button.ydd-button-large")))
                    except Exception:
                        pass
                time.sleep(min(0.2 * stuck, 2.0))
                continue
            stuck = 0
            t = targets.get(current, next(iter(targets.values())))
            # 「取消预约」= 已经约上了 → 最可靠的信号，立刻报成功并停（别再空转）
            if _is_booked():
                print(f"🎉 详情页已显示「取消预约」→ 确认已约上，抢到用时 {_grab_elapsed_str()}")
                _announce_success(t, extra=f"⏱ 抢到用时：{_grab_elapsed_str()}（自 19:00:00 起）")
                return
            if click_detail_button():
                print("  已点击“预约”按钮，处理验证码...")
                res = do_captcha()
                if res is True:
                    print(f"🎉 抢到！用时 {_grab_elapsed_str()}（自 19:00:00 起）")
                    _announce_success(t, extra=f"⏱ 抢到用时：{_grab_elapsed_str()}（自 19:00:00 起）")
                    return
                elif res is None:
                    try:
                        driver.save_screenshot(_state_path("shot_ambiguous.png"))
                    except Exception:
                        pass
                    print("  ⚠ 已提交但未拿到明确成功提示 → 发“疑似成功”通知（只发一次）")
                    notify_ambiguous(t)
                else:
                    print("  验证码失败，继续重试...")
            # 掉登录（跳认证页）就重登；抢票窗口内用短等待，别耗 120s
            if "auth.seu.edu.cn" in driver.current_url or "login" in driver.current_url:
                print("🔁 登录失效，重新登录...")
                login(redirect_wait=5)
                current = next(iter(targets), None)
                continue
        except Exception as e:
            print(f"‼ 抢票异常: {str(e).splitlines()[0]}")  # 只打首行：selenium 的长 stacktrace
            stuck += 1                                    # 会把日志刷爆（实测一晚上 1300+ 行）
            time.sleep(min(0.2 * stuck, 2.0))
            continue
        time.sleep(0.02)

    # 4) 回流票监控：不停进出详情页，检测是否又有开放（直到预约截止时间）
    if CONFIG.get("watch_backflow", True):
        until_ts = None
        for _c in targets.values():
            _ct = _c.get("close_time")
            if _ct:
                try:
                    _t = datetime.strptime(_ct, "%Y/%m/%d %H:%M").timestamp()
                    until_ts = _t if until_ts is None else max(until_ts, _t)
                except:
                    pass
        if until_ts is None or until_ts <= time.time():
            until_ts = time.time() + 3600
        # 总时长上限：默认只盯 backflow_total_seconds 秒（默认 60s），到点就收工、进程退出，
        # 不整晚挂着。想盯更久把 backflow_total_seconds 调大即可。
        _cap = CONFIG.get("backflow_total_seconds", 60)
        if _cap and _cap > 0:
            until_ts = min(until_ts, time.time() + _cap)
        print(f"♻️ 开始回流票监控，直到 {datetime.fromtimestamp(until_ts):%Y/%m/%d %H:%M:%S}")
        send_qq_mail("【SEU抢票】转为监控回流票",
                     f"19:00 未抢到，转为回流票监控：约 {int(_cap)} 秒内每 {CONFIG.get('backflow_interval', 3)} 秒查一次，"
                     f"到 {datetime.fromtimestamp(until_ts):%H:%M:%S} 结束。抢到会通知你。")
        if backflow_watch(targets, until_ts):
            print("🎉 回流票抢到，退出")
            return
        print("♻️ 回流监控结束（已达监控时长上限 / 预约截止），收工")

# ==================== 程序入口 ====================
if __name__ == "__main__":
    # 看门狗状态：本轮开始 → 写 ACTIVE + PID（guard 靠 PID 判活，不用 pgrep 免得自匹配）+ 打一次心跳
    clear_state(".seu_done")
    mark_state(".seu_active", str(int(time.time())))
    mark_state(".seu_pid", str(os.getpid()))
    heartbeat()
    _finished = False  # True = 正常收尾；异常退出时保持 False，留给 guard 重拉
    try:
        try:
            # 执行登录，检查是否成功
            if login():
                print("🚀 启动讲座扫描流程...")
                main_process()  # 启动主流程
                _finished = True
            else:
                # 刻意不标 DONE：留 ACTIVE 让看门狗重拉（重启上限 20 次兜底，不会无限重登；
                # 失败提醒已由 _notify_login_problem 当天去重发过一遍）。
                # 原来这里标 DONE = 看门狗彻底放弃 → 一次登录失败就整晚静默没戏。
                print("❌ 登录失败，保留 ACTIVE 交给看门狗重试")
        except KeyboardInterrupt:
            print("\n👋 用户中断程序")
            _finished = True
        # 正常收尾：标 DONE、清 ACTIVE、重置重启计数，看门狗不会再拉起
        if _finished:
            mark_state(".seu_done", str(int(time.time())))
            clear_state(".seu_active", ".seu_restarts", ".seu_pid")
    except Exception as e:
        print(f"\n❌ 程序异常: {str(e)}")
        import traceback
        traceback.print_exc()
        # 故意保留 .seu_active、不写 .seu_done —— 交由 guard 重拉
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("🚗 浏览器已关闭")