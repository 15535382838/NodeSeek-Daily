# -- coding: utf-8 -*-
"""
NodeSeek 自动签到 + AI 评论脚本（DeepSeek 版）
依赖: undetected_chromedriver, selenium, openai
环境变量:
  - NS_COOKIE / COOKIE: 登录 Cookie（必填）
  - DEEPSEEK_API_KEY: DeepSeek API Key（必填，用于AI评论）
  - NS_RANDOM: true=试试手气 / false=鸡腿x5（默认false）
  - HEADLESS: true/false（默认true）
  - COMMENT_COUNT: 评论帖子数（默认3，少而精）
  - DEEPSEEK_MODEL: 模型名（默认 deepseek-v4-flash，便宜够用）
  - CHROME_BIN: Chrome/Chromium 路径（青龙环境需指定）
  - AI_FALLBACK: true/false（默认false；AI失败/无Key时直接跳过该帖，不发水评论）
  - DRY_RUN: true/false（默认false；true时只生成评论不发布，先审质量）
  - COMMENT_INTERVAL_MIN/MAX: 成功评论之间的间隔秒数（默认45/90）
  - HISTORY_FILE: 已评论帖子记录文件（默认 commented_posts.json，用于跨天去重）
  - CATEGORIES: 评论的版块，逗号分隔（默认 trade）
  - LOG_LEVEL: 日志级别（默认 INFO）
  - MAX_RUNTIME_MINUTES: 单次运行最长分钟数（默认 30，超时即停）
  - HISTORY_DAYS: 历史记录保留天数（默认 90）
  - DISABLE_IMAGES: true/false（默认 true，禁图片可显著加快页面加载）
"""

import json
import logging
import os
import re
import sys
import random
import time
import traceback

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

from openai import OpenAI

# ========== 配置 ==========
def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        log.warning(f"环境变量 {name} 非法，使用默认值 {default}")
        return default

def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        log.warning(f"环境变量 {name} 非法，使用默认值 {default}")
        return default

NS_RANDOM = _env_bool("NS_RANDOM", False)
COOKIE_STR = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
HEADLESS = _env_bool("HEADLESS", True)
COMMENT_COUNT = _env_int("COMMENT_COUNT", 3)
CHROME_BIN = os.environ.get("CHROME_BIN", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
AI_FALLBACK = _env_bool("AI_FALLBACK", False)
DRY_RUN = _env_bool("DRY_RUN", False)
COMMENT_INTERVAL_MIN = _env_float("COMMENT_INTERVAL_MIN", 45)
COMMENT_INTERVAL_MAX = _env_float("COMMENT_INTERVAL_MAX", 90)
HISTORY_FILE = os.environ.get("HISTORY_FILE", "commented_posts.json")
MIN_COMMENT_LEN = _env_int("MIN_COMMENT_LEN", 20)
MAX_COMMENT_LEN = _env_int("MAX_COMMENT_LEN", 90)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
CATEGORIES = [c.strip() for c in os.environ.get("CATEGORIES", "trade").split(",") if c.strip()] or ["trade"]
MAX_RUNTIME_MINUTES = _env_float("MAX_RUNTIME_MINUTES", 30)
HISTORY_DAYS = _env_int("HISTORY_DAYS", 90)
DISABLE_IMAGES = _env_bool("DISABLE_IMAGES", True)
PAGE_TIMEOUT = _env_int("PAGE_TIMEOUT", 30)
WAIT_TIMEOUT = _env_int("WAIT_TIMEOUT", 15)

# 配置自检：区间写反、数量越界时自动纠正，而不是静默跑出奇怪行为
if COMMENT_INTERVAL_MIN > COMMENT_INTERVAL_MAX:
    COMMENT_INTERVAL_MIN, COMMENT_INTERVAL_MAX = COMMENT_INTERVAL_MAX, COMMENT_INTERVAL_MIN
if COMMENT_COUNT < 0:
    COMMENT_COUNT = 0
if MIN_COMMENT_LEN > MAX_COMMENT_LEN:
    MIN_COMMENT_LEN, MAX_COMMENT_LEN = MAX_COMMENT_LEN, MIN_COMMENT_LEN

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nodeseek")

# 降级用的随机语料：原版的 "bd / 绑定 / mark" 已删除。
# 即使开启 AI_FALLBACK，也只用下面这种带帖子主题、完整句子的模板，
# 绝不发两三个字的灌水评论。无主题时返回 None（直接跳过该帖）。
FALLBACK_TEMPLATES = [
    "看到{topic}这个点挺感兴趣的，实际用起来稳定性怎么样？",
    "关于{topic}，想请教一下楼主在具体场景下是怎么取舍的？",
    "{topic}这部分写得很具体，之前也遇到过类似情况，确实值得留意。",
    "对{topic}这个做法有点好奇，它和常规方案相比最大的取舍在哪？",
]

def topic_from_title(title):
    """从标题里取一个有意义的主题词，用于降级模板；取不到返回空。"""
    import re as _re
    t = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9 ]", " ", title or "")
    stop = {"这个", "一个", "怎么", "什么", "可以", "就是", "还是", "以及", "不过", "但是", "分享", "讨论", "求助", "教程"}
    for w in t.split():
        if len(w) >= 2 and w not in stop:
            return w
    m = _re.findall(r"[\u4e00-\u9fff]{2,6}", title or "")
    for w in m:
        if w not in stop:
            return w
    return ""

