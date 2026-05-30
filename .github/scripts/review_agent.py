"""
AI Code Review Agent
PR이 열리거나 업데이트될 때 자동으로 코드를 리뷰하고 GitHub에 코멘트를 남깁니다.
"""

import os
import sys
import json
import requests
from anthropic import Anthropic

# ── 환경 변수 ──────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REPO = os.environ["GITHUB_REPOSITORY"]          # e.g. "username/repo"
PR_NUMBER = os.environ["PR_NUMBER"]              # e.g. "42"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_DIFF_CHARS = 30_000  # 너무 큰 diff는 앞부분만 리뷰


# ── System Prompt ──────────────────────────────────────────────
REVIEWER_SYSTEM_PROMPT = """
당신은 5년 이상 경력의 시니어 백엔드 엔지니어입니다.
판교 핀테크 스타트업에서 주로 Python/FastAPI 기반 서비스를 개발해왔으며,
현재 이 팀의 테크리드를 맡고 있습니다.

## 리뷰 철학
- 코드를 까는 게 목적이 아니라, 팀 전체의 코드 퀄리티를 높이는 것이 목적입니다.
- 좋은 부분은 칭찬하고, 개선이 필요한 부분은 이유와 함께 명확히 짚어줍니다.
- "이렇게 바꿔라" 보다 "이 방향이 더 낫지 않을까요?"처럼 제안 형식으로 말합니다.

## 반드시 확인하는 항목
1. **버그 및 로직 오류** — 엣지케이스, 경계값, None/null 처리 누락
2. **보안** — SQL Injection, 인증 누락, 민감 정보 노출, 입력값 검증
3. **성능** — N+1 쿼리, 불필요한 반복, 메모리 낭비
4. **가독성** — 변수명, 함수명, 주석 필요 여부
5. **에러 핸들링** — 예외 처리 누락, 너무 광범위한 except, 적절한 로깅
6. **테스트 가능성** — 의존성 주입, 테스트하기 어려운 구조
7. **설계** — 단일 책임 원칙, 과도한 결합도

## 출력 형식
다음 구조로 리뷰를 작성하세요:

### 🔍 전체 요약
(변경 사항 전체를 한 문단으로 요약하고, 전반적인 코드 품질 평가)

### ✅ 잘 된 부분
(구체적으로 어떤 점이 좋았는지)

### 🚨 Critical (반드시 수정)
(머지 전 반드시 고쳐야 할 것들 — 버그, 보안 이슈 등)

### ⚠️ Major (강력 권장)
(수정하면 확실히 나아지는 것들)

### 💡 Minor (제안)
(선택적으로 고려할 개선사항, 스타일, 리팩토링 아이디어)

### 📝 기타
(참고 사항, 질문, 다음에 논의할 것들)

---
각 항목에는 **파일명과 관련 코드 스니펫**을 반드시 언급해주세요.
항목이 없으면 "없음" 으로 표시하세요.
"""


# ── GitHub API helpers ─────────────────────────────────────────
def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_info() -> dict:
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
    res = requests.get(url, headers=gh_headers())
    res.raise_for_status()
    return res.json()


def get_pr_diff() -> str:
    """PR의 파일별 diff를 문자열로 반환합니다."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files"
    res = requests.get(url, headers=gh_headers())
    res.raise_for_status()

    files = res.json()
    parts = []
    for f in files:
        header = f"### 📄 {f['filename']}  ({f['status']}, +{f['additions']} -{f['deletions']})"
        patch = f.get("patch", "(binary file or too large to show)")
        parts.append(f"{header}\n```diff\n{patch}\n```")

    return "\n\n".join(parts)


def post_pr_review(body: str, event: str = "COMMENT") -> None:
    """
    GitHub PR Review를 등록합니다.
    event: "COMMENT" | "APPROVE" | "REQUEST_CHANGES"
    """
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/reviews"
    payload = {"body": body, "event": event}
    res = requests.post(url, headers=gh_headers(), json=payload)
    res.raise_for_status()
    print(f"✅ Review posted (event={event})")


# ── Review logic ───────────────────────────────────────────────
def decide_review_event(review_text: str) -> str:
    """리뷰 내용에 Critical 이슈가 있으면 REQUEST_CHANGES, 없으면 COMMENT."""
    critical_section = review_text.split("### 🚨 Critical")[1] if "### 🚨 Critical" in review_text else ""
    if critical_section and "없음" not in critical_section[:100]:
        return "REQUEST_CHANGES"
    return "COMMENT"


def run_review(diff: str, pr_info: dict) -> str:
    title = pr_info.get("title", "")
    body = pr_info.get("body") or "(PR 설명 없음)"
    author = pr_info.get("user", {}).get("login", "unknown")
    base = pr_info.get("base", {}).get("ref", "main")
    head = pr_info.get("head", {}).get("ref", "feature")

    # diff가 너무 크면 앞부분만 전송
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n\n> ⚠️ diff가 너무 커서 앞 {MAX_DIFF_CHARS:,}자만 리뷰합니다."

    user_message = f"""
## PR 정보
- **제목**: {title}
- **작성자**: {author}
- **브랜치**: `{head}` → `{base}`
- **설명**:
{body}

---

## 변경된 코드
{diff}
"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=REVIEWER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text


# ── Entry point ────────────────────────────────────────────────
def main():
    print(f"🤖 AI Code Review 시작 — {REPO} PR #{PR_NUMBER}")

    pr_info = get_pr_info()
    diff = get_pr_diff()

    if not diff.strip():
        print("변경된 파일이 없습니다. 리뷰를 건너뜁니다.")
        sys.exit(0)

    print(f"📝 diff 크기: {len(diff):,}자 — Claude에게 리뷰 요청 중...")
    review_text = run_review(diff, pr_info)

    event = decide_review_event(review_text)
    footer = "\n\n---\n> 🤖 *이 리뷰는 AI Code Review Agent가 자동으로 작성했습니다.*"
    post_pr_review(review_text + footer, event=event)

    print("✅ 완료!")


if __name__ == "__main__":
    main()
