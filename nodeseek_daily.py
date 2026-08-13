# -- coding: utf-8 -*-
"""
NodeSeek 自动签到 + AI 评论脚本（DeepSeek 版）
依赖: undetected_chromedriver, selenium, openai
环境变量:
  - NS_COOKIE / COOKIE: 登录 Cookie（必填）
  - DEEPSEEK_API_KEY: DeepSeek API Key（必填，用于AI评论）
  - NS_RANDOM: true=试试手气 / false=鸡腿x5（默认false）
  - HEADLESS: true/false（默认true）
  - COMMENT_COUNT: 评论帖子数（默认5）
  - DEEPSEEK_MODEL: 模型名（默认 deepseek-v4-flash，便宜够用）
  - CHROME_BIN: Chrome/Chromium 路径（青龙环境需指定）
  - AI_FALLBACK: true/false（默认true，AI失败时回退到随机语料）
"""

import os
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
NS_RANDOM = os.environ.get("NS_RANDOM", "false").lower() == "true"
COOKIE_STR = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
COMMENT_COUNT = int(os.environ.get("COMMENT_COUNT", "5"))
CHROME_BIN = os.environ.get("CHROME_BIN", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
AI_FALLBACK = os.environ.get("AI_FALLBACK", "true").lower() == "true"

# 降级用的随机语料
FALLBACK_COMMENTS = [
    "bd", "绑定", "帮顶", "顶一下", "mark",
    "路过看看", "感谢分享", "收藏了", "学习了",
    "不错不错", "支持一下", "留个脚印", "看看先",
]

# AI 评论风格（随机选一种，增加多样性）
COMMENT_STYLES = [
    "提问式：针对帖子内容提出一个具体的小问题",
    "赞同式：表达对楼主观点的认同，可以补充一点自己的看法",
    "感谢式：感谢楼主分享，说一下自己的收获",
    "补充式：对帖子内容做一个简短的补充或延伸",
    "围观式：路过围观，表达一下关注，说一句相关的感受",
    "经验式：结合自己类似的经历，说一两句简短感受",
]

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
    content = (post_content or "").strip()[:800]

    if not title and not content:
        return None

    style = random.choice(COMMENT_STYLES)

    system_prompt = """你是一个真实的 NodeSeek 论坛用户，正在浏览帖子并写评论。
要求：
1. 评论必须简短，10-50个字之间，像真实用户的随手回复
2. 口语化、自然，不要书面语，不要有AI感
3. 必须结合帖子的具体内容，不能泛泛而谈
4. 不要用"作为AI"、"我认为"等开头
5. 不要重复帖子标题，不要复述帖子内容
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

        # 长度校验
        if len(comment) < 2 or len(comment) > 100:
            print(f"[WARN] AI 评论长度异常: {len(comment)} 字，跳过")
            return None

        return comment
    except Exception as e:
        print(f"[ERROR] AI 生成评论失败: {e}")
        return None


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
        content = content_elem.text.strip()
    except Exception:
        # 降级：取页面主要文本
        try:
            main = driver.find_element(By.CSS_SELECTOR, ".post-list-item, .topic-body")
            content = main.text.strip()[:500]
        except Exception:
            pass

    return title, content


# ========== 浏览器初始化 ==========
def setup_driver():
    if not COOKIE_STR:
        print("[ERROR] 未配置 NS_COOKIE 环境变量")
        return None

    print("[INFO] 初始化浏览器...")
    options = uc.ChromeOptions()

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")

    if HEADLESS:
        print("[INFO] 启用无头模式")
        options.add_argument("--headless=new")

    kwargs = {"options": options}
    if CHROME_BIN and os.path.exists(CHROME_BIN):
        print(f"[INFO] 使用指定 Chrome: {CHROME_BIN}")
        kwargs["browser_executable_path"] = CHROME_BIN

    try:
        driver = uc.Chrome(**kwargs)
        driver.set_page_load_timeout(30)
        print("[INFO] Chrome 启动成功")
    except Exception as e:
        print(f"[ERROR] Chrome 启动失败: {e}")
        print("[提示] 青龙面板请安装 chromium，并通过 CHROME_BIN 指定路径")
        return None

    # 设置 Cookie
    print("[INFO] 设置登录 Cookie...")
    driver.get("https://www.nodeseek.com")
    random_sleep(2, 4)

    success_count = 0
    for item in COOKIE_STR.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        try:
            driver.add_cookie({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".nodeseek.com",
                "path": "/",
            })
            success_count += 1
        except Exception as e:
            print(f"[WARN] Cookie 设置失败 [{name}]: {e}")

    print(f"[INFO] 成功设置 {success_count} 个 Cookie")
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
            print(f"[INFO] 已点击签到按钮: {btn_text}")
            random_sleep(2, 4)
            return True
        except Exception:
            print("[INFO] 今日已签到或按钮不存在")
            return True
    except Exception as e:
        print(f"[ERROR] 签到异常: {e}")
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
            print("[INFO] 帖子超过7天，无法加鸡腿")
            ok_btn = driver.find_element(By.CSS_SELECTOR, ".msc-confirm .msc-ok")
            ok_btn.click()
            return False
        except Exception:
            pass

        ok_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".msc-confirm .msc-ok"))
        )
        ok_btn.click()
        print("[INFO] 加鸡腿成功")

        WebDriverWait(driver, 5).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".msc-overlay"))
        )
        random_sleep(1, 2)
        return True
    except Exception as e:
        print(f"[INFO] 加鸡腿跳过: {str(e)[:60]}")
        return False


# ========== AI 评论 ==========
def do_comment(driver):
    ai_enabled = DEEPSEEK_API_KEY != ""
    print(f"\n[STEP 2] 执行评论（目标 {COMMENT_COUNT} 帖，AI评论: {'开' if ai_enabled else '关'}）...")

    if not ai_enabled:
        print("[WARN] 未配置 DEEPSEEK_API_KEY，将使用随机语料评论")

    try:
        driver.get("https://www.nodeseek.com/categories/trade")
        random_sleep(3, 5)

        posts = WebDriverWait(driver, 15).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".post-list-item"))
        )
        print(f"[INFO] 获取到 {len(posts)} 个帖子")

        # 过滤置顶帖
        normal_posts = []
        for post in posts:
            try:
                if not post.find_elements(By.CSS_SELECTOR, ".pinned, .is-pinned"):
                    link = post.find_element(By.CSS_SELECTOR, ".post-title a")
                    normal_posts.append(link.get_attribute("href"))
            except Exception:
                continue

        if not normal_posts:
            print("[WARN] 未找到可评论的帖子")
            return

        target_count = min(COMMENT_COUNT, len(normal_posts))
        selected = random.sample(normal_posts, target_count)
        print(f"[INFO] 随机选择 {target_count} 个帖子")

        success = 0
        ai_success = 0
        for idx, url in enumerate(selected):
            print(f"\n--- [{idx+1}/{target_count}] {url} ---")
            try:
                driver.get(url)
                random_sleep(2, 4)

                # 模拟阅读
                human_scroll(driver, "down", random.randint(2, 4))
                random_sleep(1, 2)

                # 加鸡腿
                click_chicken_leg(driver)

                # 提取帖子内容
                post_title, post_content = extract_post_content(driver)
                print(f"[INFO] 帖子标题: {post_title[:50]}...")

                # 生成评论
                comment_text = None
                if ai_enabled:
                    print("[INFO] 正在调用 DeepSeek 生成评论...")
                    comment_text = generate_ai_comment(post_title, post_content)
                    if comment_text:
                        ai_success += 1
                        print(f"[INFO] AI 评论: {comment_text}")
                    elif AI_FALLBACK:
                        comment_text = random.choice(FALLBACK_COMMENTS)
                        print(f"[INFO] AI 失败，回退到随机评论: {comment_text}")
                    else:
                        print("[WARN] AI 生成失败且未启用降级，跳过此帖")
                        continue
                else:
                    comment_text = random.choice(FALLBACK_COMMENTS)
                    print(f"[INFO] 随机评论: {comment_text}")

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

                print(f"[OK] 评论发布成功")
                success += 1

                # 拉长间隔
                random_sleep(10, 20)

            except Exception as e:
                print(f"[FAIL] 评论失败: {str(e)[:80]}")
                random_sleep(3, 6)
                continue

        print(f"\n[INFO] 评论完成: 成功 {success}/{target_count}，AI生成成功 {ai_success}")

    except Exception as e:
        print(f"[ERROR] 评论任务异常: {e}")
        traceback.print_exc()


# ========== 主流程 ==========
def main():
    print("=" * 55)
    print("  NodeSeek 自动签到 + AI 评论（DeepSeek 版）")
    print(f"  随机签到: {'开' if NS_RANDOM else '关'} | 无头模式: {'开' if HEADLESS else '关'}")
    print(f"  AI 模型: {DEEPSEEK_MODEL if DEEPSEEK_API_KEY else '未启用'}")
    print(f"  评论数量: {COMMENT_COUNT}")
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
        print(f"[ERROR] 主流程异常: {e}")
        traceback.print_exc()
    finally:
        if driver:
            try:
                driver.quit()
                print("[INFO] 浏览器已关闭")
            except Exception:
                pass

    print("\n[DONE] 脚本执行完毕")


if __name__ == "__main__":
    main()