def build_fallback_comment(title):
    topic = topic_from_title(title)
    if not topic:
        return None
    return random.choice(FALLBACK_TEMPLATES).format(topic=topic)

# AI 评论风格：只保留“必须结合帖子具体内容”的四种，删掉围观式、感谢式
COMMENT_STYLES = [
    "提问式：针对帖子里的一个具体细节，提出一个有针对性的问题",
    "经验补充式：结合自己的类似经历，补充一个具体的点，不要只说支持",
    "观点补充式：对楼主的一个具体观点做简短补充或不同角度的看法",
    "细节追问式：追问帖子里没讲清楚的一个具体环节，比如成本、稳定性、适用场景",
]

# ========== 评论质检：这是“不水”的核心门槛 ==========
GENERIC_COMMENTS = {
    "bd", "绑定", "帮顶", "顶一下", "mark", "路过看看", "感谢分享",
    "收藏了", "学习了", "不错不错", "支持一下", "留个脚印", "看看先",
    "支持", "不错", "顶", "好帖", "赞", "路过", "看看",
}
STOPWORDS = {"这个", "一个", "怎么", "什么", "可以", "就是", "还是", "以及", "不过", "但是", "分享", "讨论"}

def extract_keywords(title):
    import re as _re
    # 中英文数字混写也当一个词，例如标题里的“4核8G”要整体保留，不要拆散
    words = _re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", title or "")
    return [w for w in words if w not in STOPWORDS]

def is_quality_comment(comment, title):
    """不通过就返回 False，调用方应重试或跳过该帖，绝不将就发布。"""
    if not comment:
        return False
    c = comment.strip()
    if c in GENERIC_COMMENTS:
        return False
    if len(c) < MIN_COMMENT_LEN or len(c) > MAX_COMMENT_LEN:
        return False
    # 纯泛泛而谈：整句几乎没有实词
    if len(c) <= 12 and not any(k in c for k in extract_keywords(title)):
        return False
    # 必须沾上标题里的至少一个关键词（允许部分重合，例如标题“够不够用”、
    # 评论只写“够吗”也算沾边）；标题实在取不出关键词时放行（靠长度+AI提示兜底）
    kws = extract_keywords(title)
    def _related(kw):
        if kw.lower() in c.lower():
            return True
        return any(kw[i:i+2] in c for i in range(len(kw) - 1))
    if kws and not any(_related(k) for k in kws):
        return False
    # 不能只是复述标题
    if title and c == title.strip():
        return False
    return True

# ========== DeepSeek AI 客户端 ==========
_ai_client = None

def get_ai_client():
    global _ai_client
    if _ai_client is None:
        if not DEEPSEEK_API_KEY:
            return None
        _ai_client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
        )
    return _ai_client


