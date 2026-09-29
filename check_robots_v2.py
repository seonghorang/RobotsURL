#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전라도·충청도 군청 robots.txt 점검 v2

v1 대비 개선점
1. User-agent 그룹을 제대로 해석 (고흥군처럼 봇별로 Allow/Disallow가 갈리는 경우 대응)
   - 우리 크롤러(BOT_NAME)에 해당하는 그룹만 적용, 없으면 '*' 그룹 적용
   - Allow/Disallow는 RFC 9309 방식(가장 긴 경로 우선, 동률이면 Allow, * 와 $ 지원)
2. 실제 수집할 '게시판 URL'이 허용되는지 경로 단위로 검사 (장수군의 Disallow: /board 같은 경우 포착)
3. 게시판 URL 자동 탐색(홈페이지 링크에서 공지사항/고시공고 찾기) + 수동 지정(board_urls.csv)
4. 오류 처리 개선: 재시도, 타임아웃 연장, SSL 인증서 오류 시 읽기 전용 재시도(표시함)
   - 404 등 4xx는 '없음=제한 없음', 5xx/접속실패는 '확인 실패'로 구분
5. 원문 robots.txt를 robots_raw/ 폴더에 저장해 눈으로 확인 가능

사용법
  pip install requests
  python check_robots_v2.py

수동 게시판 지정(선택): board_urls.csv (UTF-8) 를 같은 폴더에 두면 자동 탐색 대신 사용
  군명,게시판명,URL
  장수군,공지사항,https://www.jangsu.go.kr/board/list.jangsu?boardId=BBS_0000001
