import json
import os
import random
import re
import string
import sys
import time
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

import requests


GPTMAIL_API_KEY = "key"
GPTMAIL_BASE_URL = "https://mail.chatgpt.org.uk"
ANYTHING_BASE_URL = "https://www.anything.com"
GRAPHQL_URL = f"{ANYTHING_BASE_URL}/api/graphql"
REFERRAL_CODE = "code"
SIGNUP_URL = f"{ANYTHING_BASE_URL}/signup?rid={REFERRAL_CODE}"
LANGUAGE = "zh-CN"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
MAX_CONSECUTIVE_FAILURES = 3
OUTPUT_EMAILS_FILE = os.path.join(os.path.dirname(__file__), "registered_emails.txt")
OUTPUT_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "registered_results.jsonl")


def configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")

SIGNUP_MUTATION = """
mutation SignUpWithAppPrompt($input: SignUpWithAppPromptInput!) {
  signUpAndStartAgent(input: $input) {
    ... on SignUpWithoutAppPromptPayload {
      success
      accessToken
      project {
        id
        projectGroup {
          id
          __typename
        }
        __typename
      }
      projectGroup {
        id
        __typename
      }
      user {
        ...UserFragment
        __typename
      }
      organization {
        id
        __typename
      }
      __typename
    }
    ... on SignUpAndStartAgentErrorResult {
      success
      errors {
        kind
        message
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment UserFragment on User {
  id
  email
  roles
  badges
  displayName
  username
  profile {
    firstName
    lastName
    photoURL
    xUsername
    instagramUsername
    facebookUsername
    githubUsername
    linkedinUsername
    tiktokUsername
    __typename
  }
  __typename
}
""".strip()


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: List[Dict[str, str]] = []
        self._current_href: Optional[str] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = ""
        for key, value in attrs:
            if key.lower() == "href":
                href = value or ""
                break
        self._current_href = href
        self._current_text = []

    def handle_data(self, data):
        if self._current_href is not None and data:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self._current_href is None:
            return
        text = " ".join(part.strip() for part in self._current_text if part.strip()).strip()
        self.links.append({"href": self._current_href, "text": text})
        self._current_href = None
        self._current_text = []


def generate_email_prefix() -> str:
    letters = "".join(random.choice(string.ascii_lowercase) for _ in range(random.randint(4, 6)))
    mixed = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(random.randint(2, 5)))
    return letters + mixed


def append_registered_result(email: str, user_id: str, project_group_id: str, final_url: str, title: str) -> None:
    with open(OUTPUT_EMAILS_FILE, "a", encoding="utf-8") as email_file:
        email_file.write(f"{email}\n")

    with open(OUTPUT_RESULTS_FILE, "a", encoding="utf-8") as result_file:
        result_file.write(
            json.dumps(
                {
                    "email": email,
                    "user_id": user_id,
                    "project_group_id": project_group_id,
                    "final_url": final_url,
                    "title": title,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def create_gptmail_headers() -> Dict[str, str]:
    return {
        "X-API-Key": GPTMAIL_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def create_temp_email(prefix: Optional[str] = None) -> Tuple[str, str]:
    url = f"{GPTMAIL_BASE_URL}/api/generate-email"
    payload = {"prefix": prefix} if prefix else None
    response = requests.post(url, headers=create_gptmail_headers(), json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"GPTMail 创建邮箱失败: {data.get('error') or 'unknown error'}")
    email = (data.get("data") or {}).get("email")
    if not email:
        raise RuntimeError("GPTMail 未返回邮箱地址")
    return email, email


def list_emails(email: str) -> List[Dict]:
    response = requests.get(
        f"{GPTMAIL_BASE_URL}/api/emails",
        params={"email": email},
        headers=create_gptmail_headers(),
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"GPTMail 获取邮件失败: {data.get('error') or 'unknown error'}")
    return ((data.get("data") or {}).get("emails") or [])


def extract_magic_login_link(email_html: str) -> Optional[str]:
    parser = LinkParser()
    parser.feed(email_html or "")

    preferred_texts = ["sign in", "magic login", "log in", "login"]
    for link in parser.links:
        href = (link.get("href") or "").strip()
        text = (link.get("text") or "").strip().lower()
        if not href:
            continue
        if any(token in text for token in preferred_texts):
            return href

    for link in parser.links:
        href = (link.get("href") or "").strip()
        if href and "anything.com/ls/click" in href:
            return href

    regex_match = re.search(r"https?://[^\s\"'<>]+", email_html or "")
    if regex_match:
        return regex_match.group(0)
    return None


def poll_magic_login_email(email: str, max_attempts: int = 24, interval_seconds: int = 5) -> Tuple[Dict, str]:
    for attempt in range(1, max_attempts + 1):
        emails = list_emails(email)
        subjects = [item.get("subject") for item in emails[:5]]
        print(f"[*] 拉取邮件 {attempt}/{max_attempts}，最近主题: {subjects}")

        for item in emails:
            subject = (item.get("subject") or "").strip().lower()
            if subject != "magic login link":
                continue
            content = item.get("html_content") or item.get("content") or ""
            link = extract_magic_login_link(content)
            if not link:
                raise RuntimeError("已收到 Magic Login Link 邮件，但未能提取链接")
            return item, link

        time.sleep(interval_seconds)

    raise TimeoutError("轮询超时，未收到 Magic Login Link 邮件")


def build_signup_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "accept": "application/graphql-response+json,application/json;q=0.9",
            "accept-language": f"{LANGUAGE},zh;q=0.9",
            "apollographql-client-name": "flux-web",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": ANYTHING_BASE_URL,
            "pragma": "no-cache",
            "referer": SIGNUP_URL,
            "user-agent": USER_AGENT,
        }
    )
    return session


def build_magic_link_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": f"{LANGUAGE},zh;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": SIGNUP_URL,
            "upgrade-insecure-requests": "1",
            "user-agent": USER_AGENT,
        }
    )
    return session