def generate_ai_comment(post_title, post_content):
    """
    根据帖子内容生成 AI 评论
    成功返回评论文本，失败返回 None
    """
    client = get_ai_client()
    if not client:
        return None

    # 截断内容，避免 token 过多（帖子一般不长，这里保守限制）
    title = (post_title or "").strip()[:200]
    content = (post_content or "").strip()[:2000]

    if not title and not content:
        return None

    # 最多换两种风格各试一次，两次都不达标就返回 None（由调用方跳过）
    for attempt in range(2):
        style = random.choice(COMMENT_STYLES)
        comment = _generate_once(client, title, content, style)
        if comment and is_quality_comment(comment, title):
            return comment
        if comment:
            log.warning(f"AI 评论未通过质检（第{attempt+1}次）: {comment}")
        if attempt == 0:
            random_sleep(1, 3)
    return None


def _generate_once(client, title, content, style):
    system_prompt = """你是一个真实的 NodeSeek 论坛用户，正在浏览帖子并写评论。
硬性要求，缺一条就不合格：
1. 30-80 个字，必须提到帖子里的一个具体细节、做法或问题，不能只写感受
2. 要么提出一个针对性的问题，要么补充一个具体的看法，二选一，不要两者都泛泛写
3. 口语化、自然，不要书面语，不要有 AI 感
4. 禁止出现这些灌水写法：bd、绑定、帮顶、支持一下、不错不错、路过、感谢分享、mark
5. 不要用"作为 AI"、"我认为"开头，不要重复帖子标题，不要复述帖子内容
6. 不要加表情符号
7. 直接输出评论内容，不要加引号、不要解释"""

    user_prompt = f"""帖子标题：{title}
帖子内容：{content}

请用以下风格写一条评论：{style}
直接输出评论内容，不要任何其他文字。"""

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,  # 高温度，增加多样性
            max_tokens=100,
            timeout=15,
        )
        comment = response.choices[0].message.content.strip()
        # 清理：去掉可能的引号、多余换行
        comment = comment.strip('"').strip("'").strip()
        comment = comment.split("\n")[0].strip()  # 只取第一行

        return comment
    except Exception as e:
        log.error(f"AI 生成评论失败: {e}")
        return None



# ========== 通用工具：Cookie 解析 / 正文清洗 / 历史记录 ==========
def parse_cookies(cookie_str):
    """解析 Cookie 字符串，返回去重后的 (name, value) 列表；非法片段直接丢弃。"""
    out, seen = [], set()
    for item in (cookie_str or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip().strip('"')
        if name and value and name not in seen:
            seen.add(name)
            out.append((name, value))
    return out

def clean_post_text(text):
    """去掉代码块标记、多余空白和超长尾巴，AI 读到的才是帖子正文。"""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"`[^`]+`", " ", t)
    return " ".join(t.split())

def load_history(path, days=90):
    """兼容旧版 list 格式；新版为 {url: timestamp}，并按天数清理过期记录。"""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, list):
        return {u: time.time() for u in data}
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - days * 86400
    return {u: ts for u, ts in data.items() if isinstance(ts, (int, float)) and ts >= cutoff}

