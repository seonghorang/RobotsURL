#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
군청 임시거주시설 공고 모음 (v0.1)

실행:  python crawl.py      → site/index.html 생성 (더블클릭으로 열기), data.json 저장
필요:  pip install requests   /  check_robots_v2.py, boards.json 을 같은 폴더에 둘 것

동작: boards.json의 각 게시판을 robots.txt(호스트별)로 다시 확인한 뒤, 허용된 곳만
      1페이지를 읽어 최근 7일 글을 뽑고, 제목 키워드로 임시거주시설 관련 여부를 표시한다.
한계: 목록이 자바스크립트로 그려지는 게시판은 0건으로 나온다(화면 하단 '수집 상태'에 표시).
"""
import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests

import check_robots_v2 as rb

DAYS = 7
KEYWORDS = ["귀농", "귀촌", "임시거주", "만원주택", "만원하우스", "행복주택", "세컨", "새들하우스", "모듈",
            "빈집", "체류형", "살아보기", "입주자", "입주 모집", "주거"]
DATE_RE = re.compile(r"(20\d{2})[-./]\s?(\d{1,2})[-./]\s?(\d{1,2})")


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def fetch(url):
    try:
        resp = rb.http_get(url, verify=True)
    except requests.exceptions.SSLError:
        resp = rb.http_get(url, verify=False)
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def parse_list(page, base):
    """<tr>(없으면 <li>) 중 날짜가 있는 행에서 (제목, 링크, 날짜)를 뽑는다."""
    items = []
    for tag in ("tr", "li"):
        for block in re.findall(rf"<{tag}\b.*?</{tag}>", page, re.S | re.I):
            m = DATE_RE.search(clean(block))
            anchors = re.findall(r'<a\s[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', block, re.S | re.I)
            if not m or not anchors:
                continue
            href, inner = max(anchors, key=lambda a: len(clean(a[1])))
            title = clean(inner)
            if len(title) < 4:
                continue
            href = html.unescape(href).strip()
            link = base if (not href or href.lower().startswith(("javascript", "#"))) else urljoin(base, href)
            try:
                date = datetime(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                continue
            items.append({"title": title, "url": link, "date": date})
        if items:
            break
    return items


def main():
    with open("boards.json", encoding="utf-8") as f:
        boards = json.load(f)
    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=DAYS)
    notices, status, seen, last_hit = [], [], set(), {}

    for b in boards:
        url, st = b["url"], {"county": b["county"], "board": b["board"], "url": b["url"], "msg": "", "n": 0}
        print(f"{b['county']} {b['board']}")
        _, rules, delay, _ = rb.robots_for(url)
        if rules is None:
            st["msg"] = "robots.txt 확인 실패 - 건너뜀"
        elif not rb.is_allowed(rules, rb.path_of(url)):
            st["msg"] = "robots.txt 차단 - 건너뜀"
        else:
            host = urlparse(url).netloc
            wait = max(1.5, delay or 0) - (time.time() - last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            try:
                items = parse_list(fetch(url), url)
                recent = [i for i in items if i["date"] >= cutoff]
                st["n"] = len(recent)
                st["msg"] = ("목록을 읽지 못함(자바스크립트 화면이거나 구조가 다름)" if not items
                             else f"정상 (목록 {len(items)}건 중 최근 {DAYS}일 {len(recent)}건)")
                for i in recent:
                    key = (i["url"], i["title"])
                    if key in seen:
                        continue
                    seen.add(key)
                    notices.append({"county": b["county"], "board": b["board"], "title": i["title"], "url": i["url"],
                                    "date": i["date"].strftime("%Y-%m-%d"),
                                    "match": any(k in i["title"] for k in KEYWORDS)})
            except Exception as e:  # 네트워크/파싱 오류는 해당 게시판만 건너뜀
                st["msg"] = f"오류: {type(e).__name__}"
            last_hit[host] = time.time()
        status.append(st)

    prov = ["전북"] * 8 + ["전남"] * 17 + ["충북"] * 8 + ["충남"] * 7
    by_gun = {}
    for b in boards:
        by_gun.setdefault(b["county"], []).append({"board": b["board"], "url": b["url"]})
    links = [{"county": n, "province": prov[i] if i < len(prov) else "기타", "home": u, "boards": by_gun.get(n, [])}
             for i, (n, u) in enumerate(rb.GUNCHEONG_LIST)]
    known = {n for n, _ in rb.GUNCHEONG_LIST}
    for n, bs in by_gun.items():  # 목록에 없는 군을 boards.json에 추가한 경우
        if n not in known:
            p = urlparse(bs[0]["url"])
            links.append({"county": n, "province": "기타", "home": f"{p.scheme}://{p.netloc}", "boards": bs})

    notices.sort(key=lambda n: n["date"], reverse=True)
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "days": DAYS, "notices": notices, "status": status, "links": links}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    print(f"\n완료: 최근 {DAYS}일 {len(notices)}건 (관련 {sum(n['match'] for n in notices)}건) → site/index.html")


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>귀농 임시거주 공고</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.css">
<style>
:root{--bg:#000;--sf:#0f0f0f;--line:#232323;--tx:#ededed;--mu:#8c8c8c;--ac:#cdb27a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.6 "Pretendard Variable",Pretendard,"Malgun Gothic",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:720px;margin:0 auto;padding:56px 20px 96px}
h1{font-size:28px;font-weight:700;letter-spacing:-.02em;margin:0 0 6px}
.sub{color:var(--mu);font-size:14px;margin:0 0 28px}
.tools{display:flex;flex-direction:column;gap:14px;margin-bottom:8px}
input[type=search]{width:100%;background:var(--sf);border:1px solid var(--line);border-radius:10px;color:var(--tx);font:inherit;font-size:15px;padding:11px 14px}
input[type=search]::placeholder{color:var(--mu)}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:none;border:1px solid var(--line);color:var(--mu);border-radius:999px;font:inherit;font-size:13.5px;padding:5px 13px;cursor:pointer}
.chip[aria-pressed=true]{border-color:var(--ac);color:var(--ac)}
.chip b{font-weight:500;margin-left:5px;opacity:.7}
.sw{display:flex;align-items:center;gap:8px;color:var(--mu);font-size:14px;cursor:pointer;width:max-content}
.sw input{accent-color:var(--ac);width:16px;height:16px}
button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
h2{font-size:13px;font-weight:600;color:var(--mu);margin:36px 0 4px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.row{display:block;padding:15px 0;border-bottom:1px solid var(--line);color:inherit;text-decoration:none}
.row:hover .t{color:var(--ac)}
.t{font-size:16.5px;font-weight:600;line-height:1.45;transition:color .15s}
.m{display:flex;gap:10px;align-items:baseline;margin-top:5px;font-size:13.5px;color:var(--mu)}
.m em{font-style:normal;color:var(--tx);opacity:.85}
.hit{width:6px;height:6px;border-radius:50%;background:var(--ac);flex:none;transform:translateY(-2px)}
.none{color:var(--mu);padding:56px 0;text-align:center;font-size:15px}
details{margin-top:56px;color:var(--mu);font-size:13px}summary{cursor:pointer}
table{margin-top:12px;border-collapse:collapse}td{padding:3px 16px 3px 0;vertical-align:top}
.bad{color:#c98a8a}
.tabs{display:flex;gap:26px;border-bottom:1px solid var(--line);margin-bottom:22px}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--mu);font:inherit;font-size:15px;font-weight:600;padding:0 0 10px;margin-bottom:-1px;cursor:pointer}
.tabs button[aria-pressed=true]{color:var(--tx);border-bottom-color:var(--ac)}
.lk{display:flex;gap:16px;align-items:flex-start;padding:13px 0;border-bottom:1px solid var(--line)}
.cn{width:64px;flex:none;font-weight:600;padding-top:5px}
.bs{display:flex;flex-wrap:wrap;gap:8px}
.btn{border:1px solid var(--line);border-radius:8px;color:var(--tx);text-decoration:none;font-size:14px;padding:6px 13px}
.btn:hover{border-color:var(--ac);color:var(--ac)}
.btn.home{background:var(--sf)}
@media(max-width:480px){main{padding-top:36px}h1{font-size:24px}}
</style></head><body><main>
<h1>귀농 임시거주 공고</h1>
<p class="sub" id="sub"></p>
<nav class="tabs"><button id="t1" aria-pressed="true">새 공고</button><button id="t2" aria-pressed="false">군청 바로가기</button></nav>
<section id="v1">
<div class="tools">
<input type="search" id="q" placeholder="제목 검색" aria-label="제목 검색">
<div class="chips" id="chips"></div>
<label class="sw"><input type="checkbox" id="only" checked>임시거주시설 관련만 보기</label>
</div>
<div id="list"></div>
</section>
<section id="v2" hidden>
<div class="tools"><input type="search" id="q2" placeholder="군 이름 검색" aria-label="군 이름 검색"></div>
<div id="links"></div>
</section>
<details><summary>수집 상태</summary><table id="st"></table></details>
</main>
<script>
const D=__DATA__,$=id=>document.getElementById(id);
const esc=s=>s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const today=new Date(D.generated.slice(0,10));let gun="";
const ago=d=>Math.round((today-new Date(d))/864e5);
const label=d=>{const n=ago(d),x=new Date(d);return n<=0?"오늘":n==1?"어제":`${n}일 전 · ${x.getMonth()+1}월 ${x.getDate()}일`};
function render(){
  const only=$("only").checked,q=$("q").value.trim();
  const base=D.notices.filter(n=>(!only||n.match)&&(!q||n.title.includes(q)));
  const counts={};base.forEach(n=>counts[n.county]=(counts[n.county]||0)+1);
  $("chips").innerHTML=[["","전체",base.length],...Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([c,n])=>[c,c,n])]
    .map(([v,t,n])=>`<button class="chip" data-v="${v}" aria-pressed="${gun===v}">${t}<b>${n}</b></button>`).join("");
  document.querySelectorAll(".chip").forEach(b=>b.onclick=()=>{gun=b.dataset.v;render()});
  const rows=base.filter(n=>!gun||n.county===gun);
  $("sub").textContent=`최근 ${D.days}일 · ${D.generated} 기준 ${rows.length}건`;
  let last="",html="";
  rows.forEach(n=>{const g=label(n.date);if(g!==last){html+=`<h2>${g}</h2>`;last=g}
    html+=`<a class="row" href="${esc(n.url)}" target="_blank" rel="noopener"><div class="t">${esc(n.title)}</div>
    <div class="m">${n.match?'<i class="hit" title="임시거주시설 관련"></i>':''}<em>${esc(n.county)}</em><span>${esc(n.board)}</span></div></a>`});
  $("list").innerHTML=html||'<div class="none">조건에 맞는 공고가 없어요. 관련만 보기를 끄거나 다른 군을 선택해 보세요.</div>';
}
$("st").innerHTML=D.status.map(s=>`<tr class="${s.n||s.msg.startsWith("정상")?"":"bad"}"><td>${esc(s.county)}</td><td>${esc(s.board)}</td><td>${esc(s.msg)}</td></tr>`).join("");
function renderLinks(){
  const q=$("q2").value.trim();let last="",h="";
  D.links.filter(l=>!q||l.county.includes(q)).forEach(l=>{
    if(l.province!==last){h+=`<h2>${esc(l.province)}</h2>`;last=l.province}
    h+=`<div class="lk"><span class="cn">${esc(l.county)}</span><span class="bs"><a class="btn home" href="${esc(l.home)}" target="_blank" rel="noopener">홈페이지</a>`
      +l.boards.map(b=>`<a class="btn" href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.board)}</a>`).join("")+`</span></div>`});
  $("links").innerHTML=h||'<div class="none">일치하는 군이 없어요.</div>';
}
function show(n){
  $("v1").hidden=n!=1;$("v2").hidden=n!=2;
  $("t1").setAttribute("aria-pressed",n==1);$("t2").setAttribute("aria-pressed",n==2);
  if(n==1)render();else{$("sub").textContent="군청 홈페이지와 공지사항·고시공고 게시판으로 바로 이동해요";renderLinks()}
}
$("t1").onclick=()=>show(1);$("t2").onclick=()=>show(2);$("q2").oninput=renderLinks;
$("only").onchange=$("q").oninput=render;show(1);
</script></body></html>"""

if __name__ == "__main__":
    main()