"""

import argparse
import csv
import html
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import urllib3

# ---------------------------------------------------------------- 설정
BOT_NAME = "GunNoticeBot"  # robots.txt에서 우리에게만 적용되는 그룹 이름
USER_AGENT = f"{BOT_NAME}/0.1 (personal research; robots.txt-compliant)"
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = (10, 25)  # (연결, 읽기) 초
RETRIES = 3
SLEEP_BETWEEN = 1.0  # 군청 사이 대기(초)
MAX_BOARDS_PER_GUN = 6

BOARD_KEYWORDS = ["공지사항", "고시공고", "고시/공고", "고시 공고", "공고", "고시", "공지"]

GUNCHEONG_LIST = [
    # 전북특별자치도
    ("완주군", "https://www.wanju.go.kr"),
    ("진안군", "https://www.jinan.go.kr"),
    ("무주군", "https://www.muju.go.kr"),
    ("장수군", "https://www.jangsu.go.kr"),
    ("임실군", "https://www.imsil.go.kr"),
    ("순창군", "https://www.sunchang.go.kr"),
    ("고창군", "https://www.gochang.go.kr"),
    ("부안군", "https://www.buan.go.kr"),
    # 전라남도
    ("담양군", "https://www.damyang.go.kr"),
    ("곡성군", "https://www.gokseong.go.kr"),
    ("구례군", "https://www.gurye.go.kr"),
    ("고흥군", "https://www.goheung.go.kr"),
    ("보성군", "https://www.boseong.go.kr"),
    ("화순군", "https://www.hwasun.go.kr"),
    ("장흥군", "https://www.jangheung.go.kr"),
    ("강진군", "https://www.gangjin.go.kr"),
    ("해남군", "https://www.haenam.go.kr"),
    ("영암군", "https://www.yeongam.go.kr"),
    ("무안군", "https://www.muan.go.kr"),
    ("함평군", "https://www.hampyeong.go.kr"),
    ("영광군", "https://www.yeonggwang.go.kr"),
    ("장성군", "https://www.jangseong.go.kr"),
    ("완도군", "https://www.wando.go.kr"),
    ("진도군", "https://www.jindo.go.kr"),
    ("신안군", "https://www.shinan.go.kr"),
    # 충청북도
    ("보은군", "https://www.boeun.go.kr"),
    ("옥천군", "https://www.oc.go.kr"),
    ("영동군", "https://www.yd21.go.kr"),
    ("증평군", "https://www.jp.go.kr"),
    ("진천군", "https://www.jincheon.go.kr"),
    ("괴산군", "https://www.goesan.go.kr"),
    ("음성군", "https://www.eumseong.go.kr"),
    ("단양군", "https://www.danyang.go.kr"),  # v1의 dy21.net은 도메인 조회 실패 → 확인 필요
    # 충청남도
    ("금산군", "https://www.geumsan.go.kr"),
    ("부여군", "https://www.buyeo.go.kr"),
    ("서천군", "https://www.seocheon.go.kr"),
    ("청양군", "https://www.cheongyang.go.kr"),
    ("홍성군", "https://www.hongseong.go.kr"),
    ("예산군", "https://www.yesan.go.kr"),
    ("태안군", "https://www.taean.go.kr"),
]


# ---------------------------------------------------------------- robots.txt 해석
def parse_robots(text):
    """robots.txt 텍스트를 그룹 목록으로 파싱한다.
    반환: [{'agents': [소문자 agent...], 'rules': [('allow'|'disallow', 경로)...], 'delay': float|None}]
    """
    groups, current, last_was_ua = [], None, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not last_was_ua or current is None:
                current = {"agents": [], "rules": [], "delay": None}
                groups.append(current)
            current["agents"].append(value.lower())
            last_was_ua = True
        elif key in ("allow", "disallow"):
            if current is not None:
                current["rules"].append((key, value))
            last_was_ua = False
        elif key == "crawl-delay":
            if current is not None:
                try:
                    current["delay"] = float(value)
                except ValueError:
                    pass
            last_was_ua = False
        # sitemap 등 기타 지시어는 그룹 구분에 영향 없음
    return groups


def select_rules(groups, bot_name):
    """우리 봇에 적용될 (규칙 목록, crawl-delay, 적용 그룹 설명)을 고른다."""
    bot = bot_name.lower()
    specific = [g for g in groups if any(a != "*" and a in bot for a in g["agents"])]
    if specific:
        label = "전용 그룹: " + ",".join(sorted({a for g in specific for a in g["agents"]}))
        chosen = specific
    else:
        chosen = [g for g in groups if "*" in g["agents"]]
        label = "'*' 그룹" if chosen else "해당 그룹 없음(제한 없음)"
    rules = [r for g in chosen for r in g["rules"]]
    delays = [g["delay"] for g in chosen if g["delay"] is not None]
    return rules, (delays[0] if delays else None), label


def _to_regex(pattern):
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = re.escape(pattern).replace(r"\*", ".*")
    return re.compile("^" + body + ("$" if anchored else ""))


def is_allowed(rules, path):
    """RFC 9309: 일치하는 규칙 중 가장 긴 것이 우선, 길이가 같으면 Allow 우선."""
    best_len, best_allow = -1, True  # 일치 규칙이 없으면 허용
    for kind, pattern in rules:
        if pattern == "":  # 빈 Disallow = 제한 없음, 빈 Allow는 무시
            continue
        if _to_regex(pattern).match(path):
            length = len(pattern)
            allow = kind == "allow"
            if length > best_len or (length == best_len and allow):
                best_len, best_allow = length, allow
    return best_allow


def path_of(url):
    p = urlparse(url)
    return (p.path or "/") + (("?" + p.query) if p.query else "")


# ---------------------------------------------------------------- 네트워크
def http_get(url, verify=True):
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=verify)


def fetch_robots(base_url):
    """robots.txt를 가져온다. 반환 dict: state = ok | none | fail"""
    robots_url = urljoin(base_url, "/robots.txt")
    out = {"url": robots_url, "state": "fail", "status": None, "text": "", "note": ""}
    verify = True
    for attempt in range(1, RETRIES + 1):
        try:
            resp = http_get(robots_url, verify=verify)
            out["status"] = resp.status_code
            if resp.status_code == 200:
                out["state"], out["text"] = "ok", resp.text
            elif 400 <= resp.status_code < 500:
                out["state"] = "none"  # 4xx: robots.txt 없음 → 제한 없음으로 취급
            else:
                out["note"] = f"HTTP {resp.status_code}"  # 5xx: 재시도 대상
                time.sleep(2 * attempt)
                continue
            return out
        except requests.exceptions.SSLError as e:
            if verify:
                verify = False  # 읽기 전용(robots.txt/공개 페이지)으로만 재시도
                out["note"] = "SSL 인증서 검증 실패 → 검증 없이 재시도"
                continue
            out["note"] = f"SSL 오류: {e}"[:200]
        except requests.exceptions.RequestException as e:
            out["note"] = f"{type(e).__name__}: {e}"[:200]
            time.sleep(2 * attempt)
    return out


def discover_boards(base_url, rules):
    """홈페이지 링크에서 공지/고시공고 게시판 후보를 찾는다(홈페이지가 허용된 경우에만)."""
    if not is_allowed(rules, "/"):
        return []
    try:
        resp = http_get(base_url + "/", verify=False)
        resp.encoding = resp.apparent_encoding or resp.encoding
        page = resp.text
    except requests.exceptions.RequestException:
        return []
    found, seen = [], set()
    for href, inner in re.findall(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.I | re.S):
        text = re.sub(r"<[^>]+>|\s+", " ", inner).strip()
        if not text or len(text) > 20:
            continue
        if any(k in text for k in BOARD_KEYWORDS) and not href.lower().startswith(("javascript", "#", "mailto")):
            url = urljoin(base_url + "/", html.unescape(href))
            if url not in seen and urlparse(url).netloc.endswith(urlparse(base_url).netloc.replace("www.", "")):
                seen.add(url)
                found.append((text, url))
    return found[:MAX_BOARDS_PER_GUN]


_ROBOTS_CACHE = {}


def robots_for(url):
    """URL의 호스트별 robots.txt를 가져와 캐시한다. (rb, rules, delay, label)
    rules가 None이면 확인 실패. 게시판이 eminwon.* 같은 다른 호스트에 있을 때도 그 호스트 기준으로 판정한다."""
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _ROBOTS_CACHE:
        rb = fetch_robots(root)
        if rb["state"] == "ok":
            rules, delay, label = select_rules(parse_robots(rb["text"]), BOT_NAME)
        elif rb["state"] == "none":
            rules, delay, label = [], None, "robots.txt 없음(제한 없음)"
        else:
            rules, delay, label = None, None, "확인 실패"
        _ROBOTS_CACHE[root] = (rb, rules, delay, label)
    return _ROBOTS_CACHE[root]


def load_manual_boards(path="board_urls.csv"):
    boards = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                boards.setdefault(row["군명"].strip(), []).append((row["게시판명"].strip(), row["URL"].strip()))
    return boards


# ---------------------------------------------------------------- 메인
def main():
    global TIMEOUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="쉼표로 구분한 군 이름만 재점검 (예: 구례군,보성군)")
    ap.add_argument("--timeout", type=int, help="읽기 타임아웃(초), 기본 25")
    args = ap.parse_args()
    if args.timeout:
        TIMEOUT = (max(10, args.timeout // 2), args.timeout)
    targets = GUNCHEONG_LIST
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        targets = [t for t in GUNCHEONG_LIST if t[0] in wanted]
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("robots_raw", exist_ok=True)
    manual = load_manual_boards()
    rows = []
    total = len(targets)
    print(f"군청 robots.txt 점검 v2 시작 ({stamp}) — 총 {total}곳, 봇 이름: {BOT_NAME}")

    for i, (name, base) in enumerate(targets, 1):
        print(f"[{i:02d}/{total}] {name} {base}")
        rb, rules, delay, label = robots_for(base)
        base_row = {"군명": name, "홈페이지": base, "robots.txt 상태코드": rb["status"] or "", "비고": rb["note"]}

        if rb["state"] == "fail":
            rows.append({**base_row, "robots 판정": "❓ 확인 실패(재시도 필요)", "적용 그룹": "", "Crawl-delay": "",
                         "게시판명": "", "게시판URL": "", "게시판 허용": ""})
            print("   → 확인 실패:", rb["note"])
            time.sleep(SLEEP_BETWEEN)
            continue

        if rb["state"] == "ok":
            with open(f"robots_raw/{name}.txt", "w", encoding="utf-8") as f:
                f.write(rb["text"])

        root_ok = is_allowed(rules, "/")
        judgment = "✅ 사이트 접근 가능" if root_ok else "🚫 우리 봇에 전체 차단"
        boards = manual.get(name) or discover_boards(base, rules)
        source = "수동" if name in manual else "자동탐색"

        if not boards:
            rows.append({**base_row, "robots 판정": judgment, "적용 그룹": label, "Crawl-delay": delay or "",
                         "게시판명": "", "게시판URL": "", "게시판 허용": "(게시판 URL 미확인)"})
            print(f"   → {judgment} / 게시판 URL 미확인")
        for title, url in boards:
            same_host = urlparse(url).netloc == urlparse(base).netloc
            if same_host:
                b_rules, note = rules, base_row["비고"]
            else:  # 게시판이 다른 호스트(예: eminwon.*)에 있으면 그 호스트의 robots.txt로 판정
                _, b_rules, _, _ = robots_for(url)
                note = f"다른 호스트: {urlparse(url).netloc}"
            if b_rules is None:
                verdict = "확인 실패(해당 호스트 robots)"
            else:
                verdict = "허용" if is_allowed(b_rules, path_of(url)) else "차단"
            rows.append({**base_row, "비고": note, "robots 판정": judgment, "적용 그룹": label, "Crawl-delay": delay or "",
                         "게시판명": f"{title} ({source})", "게시판URL": url, "게시판 허용": verdict})
            print(f"   → [{title}] {verdict}  {url}")
        time.sleep(SLEEP_BETWEEN)

    fname = f"guncheong_robots_v2_{stamp}.csv"
    with open(fname, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    guns = {r["군명"] for r in rows}
    ok_guns = {r["군명"] for r in rows if r["게시판 허용"] == "허용"}
    blocked_guns = {r["군명"] for r in rows if r["robots 판정"].startswith("🚫")}
    fail_guns = {r["군명"] for r in rows if r["robots 판정"].startswith("❓")}
    print(f"\n완료: {fname}")
    print(f"  전체 {len(guns)}곳 / 게시판 수집 가능 {len(ok_guns)}곳 / 전체 차단 {len(blocked_guns)}곳 / 확인 실패 {len(fail_guns)}곳")
    print("  '게시판 URL 미확인'인 군은 board_urls.csv에 직접 넣고 다시 실행하세요.")


if __name__ == "__main__":
    main()