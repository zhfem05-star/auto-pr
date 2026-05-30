"""
AI Test Analysis Agent
pytest 실패 시 원인을 분석하고 PR에 코멘트로 남깁니다.
"""

import os
import sys
import subprocess
import requests
from anthropic import Anthropic

# ── 환경 변수 ──────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REPO = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = os.environ.get("PR_NUMBER", "")
TEST_OUTPUT_FILE = os.environ.get("TEST_OUTPUT_FILE", "test_output.txt")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_LOG_CHARS = 20_000


# ── System Prompt ──────────────────────────────────────────────
TEST_ANALYST_SYSTEM_PROMPT = """
당신은 QA 엔지니어이자 Python 테스트 전문가입니다.
pytest 테스트 실패 로그를 분석하여 개발자가 빠르게 원인을 파악하고
수정할 수 있도록 도와주는 것이 당신의 역할입니다.

## 분석 방침
- 테스트 실패의 **근본 원인(root cause)** 을 정확히 짚어줍니다.
- 에러 메시지만 반복하는 것이 아니라, 왜 이 에러가 발생했는지 설명합니다.
- 수정 방향을 구체적으로 제시합니다 (코드 예시 포함 가능).
- 여러 테스트가 실패했다면, 연관성이 있는 것끼리 묶어서 설명합니다.

## 출력 형식

### 🧪 테스트 결과 요약
| 항목 | 값 |
|------|-----|
| 전체 테스트 | N개 |
| 성공 | N개 |
| 실패 | N개 |
| 에러 | N개 |

### 🔴 실패 분석

**[테스트명]**
- **실패 원인**: (왜 실패했는가)
- **관련 코드**: (어느 코드/로직의 문제인가)
- **수정 방향**: (어떻게 고치면 되는가)

(실패한 각 테스트마다 반복)

### 🔗 공통 원인 패턴
(여러 테스트가 같은 이유로 실패했다면 묶어서 설명)

### 🛠️ 권장 수정 순서
1. 가장 먼저 고쳐야 할 것
2. 그 다음...
"""


# ── Run tests ──────────────────────────────────────────────────
def run_pytest() -> tuple[int, str]:
    """
    pytest를 실행하고 (return_code, output) 를 반환합니다.
    이미 test_output.txt가 있으면 그것을 읽습니다 (CI에서 미리 실행한 경우).
    """
    if os.path.exists(TEST_OUTPUT_FILE):
        print(f"📄 기존 테스트 결과 파일 사용: {TEST_OUTPUT_FILE}")
        with open(TEST_OUTPUT_FILE, "r", encoding="utf-8") as f:
            output = f.read()
        # 실패 여부는 출력에서 판단
        failed = "failed" in output.lower() or "error" in output.lower()
        return (1 if failed else 0), output

    print("🧪 pytest 실행 중...")
    result = subprocess.run(
        ["python", "-m", "pytest", "-v", "--tb=short", "--no-header"],
        capture_output=True,
        text=True,
        timeout=300,  # 5분 타임아웃
    )
    output = result.stdout + result.stderr
    return result.returncode, output


# ── GitHub API helpers ─────────────────────────────────────────
def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def post_pr_comment(body: str) -> None:
    if not PR_NUMBER:
        print("PR_NUMBER가 없습니다. 결과를 stdout에만 출력합니다.")
        print(body)
        return

    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    res = requests.post(url, headers=gh_headers(), json={"body": body})
    res.raise_for_status()
    print("✅ PR 코멘트 등록 완료")


def update_commit_status(state: str, description: str, sha: str) -> None:
    """커밋 상태를 업데이트합니다. state: success | failure | error | pending"""
    url = f"https://api.github.com/repos/{REPO}/statuses/{sha}"
    payload = {
        "state": state,
        "description": description,
        "context": "ai-test-agent",
    }
    try:
        res = requests.post(url, headers=gh_headers(), json=payload)
        res.raise_for_status()
    except Exception as e:
        print(f"⚠️ 커밋 상태 업데이트 실패 (무시): {e}")


# ── Analysis logic ─────────────────────────────────────────────
def analyze_failures(test_output: str) -> str:
    if len(test_output) > MAX_LOG_CHARS:
        test_output = test_output[:MAX_LOG_CHARS] + f"\n\n> ⚠️ 로그가 너무 길어 앞 {MAX_LOG_CHARS:,}자만 분석합니다."

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=TEST_ANALYST_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"다음 pytest 실행 결과를 분석해주세요:\n\n```\n{test_output}\n```",
            }
        ],
    )
    return response.content[0].text


def format_success_message(test_output: str) -> str:
    """테스트 전부 통과 시 간단한 성공 메시지를 생성합니다."""
    # 마지막 줄에서 통과 개수 추출 시도
    lines = test_output.strip().splitlines()
    summary_line = next((l for l in reversed(lines) if "passed" in l), "모든 테스트 통과")
    return f"### ✅ 테스트 전체 통과\n\n`{summary_line}`\n\n🎉 잘 하셨습니다!"


# ── Entry point ────────────────────────────────────────────────
def main():
    print(f"🤖 AI Test Analysis Agent 시작 — {REPO}")

    return_code, test_output = run_pytest()

    if return_code == 0:
        print("✅ 모든 테스트 통과!")
        message = format_success_message(test_output)
        sha = os.environ.get("GITHUB_SHA", "")
        if sha:
            update_commit_status("success", "All tests passed", sha)
    else:
        print(f"❌ 테스트 실패 감지. Claude에게 분석 요청 중...")
        analysis = analyze_failures(test_output)
        raw_log_section = f"\n\n<details>\n<summary>📋 전체 테스트 로그 보기</summary>\n\n```\n{test_output[:5000]}\n```\n</details>"
        message = (
            analysis
            + raw_log_section
            + "\n\n---\n> 🤖 *이 분석은 AI Test Analysis Agent가 자동으로 작성했습니다.*"
        )
        sha = os.environ.get("GITHUB_SHA", "")
        if sha:
            update_commit_status("failure", "Tests failed — see PR comment for analysis", sha)

    post_pr_comment(message)
    sys.exit(return_code)


if __name__ == "__main__":
    main()