def extract_html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def signup_anything(email: str) -> Dict:
    session = build_signup_session()
    warmup = session.get(SIGNUP_URL, timeout=30)
    warmup.raise_for_status()

    payload = {
        "operationName": "SignUpWithAppPrompt",
        "variables": {
            "input": {
                "email": email,
                "postLoginRedirect": None,
                "language": LANGUAGE,
                "referralCode": REFERRAL_CODE,
            }
        },
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}},
        "query": SIGNUP_MUTATION,
    }

    response = session.post(GRAPHQL_URL, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()

    result = ((data.get("data") or {}).get("signUpAndStartAgent") or {})
    if not result:
        raise RuntimeError(f"GraphQL 响应异常: {json.dumps(data, ensure_ascii=False)}")

    if result.get("__typename") == "SignUpAndStartAgentErrorResult":
        errors = result.get("errors") or []
        raise RuntimeError(f"Anything 注册失败: {json.dumps(errors, ensure_ascii=False)}")

    if not result.get("success"):
        raise RuntimeError(f"Anything 注册未成功: {json.dumps(result, ensure_ascii=False)}")

    return result


def open_magic_link_direct(magic_link: str) -> Dict[str, str]:
    session = build_magic_link_session()
    response = session.get(magic_link, allow_redirects=True, timeout=60)
    response.raise_for_status()

    final_url = response.url
    content = response.text or ""
    title = extract_html_title(content)
    redirect_chain = [item.headers.get("Location") or item.url for item in response.history]

    if "anything.com" not in final_url or "/ls/click" in final_url:
        raise RuntimeError(
            f"Magic Link 直连后未跳转到目标站点，当前 URL: {final_url}；"
            f"重定向链路: {json.dumps(redirect_chain, ensure_ascii=False)}"
        )

    if not title and "anything" not in content.lower():
        raise RuntimeError("Magic Link 已直连，但页面未返回可识别的 Anything 内容")

    return {
        "final_url": final_url,
        "title": title,
    }


def register_one(index: int) -> Dict[str, str]:
    print("=" * 72)
    print(f"[*] 开始第 {index} 次注册")

    prefix = generate_email_prefix()
    print(f"[*] 生成邮箱前缀: {prefix}")
    _, email = create_temp_email(prefix)
    print(f"[+] 临时邮箱创建成功: {email}")

    signup_result = signup_anything(email)
    user_id = ((signup_result.get("user") or {}).get("id") or "")
    project_group_id = ((signup_result.get("projectGroup") or {}).get("id") or "")
    print(f"[+] Anything 协议注册成功: email={email}, user_id={user_id}, project_group_id={project_group_id}")

    mail_item, magic_link = poll_magic_login_email(email)
    print(f"[+] 收到 Magic Login Link 邮件: id={mail_item.get('id')}, subject={mail_item.get('subject')}")
    print(f"[+] 提取注册链接成功: {magic_link}")

    protocol_result = open_magic_link_direct(magic_link)
    print(f"[+] HTTP 直连打开成功: {protocol_result['final_url']}")
    print(f"[+] 页面标题: {protocol_result['title']}")

    result = {
        "email": email,
        "user_id": user_id,
        "project_group_id": project_group_id,
        "magic_link_subject": mail_item.get("subject"),
        "final_url": protocol_result["final_url"],
        "title": protocol_result["title"],
    }
    append_registered_result(
        email=email,
        user_id=user_id,
        project_group_id=project_group_id,
        final_url=protocol_result["final_url"],
        title=protocol_result["title"],
    )

    print("=" * 72)
    print("[SUCCESS] Anything 注册与 Magic Link HTTP 直连流程完成")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[+] 已追加邮箱记录到: {OUTPUT_EMAILS_FILE}")
    return result


def main():
    configure_console_encoding()
    print("=" * 72)
    print("[*] Anything 单文件协议注册机启动")
    print("[*] 邮箱渠道: GPTMail（渠道 2）")
    print(f"[*] GPTMail 密钥: {GPTMAIL_API_KEY}")
    print(f"[*] 成功邮箱记录文件: {OUTPUT_EMAILS_FILE}")
    print(f"[*] 详细结果记录文件: {OUTPUT_RESULTS_FILE}")
    print(f"[*] 连续失败 {MAX_CONSECUTIVE_FAILURES} 次后自动停止")
    print("=" * 72)

    consecutive_failures = 0
    total_successes = 0
    total_attempts = 0

    while True:
        total_attempts += 1
        try:
            register_one(total_attempts)
            total_successes += 1
            consecutive_failures = 0
            print(f"[*] 当前统计: 尝试={total_attempts}, 成功={total_successes}, 连续失败={consecutive_failures}")
        except KeyboardInterrupt:
            print("[!] 收到中断信号，停止注册")
            raise
        except Exception as exc:
            consecutive_failures += 1
            print(f"[-] 第 {total_attempts} 次注册失败: {exc}")
            print(f"[*] 当前统计: 尝试={total_attempts}, 成功={total_successes}, 连续失败={consecutive_failures}")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[-] 连续失败已达到 {MAX_CONSECUTIVE_FAILURES} 次，脚本停止")
                break
            print("[*] 将继续下一次注册")


if __name__ == "__main__":
    main()
