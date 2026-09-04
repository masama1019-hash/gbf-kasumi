#!/usr/bin/env python3
"""古戦場 個人ランキング追跡アプリ (依存ライブラリなし)

決まった数人の個人貢献度を、英雄(2000位)・10万位・15万位のボーダーと並べて見る。
団の勝敗を見る本体アプリ(/Applications/gbf/webapp)とは別物。

要になっているのは gbfdata の users/borders で、
  ?ranks=2000,100000,150000&user_ids=a,b,c
と渡すと「ボーダー3本」と「指定した人全員の時刻毎(順位つき)」が1リクエストで返る。
本体アプリの個ランはこの引数を使っておらず、順位帯を近傍探索していて重い。こちらは1.2秒。

起動:  python3 /Applications/gbf/sunatsu/server.py   → http://localhost:8931
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8931))
BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
GBF = "https://gbfdata.com/api"

def _members():
    """追跡する人のユーザーID。gbfdataから名前を取れるのでIDだけ持つ(改名にも自動で追従)。

    ⚠️ 個人IDはコードに書かない。このリポジトリは公開されているため、
    本番は環境変数 KORAN_UIDS(カンマ区切り)、手元は members.txt(gitignore済) から読む"""
    raw = os.environ.get("KORAN_UIDS", "")
    if not raw:
        f = os.path.join(BASE, "members.txt")
        if os.path.isfile(f):
            with open(f) as fp:
                raw = fp.read()
    out = []
    for tok in raw.replace("\n", ",").split(","):
        tok = tok.split("#")[0].strip()          # 行末コメントを許す
        if tok.isdigit():
            out.append(int(tok))
    return out


MEMBERS = _members()          # 画面が何も指定しなかったときの初期メンバー
MAX_UIDS = 15                 # 1リクエストで見る人数の上限(URLとレスポンスが膨らむのを防ぐ)

# 並べるボーダー。ラベルはそのまま画面に出る
LINES = [(2000, "英雄(2000位)", "#D85A30"),
         (100000, "10万位", "#1D9E75"),
         (150000, "15万位", "#8A6FC4")]

# 人の線の色(登録順)。ボーダーの赤・緑・紫と混ざらない色。人数が色数を超えたら循環する
MCOLORS = ["#378ADD", "#E8A33D", "#2FA8A0", "#C9578E", "#6E7BD6", "#B5762E",
           "#4B9E5F", "#D4694A", "#7E63A8", "#2C7A8C"]

DAY_LABEL = {1: "予選1日目", 2: "予選2日目", 3: "インターバル",
             4: "本戦1日目", 5: "本戦2日目", 6: "本戦3日目", 7: "本戦4日目"}

CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "application/javascript; charset=utf-8", ".png": "image/png",
          ".ico": "image/x-icon", ".svg": "image/svg+xml"}

_cache = {}
_lock = threading.Lock()
CACHE_MAX = 60          # 1回の開催で1エントリしか使わないので小さくてよい
NEG_TTL = 120           # 取得失敗を覚えておく秒数


def get(url, ttl=180):
    """GET + TTLキャッシュ。gbfdataは User-Agent が無いと403を返すので必ず付ける"""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < (ttl if hit[1] is not None else NEG_TTL):
            return hit[1]
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=25).read())
    except Exception:
        data = None                       # 未開催の回などは非JSONが返る
    with _lock:
        _cache[url] = (now, data)
        if len(_cache) > CACHE_MAX:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:len(_cache) - CACHE_MAX]:
                _cache.pop(k, None)
    return data


def jst_today():
    """古戦場の「今日」(JST・5時未満はまだ前日扱い)"""
    now = datetime.now(timezone(timedelta(hours=9)))
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def hour_label(t):
    """25:00〜30:00 は日をまたいだ 1時〜6時。24:00 は 24時 のまま"""
    h = int(t.split(":")[0])
    return f"{h - 24 if h > 24 else h}時"


def oku(p):
    return None if p is None else round(p / 1e8, 1)


def fetch(raid, uids):
    """1リクエストでボーダー3本＋指定した人全員の時刻毎を取る。
    URLにuidsが入るので、見る人ごとに違う一覧でもキャッシュは混ざらない"""
    url = (f"{GBF}/users/borders?raid_number={raid}"
           f"&ranks={','.join(str(r) for r, _, _ in LINES)}")
    if uids:                         # 空のuser_idsを付けるとgbfdataがエラーを返す
        url += f"&user_ids={','.join(str(u) for u in uids)}"
    return get(url, ttl=180)


def raid_meta():
    """開催回の自動追従。gbfdataが返す latest_raid_number をそのまま最新とする。
    毎時データは直近2回しか残らないので、選べるのは最新と1つ前だけ"""
    d = fetch_latest()
    if not d:
        return {"latest": None, "raids": [], "schedules": []}
    m = d.get("meta") or {}
    latest = m.get("latest_raid_number") or m.get("raid_number")
    raids = [r for r in (latest, latest - 1 if latest else None) if r]
    return {"latest": latest, "raids": raids, "schedules": m.get("schedules") or []}


def fetch_latest():
    """最新回が何回かを知るための軽い呼び出し。raid_numberを省くと最新回が返る"""
    return get(f"{GBF}/users/borders?ranks=2000", ttl=600)


def series(points):
    """時刻毎の点を {key: 億} と {key: 順位} に開く。keyは "日付 時刻" で全期間を通して一意"""
    cum, rank = {}, {}
    for p in points or []:
        k = f"{p['day']} {p['time']}"
        cum[k] = oku(p.get("point"))
        if p.get("rank") is not None:
            rank[k] = p["rank"]
    return cum, rank


def daily(points):
    """日別の着地。その日の最後の点＝日終わりの通算累積。
    当日ぶんは前日終わりとの差。順位はその日の最後の値"""
    last = {}
    for p in points or []:
        do = p.get("day_of")
        if do:
            last[do] = p                       # 時系列順に来るので上書きで最終値になる
    out, prev = [], 0.0
    for do in sorted(last):
        p = last[do]
        c = oku(p.get("point"))
        out.append({"day_of": do, "day": p["day"], "label": DAY_LABEL.get(do, f"{do}日目"),
                    "cum": c, "gain": None if c is None else round(c - prev, 1),
                    "rank": p.get("rank")})
        if c is not None:
            prev = c
    return out


def build(raid, uids):
    d = fetch(raid, uids)
    if not d:
        return None
    keys = []                                   # 全系列の時刻を通しで並べる
    seen = set()
    for s in (d.get("data") or []) + (d.get("users") or []):
        for p in s.get("points") or []:
            k = f"{p['day']} {p['time']}"
            if k not in seen:
                seen.add(k)
                keys.append(k)
    keys.sort()                                 # "YYYY-MM-DD HH:MM" は辞書順=時系列

    days, seenday = [], set()
    for s in (d.get("data") or []) + (d.get("users") or []):
        for p in s.get("points") or []:
            do = p.get("day_of")
            if do and do not in seenday:
                seenday.add(do)
                days.append({"day_of": do, "day": p["day"],
                             "label": DAY_LABEL.get(do, f"{do}日目")})
    days.sort(key=lambda x: x["day_of"])

    byrank = {s.get("target_rank"): s for s in (d.get("data") or [])}
    lines = []
    for r, label, color in LINES:
        s = byrank.get(r) or {}
        cum, _ = series(s.get("points"))
        lines.append({"rank": r, "label": label, "color": color,
                      "cum": cum, "daily": daily(s.get("points"))})

    byuid = {u.get("user_id"): u for u in (d.get("users") or [])}
    members = []
    for i, uid in enumerate(uids):
        u = byuid.get(uid) or {}
        cum, rank = series(u.get("points"))
        members.append({"uid": uid, "name": u.get("name") or str(uid),
                        "level": u.get("level"), "color": MCOLORS[i % len(MCOLORS)],
                        "cum": cum, "rank": rank, "daily": daily(u.get("points"))})
    return {"raid": raid, "keys": keys, "labels": [hour_label(k.split(" ")[1]) for k in keys],
            "days": days, "lines": lines, "members": members}


def last_key(cum, keys):
    for k in reversed(keys):
        if cum.get(k) is not None:
            return k
    return None


def project(cur, prev):
    """着地見込み。現時点の値 ×(前回の最終 ÷ 前回の同じ位置)。
    同じ位置は「day_of と時刻が同じ点」で合わせる。前回が無ければ None"""
    if not prev:
        return {"members": {}, "lines": {}}   # 前回が無い回でも形は同じにする
    pos = {}                                    # (day_of, 時刻) -> 前回のkey
    for dd in prev["days"]:
        for k in prev["keys"]:
            if k.startswith(dd["day"]):
                pos[(dd["day_of"], k.split(" ")[1])] = k
    dayof = {dd["day"]: dd["day_of"] for dd in cur["days"]}

    def one(cum_now, cum_prev):
        k = last_key(cum_now, cur["keys"])
        if not k:
            return None
        pk = pos.get((dayof.get(k.split(" ")[0]), k.split(" ")[1]))
        base = cum_prev.get(pk) if pk else None
        fin = cum_prev.get(last_key(cum_prev, prev["keys"]))
        if not base or not fin:
            return None
        return round(cum_now[k] * (fin / base), 1)

    out = {"members": {}, "lines": {}}
    pm = {m["uid"]: m for m in prev["members"]}
    for m in cur["members"]:
        p = pm.get(m["uid"])
        out["members"][str(m["uid"])] = one(m["cum"], p["cum"]) if p else None
    pl = {l["rank"]: l for l in prev["lines"]}
    for l in cur["lines"]:
        p = pl.get(l["rank"])
        out["lines"][str(l["rank"])] = one(l["cum"], p["cum"]) if p else None
    return out


def uids_arg(q):
    """画面が持っている一覧。指定が無ければ初期メンバー。
    重複を潰し、上限で切る(URLは誰でも叩けるので鵜呑みにしない)"""
    raw = q.get("uids", [""])[0]
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit() and int(tok) not in out:
            out.append(int(tok))
    return (out or list(MEMBERS))[:MAX_UIDS]


def api_search(q):
    """名前で人を探す。ブラウザからgbfdataを直接叩けないのでサーバが中継する。
    同名が多い(「TOMO」で20件)ので、現在の貢献度と順位も付けて選べるようにする"""
    term = (q.get("q", [""])[0] or "").strip()
    if not term:
        return {"error": "名前を入力してください"}
    if term.isdigit():                       # IDを直接入れられたときはそれ1件として扱う
        cand = [{"user_id": int(term), "name": term, "level": None}]
    else:
        d = get(f"{GBF}/users/search?q={urllib.parse.quote(term)}", ttl=600)
        cand = (d or {}).get("data") or []
    cand = [c for c in cand if c.get("user_id")][:12]
    if not cand:
        return {"hits": []}

    # 候補の現在値を1リクエストでまとめて取る。同名の見分けはこれが決め手になる
    raid = raid_arg(q)
    d = fetch(raid, [c["user_id"] for c in cand])
    cum = {}
    for u in (d or {}).get("users") or []:
        pts = [p for p in (u.get("points") or []) if p.get("point") is not None]
        if pts:
            cum[u["user_id"]] = (oku(pts[-1]["point"]), pts[-1].get("rank"), u.get("name"), u.get("level"))
    hits = []
    for c in cand:
        got = cum.get(c["user_id"])
        hits.append({"uid": c["user_id"], "name": (got[2] if got else None) or c.get("name"),
                     "level": (got[3] if got else None) or c.get("level"),
                     "cum": got[0] if got else None, "rank": got[1] if got else None})
    # 貢献度の多い順。探しているのは走っている人であることが多い
    hits.sort(key=lambda h: -(h["cum"] or -1))
    return {"raid": raid, "hits": hits}


def raid_arg(q):
    meta = raid_meta()
    try:
        r = int(q.get("raid", [""])[0])
    except ValueError:
        r = meta["latest"]
    return r if r in meta["raids"] else meta["latest"]


def api_board(q):
    uids = uids_arg(q)
    if not uids:
        return {"error": "追跡する人が設定されていません。"
                         "画面の「メンバーを編集」から追加するか、"
                         "環境変数 KORAN_UIDS に初期メンバーを設定してください"}
    meta = raid_meta()
    if not meta["raids"]:
        return {"error": "gbfdataから開催情報を取得できませんでした"}
    try:
        raid = int(q.get("raid", [""])[0])
    except ValueError:
        raid = meta["latest"]
    if raid not in meta["raids"]:
        raid = meta["latest"]

    with ThreadPoolExecutor(max_workers=2) as ex:
        fc = ex.submit(build, raid, uids)
        fp = ex.submit(build, raid - 1, uids)
        cur, prev = fc.result(), fp.result()
    if not cur:
        return {"error": f"第{raid}回のデータがまだありません"}

    cur["raids"] = meta["raids"]
    cur["latest"] = meta["latest"]
    cur["default_uids"] = list(MEMBERS)      # 「初期メンバーに戻す」で使う
    cur["max_uids"] = MAX_UIDS
    cur["today"] = jst_today()
    cur["proj"] = project(cur, prev)
    # 前回は着地見込みの計算に使うだけなので、最終値だけ返して転送量を抑える
    cur["prev"] = {"raid": raid - 1,
                   "members": {str(m["uid"]): m["cum"].get(last_key(m["cum"], prev["keys"]))
                               for m in prev["members"]},
                   "lines": {str(l["rank"]): l["cum"].get(last_key(l["cum"], prev["keys"]))
                             for l in prev["lines"]}} if prev else None
    return cur


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/api/board", "/api/search"):
            q = urllib.parse.parse_qs(u.query)
            data = api_board(q) if u.path == "/api/board" else api_search(q)
            self._send(json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        name = "index.html" if u.path == "/" else u.path.lstrip("/")
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            self._send(f.read(), CTYPES.get(ext, "application/octet-stream"))


if __name__ == "__main__":
    print(f"個人ランキング追跡  →  http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