def save_history(path, history):
    """原子写入：先写临时文件再替换，避免中途被杀导致历史文件损坏。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

# ========== 工具函数 ==========
def random_sleep(min_sec=1, max_sec=3):
    time.sleep(random.uniform(min_sec, max_sec))


def human_scroll(driver, direction="down", times=1):
    for _ in range(times):
        if direction == "down":
            driver.execute_script(f"window.scrollBy(0, {random.randint(200, 500)});")
        else:
            driver.execute_script(f"window.scrollBy(0, -{random.randint(200, 500)});")
        random_sleep(0.3, 0.8)


def extract_post_content(driver):
    """提取帖子的标题和正文"""
    title = ""
    content = ""

    try:
        # 标题
        title_elem = driver.find_element(By.CSS_SELECTOR, "h1.post-title, .nsk-post-title, .title h1")
        title = title_elem.text.strip()
    except Exception:
        try:
            title = driver.title.strip()
        except Exception:
            pass

    try:
        # 正文（取第一个帖子的内容区域）
        content_elem = driver.find_element(
            By.CSS_SELECTOR,
            ".nsk-post .post-content, .post-body .content, .topic-content"
        )
        content = clean_post_text(content_elem.text)
    except Exception:
        # 降级：取页面主要文本
        try:
            main = driver.find_element(By.CSS_SELECTOR, ".post-list-item, .topic-body")
            content = clean_post_text(main.text)[:2000]
        except Exception:
            pass

    return title, content


# ========== 浏览器初始化 ==========
def setup_driver():
    if not COOKIE_STR:
        log.error("未配置 NS_COOKIE 环境变量")
        return None

    log.info("初始化浏览器...")
    options = uc.ChromeOptions()

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-extensions")
    if DISABLE_IMAGES:
        options.add_argument("--blink-settings=imagesEnabled=false")

    if HEADLESS:
        log.info("启用无头模式")
        options.add_argument("--headless=new")

    kwargs = {"options": options}
    if CHROME_BIN and os.path.exists(CHROME_BIN):
        log.info(f"使用指定 Chrome: {CHROME_BIN}")
        kwargs["browser_executable_path"] = CHROME_BIN

    driver = None
    for attempt in range(2):
        try:
            driver = uc.Chrome(**kwargs)
            driver.set_page_load_timeout(PAGE_TIMEOUT)
            log.info("Chrome 启动成功")
            break
        except Exception as e:
            log.error(f"Chrome 启动失败（第{attempt+1}次）: {e}")
            random_sleep(3, 5)
    if not driver:
        log.info("青龙面板请安装 chromium，并通过 CHROME_BIN 指定路径")
        return None

    # 设置 Cookie
    log.info("设置登录 Cookie...")
    driver.get("https://www.nodeseek.com")
    random_sleep(2, 4)

    success_count = 0
    for name, value in parse_cookies(COOKIE_STR):
        try:
            driver.add_cookie({"name": name, "value": value, "domain": ".nodeseek.com", "path": "/"})
            success_count += 1
        except Exception as e:
            log.warning(f"Cookie 设置失败 [{name}]: {e}")

    log.info(f"成功设置 {success_count} 个 Cookie")
    driver.refresh()
    random_sleep(3, 5)

    return driver


# ========== 签到 ==========
def do_sign(driver):
    print("\n[STEP 1] 执行签到...")
    try:
        driver.get("https://www.nodeseek.com/board")
        random_sleep(3, 5)

        btn_text = "试试手气" if NS_RANDOM else "鸡腿 x 5"
        try:
            btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(text(), '{btn_text}')]"))
            )
            driver.execute_script("arguments[0].click();", btn)
            log.info(f"已点击签到按钮: {btn_text}")
            random_sleep(2, 4)
            return True
        except Exception:
            log.info("今日已签到或按钮不存在")
            return True
    except Exception as e:
        log.error(f"签到异常: {e}")
        return False


# ========== 加鸡腿 ==========
def click_chicken_leg(driver):
    try:
        chicken_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (By.XPATH, '//div[contains(@class,"nsk-post")]//div[@title="加鸡腿"][1]')
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", chicken_btn)
        random_sleep(0.5, 1)
        chicken_btn.click()

        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".msc-confirm"))
        )
        random_sleep(0.5, 1)

        # 检查是否超过7天
        try:
            driver.find_element(By.XPATH, "//*[contains(text(), '7天前') or contains(text(), '七天前')]")
            log.info("帖子超过7天，无法加鸡腿")
            ok_btn = driver.find_element(By.CSS_SELECTOR, ".msc-confirm .msc-ok")
            ok_btn.click()
            return False
        except Exception:
            pass

        ok_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".msc-confirm .msc-ok"))
        )
        ok_btn.click()
        log.info("加鸡腿成功")

        WebDriverWait(driver, 5).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".msc-overlay"))
        )
        random_sleep(1, 2)
        return True
    except Exception as e:
        log.info(f"加鸡腿跳过: {str(e)[:60]}")
        return False


# ========== AI 评论 ==========
def do_comment(driver):
    ai_enabled = DEEPSEEK_API_KEY != ""
    print(f"\n[STEP 2] 执行评论（目标 {COMMENT_COUNT} 帖，AI评论: {'开' if ai_enabled else '关'}）...")

    if not ai_enabled:
        log.warning("未配置 DEEPSEEK_API_KEY：只做签到和加鸡腿，不发评论（除非 AI_FALLBACK=true）")

    # 跨天去重：已评论过的帖子不再重复水一遍
    commented = load_history(HISTORY_FILE, HISTORY_DAYS)
    run_started = time.time()

    try:
        # 多版块收集：默认只看 trade，可用 CATEGORIES=trade,tech 扩展；去重、凑够候选即停
        normal_posts, _seen = [], set()
        for category in CATEGORIES:
            if len(normal_posts) >= COMMENT_COUNT * 3:
                break
            try:
                driver.get(f"https://www.nodeseek.com/categories/{category}")
                random_sleep(3, 5)
                posts = WebDriverWait(driver, WAIT_TIMEOUT).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".post-list-item"))
                )
                log.info(f"版块 {category} 获取到 {len(posts)} 个帖子")
                for post in posts:
                    try:
                        if not post.find_elements(By.CSS_SELECTOR, ".pinned, .is-pinned"):
                            href = post.find_element(By.CSS_SELECTOR, ".post-title a").get_attribute("href")
                            if href and href not in _seen:
                                _seen.add(href)
                                normal_posts.append(href)
                    except Exception:
                        continue
            except Exception as e:
                log.warning(f"版块 {category} 获取失败，跳过: {str(e)[:80]}")
                continue

        if not normal_posts:
            log.warning("未找到可评论的帖子")
            return

        # 候选池放大 3 倍：因为短帖、已评论、质检不过的都会被跳过，
        # 目标是“发满 3 条好评论”，而不是“凑满 5 条水评论”
        pool = [u for u in normal_posts if u not in commented]
        random.shuffle(pool)
        selected = pool[: COMMENT_COUNT * 3]
        log.info(f"候选 {len(selected)} 个帖（已排除历史已评论 {len(normal_posts) - len(pool)} 个）")

        success = 0
        ai_success = 0
        skipped = 0
        for idx, url in enumerate(selected):
            if success >= COMMENT_COUNT:
                break
            if time.time() - run_started > MAX_RUNTIME_MINUTES * 60:
                log.warning(f"已达单次运行上限 {MAX_RUNTIME_MINUTES} 分钟，提前结束")
                break
            print(f"\n--- [{idx+1}/{len(selected)}] {url} ---")
            try:
                driver.get(url)
                random_sleep(2, 4)

                # 模拟阅读：按正文长度给阅读时间，短帖直接跳过不评论
                human_scroll(driver, "down", random.randint(2, 4))
                post_title_tmp, post_content_tmp = extract_post_content(driver)
                if len(post_content_tmp) < 80:
                    log.info("正文太短（<80 字），无实质内容可评，跳过")
                    skipped += 1
                    continue
                random_sleep(3, 6)

                # 加鸡腿
                click_chicken_leg(driver)

                # 上面已提取过一次，直接复用，避免重复查 DOM（原版在这里又查了一遍）
                post_title, post_content = post_title_tmp, post_content_tmp
                log.info(f"帖子标题: {post_title[:50]}...")

                # 生成评论
                comment_text = None
                if ai_enabled:
                    log.info("正在调用 DeepSeek 生成评论...")
                    comment_text = generate_ai_comment(post_title, post_content)
                    if comment_text:
                        ai_success += 1
                        log.info(f"AI 评论: {comment_text}")
                    elif AI_FALLBACK:
                        comment_text = build_fallback_comment(post_title)
                        if not comment_text:
                            log.info("AI 失败且无可用主题做降级评论，跳过此帖")
                            skipped += 1
                            continue
                        log.info(f"AI 失败，降级评论: {comment_text}")
                    else:
                        log.warning("AI 生成失败且未启用降级，跳过此帖")
                        continue
                else:
                    if not AI_FALLBACK:
                        log.info("未启用 AI 且未开启降级，不发水评论，跳过此帖")
                        skipped += 1
                        continue
                    comment_text = build_fallback_comment(post_title)
                    if not comment_text:
                        log.info("无可用主题做降级评论，跳过此帖")
                        skipped += 1
                        continue
                    log.info(f"降级评论: {comment_text}")

                if DRY_RUN:
                    log.info(f"本次不发布，仅预览评论: {comment_text}")
                    success += 1
                    continue

                # 找到编辑器并输入
                editor = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".CodeMirror"))
                )
                editor.click()
                random_sleep(0.5, 1)

                actions = ActionChains(driver)
                for char in comment_text:
                    actions.send_keys(char)
                    actions.pause(random.uniform(0.08, 0.25))
                actions.perform()
                random_sleep(1, 2)

                # 发布
                submit_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(@class, 'submit') and contains(text(), '发布评论')]")
                    )
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_btn)
                random_sleep(0.5, 1)

                try:
                    submit_btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", submit_btn)

                log.info(f"评论发布成功")
                success += 1

                # 成功后写入历史，避免以后重复评论同一帖
                commented[url] = time.time()
                try:
                    save_history(HISTORY_FILE, commented)
                except Exception as e:
                    log.warning(f"历史记录写入失败: {e}")
                # 间隔拉长到 45-90 秒：宁可慢，也别像刷屏
                random_sleep(COMMENT_INTERVAL_MIN, COMMENT_INTERVAL_MAX)

            except Exception as e:
                log.warning(f"评论失败: {str(e)[:80]}")
                random_sleep(3, 6)
                continue

        print(f"\n[INFO] 评论完成: 成功 {success}/{COMMENT_COUNT}，AI生成成功 {ai_success}，跳过 {skipped}")

    except Exception as e:
        log.error(f"评论任务异常: {e}")
        traceback.print_exc()


# ========== 主流程 ==========
def main():
    # 命令行可覆盖环境变量，方便手动试跑：--dry-run / --comment-count 2 / --categories trade,tech
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--dry-run", action="store_true")
    _ap.add_argument("--comment-count", type=int, default=None)
    _ap.add_argument("--categories", default=None)
    _args = _ap.parse_args()
    global DRY_RUN, COMMENT_COUNT, CATEGORIES
    if _args.dry_run:
        DRY_RUN = True
    if _args.comment_count is not None:
        COMMENT_COUNT = max(0, _args.comment_count)
    if _args.categories:
        CATEGORIES = [c.strip() for c in _args.categories.split(",") if c.strip()] or CATEGORIES
    print("=" * 55)
    print("  NodeSeek 自动签到 + AI 评论（DeepSeek 版）")
    print(f"  随机签到: {'开' if NS_RANDOM else '关'} | 无头模式: {'开' if HEADLESS else '关'}")
    print(f"  AI 模型: {DEEPSEEK_MODEL if DEEPSEEK_API_KEY else '未启用'}")
    print(f"  评论数量: {COMMENT_COUNT}（上限，质检不过宁缺毋滥）")
    print(f"  Dry Run: {'开（只生成不发布）' if DRY_RUN else '关'} | 降级水评论: {'开' if AI_FALLBACK else '关（默认）'}")
    print("=" * 55)

    driver = None
    try:
        driver = setup_driver()
        if not driver:
            sys.exit(1)

        do_sign(driver)
        do_comment(driver)

    except KeyboardInterrupt:
        print("\n[INFO] 用户中断")
    except Exception as e:
        log.error(f"主流程异常: {e}")
        traceback.print_exc()
    finally:
        if driver:
            try:
                driver.quit()
                log.info("浏览器已关闭")
            except Exception:
                pass

    print("\n[DONE] 脚本执行完毕")


if __name__ == "__main__":
    main()
