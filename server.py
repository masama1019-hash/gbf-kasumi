#!/usr/bin/env python3
"""グラブル古戦場サポートアプリ (ローカルWebサーバー・依存ライブラリなし)

機能:
  - ライブダッシュボード: 自団vs相手の毎時Day分・時速・リード (gbfdata)
  - 相手スカウト分析: 団名/団IDから過去実績・速度プロファイル・勝率目安

起動:  python3 /Applications/gbf/webapp/server.py   → http://localhost:8930
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8930))   # Render等のPaaSはPORT環境変数を渡す
BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
GBF = "https://gbfdata.com/api"
OURS_NAME = "霞桜団"
OURS_GID = 1147615
OPP_FILE = "/Applications/gbf/honsen_opponent.txt"
HOURS = [f"{h:02d}:00" for h in range(8, 24)] + ["24:00"]

_cache = {}
_cache_lock = threading.Lock()


def get(url, ttl=180):
    """GET with in-memory TTL cache."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=25).read())
    except Exception:
        return None
    with _cache_lock:
        _cache[url] = (now, data)
    return data


def rankings_page(raid, date, rank, time_=None, per_page=50):
    q = {"raid_number": raid, "day": date, "rank": max(1, rank), "per_page": per_page}
    if time_:
        q["time"] = time_
    d = get(f"{GBF}/guilds/rankings?" + urllib.parse.urlencode(q))
    return (d or {}).get("data") or []


def find_guild(raid, date, time_, gid=None, name=None, hint=300, max_pages=25):
    """rankingsから団を探す(hint近傍→拡張)。(point, rank, name, gid) or None"""
    base = ((hint - 1) // 50) * 50 + 1
    order, tried = [base], set()
    for dd in range(50, 3000, 50):
        order += [base + dd, base - dd]
    for s in order:
        if s < 1 or s > 30000 or s in tried:
            continue
        tried.add(s)
        for x in rankings_page(raid, date, s, time_):
            if (gid and x.get("guild_id") == gid) or (name and x.get("name") == name):
                return x["point"], x["rank"], x["name"], x["guild_id"]
        if len(tried) >= max_pages:
            break
    return None


def search_guild_by_name(name):
    """gbfdata全団検索API(/api/guilds/search)で同名団を全件取得。
    開催不参加や3000位圏外の団も拾える(イベントランキング走査では見つからない団も対応)。
    完全一致と部分一致を分けて返す。"""
    d = get(f"{GBF}/guilds/search?" + urllib.parse.urlencode({"q": name}), ttl=1800)
    data = (d or {}).get("data") or []
    exacts = [{"guild_id": g["guild_id"], "name": g["name"]} for g in data if g.get("name") == name]
    partial = [{"guild_id": g["guild_id"], "name": g["name"]} for g in data if g.get("name") != name]
    return exacts, partial[:8]


def raid_final_rank(rows, raid):
    """historiesから当該開催の最終(最大day_of)総合順位。不参加ならNone"""
    ev = [x for x in rows if x["raid_number"] == raid]
    if not ev:
        return None
    return max(ev, key=lambda x: x["day_of"])["rank"]


def prev_contrib(rows, raid, cur_do):
    """マッチング基準の前日貢献度(億)。本戦1日目=予選計、本戦2日目以降=前日の当日分"""
    ev = {r["day_of"]: r for r in rows if r["raid_number"] == raid}
    if cur_do in (5, 6, 7) and (cur_do - 1) in ev:
        return round(ev[cur_do - 1]["today_point"] / 1e8, 1)
    y = ev.get(1, {}).get("today_point", 0) + ev.get(2, {}).get("today_point", 0)
    return round(y / 1e8, 1) if y else None


def guild_histories(gid):
    rows = []
    for pg in range(1, 8):
        d = get(f"{GBF}/guilds/{gid}/histories?page={pg}", ttl=3600)
        data = (d or {}).get("data") or []
        if not data:
            break
        rows += data
    return rows


def meta_for(raid=None):
    """開催情報(raid番号と日程表)。raid指定で過去回、無指定で最新回。
    古い回はborders APIに日程が無いため自団historiesから再構成する。"""
    url = f"{GBF}/users/borders" + (f"?raid_number={raid}" if raid else "")
    d = get(url, ttl=600)
    meta = (d or {}).get("meta") or {}
    latest = meta.get("latest_raid_number")
    rn = raid or latest or meta.get("raid_number")
    sched = meta.get("schedules") or []
    if not sched and rn:
        sched = [{"raid_number": rn, "day": r["day"], "day_of": r["day_of"]}
                 for r in sorted(guild_histories(OURS_GID), key=lambda x: x.get("day", ""))
                 if r["raid_number"] == rn]
    if not latest:
        d2 = get(f"{GBF}/users/borders", ttl=600)
        latest = ((d2 or {}).get("meta") or {}).get("latest_raid_number") or rn
    return {"raid": rn, "latest": latest, "schedules": sched}


def raid_arg(q):
    v = (q.get("raid", [""])[0] or "").strip()
    return int(v) if v.isdigit() else None


def hourly_series(raid, date, base_point, gid, hint):
    """1日分の毎時Day分series {time: 億}"""
    out = {}
    h = hint
    for t in HOURS:
        r = find_guild(raid, date, t, gid=gid, hint=h)
        if r:
            out[t] = round((r[0] - base_point) / 1e8, 1)
            h = r[1]
    return out


def day_base(hist_rows, raid, date):
    """dateの前日(=直前day_of)終了累計をhistoriesから"""
    ev = sorted([r for r in hist_rows if r["raid_number"] == raid], key=lambda r: r["day_of"])
    prev = None
    for r in ev:
        if r["day"] < date:
            prev = r
        elif r["day"] == date:
            break
    return prev["point"] if prev else 0


# ---------- API handlers ----------

def api_config(q):
    opp = ""
    if os.path.exists(OPP_FILE):
        opp = open(OPP_FILE).read().strip().splitlines()[0].strip() if open(OPP_FILE).read().strip() else ""
    m = meta_for(raid_arg(q))
    # 参加履歴に加え、最新回・選択中の回は必ず一覧に含める(開催直後で
    # まだ自団の履歴行が無くてもタブに出るように)。
    rset = {x["raid_number"] for x in guild_histories(OURS_GID)}
    rset |= {r for r in (m["raid"], m["latest"]) if r}
    raids = sorted(rset, reverse=True)
    return {"ours": OURS_NAME, "opponent": opp, "raid": m["raid"], "latest": m["latest"],
            "raids": raids, "schedules": m["schedules"]}


def _speeds(series):
    sp, prev = {}, 0
    for t in HOURS:
        if t in series:
            sp[t] = round(series[t] - prev, 1)
            prev = series[t]
    return sp


def api_live(q):
    m = meta_for(raid_arg(q))
    raid = m["raid"]
    date = q.get("date", [None])[0]
    battle = [s for s in m["schedules"] if s.get("day_of", 0) >= 4]
    if not date:
        date = battle[-1]["day"] if battle else time.strftime("%Y-%m-%d")
    day_label = {s["day"]: f"本戦{s['day_of'] - 3}" for s in battle}
    past_n = int(q.get("past", ["0"])[0])
    past_dates = [s["day"] for s in battle if s["day"] < date][-past_n:] if past_n else []

    opp_q = (q.get("opp", [None])[0] or "").strip()
    if not opp_q:
        return {"error": "相手団情報を入力してください（団名 または 団ID）"}

    ours_hist = guild_histories(OURS_GID)
    cur_do = next((s["day_of"] for s in m["schedules"] if s["day"] == date), 99)
    opp_gid, opp_name = None, opp_q
    if re.fullmatch(r"\d{3,9}", opp_q):
        # 団ID直接指定(名前検索不要・確実)
        opp_gid = int(opp_q)
        rows = guild_histories(opp_gid)
        if rows:
            opp_name = rows[0].get("name", opp_q)
    elif opp_q:
        founds, partial = search_guild_by_name(opp_q)
        cand_src = founds if founds else partial
        if len(cand_src) > 1:
            # 同名団(または部分一致)が複数 → 前日(予選)貢献度と総合順位で選ばせる
            cands = []
            for g in cand_src:
                gh = guild_histories(g["guild_id"])
                cands.append({"gid": g["guild_id"], "name": g["name"],
                              "prev": prev_contrib(gh, raid, cur_do), "rank": raid_final_rank(gh, raid)})
            cands.sort(key=lambda c: (c["prev"] is None, c["rank"] is None, c["rank"] or 0))
            return {"candidates": cands, "prev_label": "予選(計)" if cur_do <= 4 else f"本戦{cur_do - 4}日目"}
        elif cand_src:
            opp_gid = cand_src[0]["guild_id"]
            opp_name = cand_src[0]["name"]
        else:
            return {"error": f"「{opp_q}」が見つかりません。団名を正確に入力するか、団IDで指定してください"}
    opp_hist = guild_histories(opp_gid) if opp_gid else []
    if not opp_hist:
        return {"error": f"「{opp_name}」は第{raid}回に参加していないため表示できません（団IDが正しいかご確認ください）"}

    # historiesの各日最終rankを探索起点に使う(どの順位帯の団でも高速・確実)。
    # 朝の順位は前日最終に近いので「対象日より前の直近日のrank」を優先。
    def hint_for(rows, d, default):
        days = sorted([(r["day"], r["rank"]) for r in rows if r["raid_number"] == raid])
        prev = [rk for dy, rk in days if dy < d]
        same = [rk for dy, rk in days if dy == d]
        return prev[-1] if prev else (same[0] if same else default)

    # 前日基準(Day分)を保証: historiesに前日が無ければ前日24:00ランキングから補完
    # (基準0のまま計算すると総貢献度が混ざり日次リードが狂うため)
    sched_days = sorted(s["day"] for s in m["schedules"])

    def base_for(hist, gid, d, hint):
        b = day_base(hist, raid, d)
        if b == 0 and gid:
            prevs = [x for x in sched_days if x < d]
            if prevs:
                r = find_guild(raid, prevs[-1], "24:00", gid=gid, hint=hint)
                if r:
                    b = r[0]
        return b

    def series_job(hist, gid, d, hint):
        return hourly_series(raid, d, base_for(hist, gid, d, hint), gid, hint)

    # 今日+過去日を並列取得(同じ2団を過去日にも遡って追う)
    jobs = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for d in [date] + past_dates:
            jobs[("ours", d)] = ex.submit(series_job, ours_hist, OURS_GID, d, hint_for(ours_hist, d, 250))
            if opp_gid:
                jobs[("opp", d)] = ex.submit(series_job, opp_hist, opp_gid, d, hint_for(opp_hist, d, 400))
        res = {k: f.result() for k, f in jobs.items()}

    ours = res.get(("ours", date), {})
    opp = res.get(("opp", date), {})
    past = [{"date": d, "label": day_label.get(d, d),
             "ours": {"cum": res.get(("ours", d), {}), "speed": _speeds(res.get(("ours", d), {}))},
             "opp": {"cum": res.get(("opp", d), {}), "speed": _speeds(res.get(("opp", d), {}))}}
            for d in past_dates]

    # 参考情報: 両団の予選(合計)と対象日より前の本戦日毎貢献度(historiesのtoday_point=公式値)
    # 各行に日程終了時点の団総合順位(historiesのrank)も添える
    lbl = {4: "本戦1日目", 5: "本戦2日目", 6: "本戦3日目", 7: "本戦4日目"}
    o_ev = {x["day_of"]: x["today_point"] / 1e8 for x in ours_hist if x["raid_number"] == raid}
    p_ev = {x["day_of"]: x["today_point"] / 1e8 for x in opp_hist if x["raid_number"] == raid}
    o_rank = {x["day_of"]: x["rank"] for x in ours_hist if x["raid_number"] == raid}
    p_rank = {x["day_of"]: x["rank"] for x in opp_hist if x["raid_number"] == raid}
    ref = [{"label": "予選(計)",
            "ours": round(o_ev.get(1, 0) + o_ev.get(2, 0), 1),
            "opp": round(p_ev.get(1, 0) + p_ev.get(2, 0), 1) if p_ev else None,
            "ours_rank": o_rank.get(2) or o_rank.get(1),
            "opp_rank": p_rank.get(2) or p_rank.get(1)}]
    for do in range(4, cur_do):
        if do in o_ev or do in p_ev:
            ref.append({"label": lbl.get(do, str(do)),
                        "ours": round(o_ev.get(do, 0), 1),
                        "opp": round(p_ev[do], 1) if do in p_ev else None,
                        "ours_rank": o_rank.get(do), "opp_rank": p_rank.get(do)})

    # ---- 相手方針判定(時速ベース) & 本日勝率予想(過去戦績×当日推移) ----
    forecast = None
    common = [t for t in HOURS if t in ours and t in opp]
    if opp_gid and common:
        import math
        hk = common[-1]
        idx = HOURS.index(hk)
        elapsed, remain = idx + 1, len(HOURS) - idx - 1
        o_now, p_now = ours[hk], opp[hk]
        prev_day = past[-1] if past else None
        o_sp, p_sp = _speeds(ours), _speeds(opp)

        # 相手方針: 直近2hの時速 vs 前日同時間帯の時速
        policy = None
        recent = [p_sp[t] for t in common[-2:] if t in p_sp]
        if recent and prev_day:
            y_sp = _speeds(prev_day["opp"]["cum"])
            y_recent = [y_sp[t] for t in common[-2:] if t in y_sp]
            if y_recent and sum(y_recent) > 0:
                pr = sum(recent) / sum(y_recent)
                pct = round(pr * 100)
                today_avg = p_now / max(1, elapsed)
                if idx >= 10 and sum(recent) / len(recent) > 1.3 * today_avg:
                    policy = {"label": "終盤爆発中⚠", "pct": pct, "tone": "bad"}
                elif pct >= 115:
                    policy = {"label": "全開(前日超)", "pct": pct, "tone": "bad"}
                elif pct >= 85:
                    policy = {"label": "通常運転", "pct": pct, "tone": "mid"}
                elif pct >= 55:
                    policy = {"label": "減速(手抜き?)", "pct": pct, "tone": "good"}
                else:
                    policy = {"label": "撤退モード", "pct": pct, "tone": "good"}

        # 最終予測: 前日の「同時刻→24時の残り伸び」を今日のペース比で補正して加算
        def proj(now, side):
            if prev_day:
                yc = prev_day[side]["cum"]
                if hk in yc and "24:00" in yc and yc[hk] > 0:
                    rest = max(0.0, yc["24:00"] - yc[hk])
                    ratio = min(1.5, max(0.5, now / yc[hk]))
                    return now + rest * ratio
            return now + (now / max(1, elapsed)) * remain
        fo, fp = proj(o_now, "ours"), proj(p_now, "opp")

        # 事前確率: 過去3回(現開催除く)の本戦日毎平均の力関係
        def honsen_avg(rows):
            raids = sorted({x["raid_number"] for x in rows if x["raid_number"] != raid}, reverse=True)[:3]
            v = [x["today_point"] / 1e8 for x in rows if x["raid_number"] in raids and x["day_of"] >= 4]
            return sum(v) / len(v) if v else None
        oa, pa = honsen_avg(ours_hist), honsen_avg(opp_hist)
        prior = 0.5
        if oa and pa:
            prior = min(0.95, max(0.05, 0.5 + (oa / pa - 1) * 0.8))

        # 当日予測の確率化(残り時間が多いほど不確実性大) → 経過に応じて事前確率とブレンド
        sigma = max(25.0, (fo + fp) / 2 * 0.06 + (fo + fp) / 2 * 0.30 * remain / len(HOURS))
        p_proj = 1 / (1 + math.exp(-(fo - fp) / sigma))
        w = elapsed / len(HOURS)
        win = round(100 * ((1 - w) * prior + w * p_proj))
        forecast = {"win": max(2, min(98, win)), "proj_ours": round(fo, 1), "proj_opp": round(fp, 1),
                    "policy": policy, "prior": round(prior * 100),
                    "basis": "前日推移ベース" if prev_day else "平均時速ベース"}

    # 過去開催の総合順位推移(最終day_ofのrank)
    def final_ranks(rows):
        ev = {}
        for r in rows:
            rr = r["raid_number"]
            if rr not in ev or r["day_of"] > ev[rr]["day_of"]:
                ev[rr] = r
        return {rr: v["rank"] for rr, v in ev.items()}
    o_rk, p_rk = final_ranks(ours_hist), final_ranks(opp_hist)
    rk_raids = sorted(set(o_rk) | set(p_rk), reverse=True)[:8][::-1]
    rank_history = [{"raid": rn, "ours": o_rk.get(rn), "opp": p_rk.get(rn)} for rn in rk_raids]

    return {"date": date, "raid": raid, "hours": HOURS, "label": day_label.get(date, date),
            "ours": {"name": OURS_NAME, "cum": ours, "speed": _speeds(ours)},
            "opp": {"name": opp_name or "", "gid": opp_gid, "cum": opp, "speed": _speeds(opp)},
            "past": past, "ref": ref, "forecast": forecast, "rank_history": rank_history}


def api_scout(q):
    query = (q.get("q", [""])[0] or "").strip()
    if not query:
        return {"error": "団名または団IDを入力してください"}
    m = meta_for(raid_arg(q))
    raid = m["raid"]
    sched = {s["day_of"]: s["day"] for s in m["schedules"]}
    last_battle_date = sched.get(7) or sched.get(6) or sched.get(5) or sched.get(4)

    cur_do = next((s["day_of"] for s in m["schedules"] if s["day"] == last_battle_date), 7)
    gid, gname = None, None
    if re.fullmatch(r"\d{3,9}", query):
        gid = int(query)
        rows = guild_histories(gid)
        if rows:
            gname = rows[0].get("name")
    else:
        exacts, partial = search_guild_by_name(query)
        cand_src = exacts if exacts else partial
        if len(cand_src) > 1:
            cands = []
            for g in cand_src:
                gh = guild_histories(g["guild_id"])
                cands.append({"gid": g["guild_id"], "name": g["name"],
                              "prev": prev_contrib(gh, raid, cur_do), "rank": raid_final_rank(gh, raid)})
            cands.sort(key=lambda c: (c["prev"] is None, c["rank"] is None, c["rank"] or 0))
            return {"candidates": cands, "prev_label": "予選(計)" if cur_do <= 4 else f"本戦{cur_do - 4}日目"}
        elif cand_src:
            gid, gname = cand_src[0]["guild_id"], cand_src[0]["name"]
    if not gid:
        return {"error": f"「{query}」が見つかりません。団名を正確に入力するか、団IDで指定してください"}

    rows = guild_histories(gid)
    if not rows and gname is None:
        return {"error": "履歴が取得できませんでした"}
    gname = gname or rows[0].get("name", f"ID{gid}")

    def event_summary(r):
        ev = {x["day_of"]: x for x in rows if x["raid_number"] == r}
        if not ev:
            return None
        last = ev[max(ev)]
        return {"raid": r,
                "daily": {do: round(ev[do]["today_point"] / 1e8, 1) for do in sorted(ev)},
                "total": round(last["point"] / 1e8, 1), "final_rank": last["rank"]}

    raids = sorted({x["raid_number"] for x in rows}, reverse=True)
    events = [e for e in (event_summary(r) for r in raids[:4]) if e]

    # 過去3回(現開催除く) 本戦日毎平均
    past = [e for e in events if e["raid"] != raid][:3]
    pv = [v for e in past for do, v in e["daily"].items() if do >= 4]
    past_avg = round(sum(pv) / len(pv), 1) if pv else None

    # 自団との比較
    ours_rows = guild_histories(OURS_GID)
    ours_ev = {x["day_of"]: x for x in ours_rows if x["raid_number"] == raid}
    opp_ev = {x["day_of"]: x for x in rows if x["raid_number"] == raid}
    compare = []
    lbl = {1: "予選1", 2: "予選2", 3: "IB", 4: "本戦1", 5: "本戦2", 6: "本戦3", 7: "本戦4"}
    for do in sorted(set(ours_ev) | set(opp_ev)):
        compare.append({"label": lbl.get(do, str(do)),
                        "ours": round(ours_ev[do]["today_point"] / 1e8, 1) if do in ours_ev else None,
                        "opp": round(opp_ev[do]["today_point"] / 1e8, 1) if do in opp_ev else None})

    ours_pv = [x["today_point"] / 1e8 for x in ours_rows
               if x["raid_number"] in [e["raid"] for e in past] and x["day_of"] >= 4]
    ours_avg = round(sum(ours_pv) / len(ours_pv), 1) if ours_pv else None
    winrate = None
    if past_avg and ours_avg:
        ratio = ours_avg / past_avg
        winrate = max(5, min(95, round(50 + (ratio - 1) * 80)))

    return {"name": gname, "gid": gid, "url": f"https://game.granbluefantasy.jp/#guild/detail/{gid}",
            "events": events, "past_avg": past_avg, "ours_avg": ours_avg,
            "winrate": winrate, "compare": compare}


def _snapshot_times(raid, date):
    """その予選日の利用可能スナップショット時刻を昇順で返す(20:00〜30:00等)"""
    d = get(f"{GBF}/guilds/rankings?" + urllib.parse.urlencode(
        {"raid_number": raid, "day": date, "rank": 300, "per_page": 1}))
    ts = sorted({s["time"] for s in (d or {}).get("snapshots", [])
                 if s.get("day") == date and s.get("time")},
                key=lambda t: int(t.split(":")[0]))
    return ts


def yosen_series(raid, dates, ours_hint=120):
    """予選(dates=予選1,2日目)を連続タイムラインで 自団cum/rank と 300位cum を収集(並列)"""
    # 予選は「1日目19時開始 〜 2日目24時(翌0時)終了」。gbfdataは20:00〜30:00表記なので
    # 開始19時を先頭に足し、最終日は24:00までに切り詰める(以降の余剰スナップは除外)。
    o_cum, o_rank, b_cum, labels = {}, {}, {}, []
    snaps = []
    for i, date in enumerate(dates):
        times = _snapshot_times(raid, date)
        if i == 0 and "19:00" not in times:
            times = ["19:00"] + times
        if i == len(dates) - 1:
            times = [t for t in times if int(t.split(":")[0]) <= 24]
        for t in times:
            key = f"{date} {t}"
            labels.append((key, f"{int(t.split(':')[0]) % 24}時"))
            snaps.append((key, date, t))

    def one(item):
        key, date, t = item
        res = {"key": key}
        bd = get(f"{GBF}/guilds/rankings?" + urllib.parse.urlencode(
            {"raid_number": raid, "day": date, "rank": 300, "per_page": 1, "time": t}))
        if bd and bd.get("data"):
            res["b"] = round(bd["data"][0]["point"] / 1e8, 1)
        r = find_guild(raid, date, t, gid=OURS_GID, name=OURS_NAME, hint=ours_hint, max_pages=12)
        if r:
            res["o"], res["r"] = round(r[0] / 1e8, 1), r[1]
        return res

    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(one, snaps):
            if "b" in res:
                b_cum[res["key"]] = res["b"]
            if "o" in res:
                o_cum[res["key"]], o_rank[res["key"]] = res["o"], res["r"]
    # データが全く無いキー(存在しない19:00等)は除外
    labels = [(k, l) for k, l in labels if k in b_cum or k in o_cum]
    keys = [k for k, _ in labels]

    def speed(cum):
        sp, prev = {}, 0
        for k in keys:
            if k in cum:
                sp[k] = round(cum[k] - prev, 1)
                prev = cum[k]
        return sp
    return {"keys": keys, "labels": [l for _, l in labels],
            "ours": {"cum": o_cum, "rank": o_rank, "speed": speed(o_cum)},
            "border": {"cum": b_cum, "speed": speed(b_cum)}}


def api_yosen(q):
    raid = raid_arg(q) or meta_for()["raid"]
    def yosen_dates(rn):
        sc = meta_for(rn)["schedules"]
        return [s["day"] for s in sorted(sc, key=lambda s: s["day_of"]) if s["day_of"] in (1, 2)]
    cur = yosen_series(raid, yosen_dates(raid))
    prev = yosen_series(raid - 1, yosen_dates(raid - 1)) if yosen_dates(raid - 1) else None
    return {"raid": raid, "keys": cur["keys"], "labels": cur["labels"],
            "ours": cur["ours"], "border": cur["border"],
            "prev": {"labels": prev["labels"], "ours": prev["ours"], "border": prev["border"]} if prev else None}


# ---------- 個人ランキング(個ラン) ----------
KORAN_LABELS = {1: "予選1", 2: "予選2", 3: "中間", 4: "本戦1", 5: "本戦2", 6: "本戦3", 7: "本戦4"}


def user_search(q):
    d = get(f"{GBF}/users/search?q=" + urllib.parse.quote(q), ttl=300)
    return (d or {}).get("data") or []


def user_histories(uid, pages=6):
    rows = []
    for pg in range(1, pages + 1):
        d = get(f"{GBF}/users/{uid}/histories?page={pg}", ttl=300)
        data = (d or {}).get("data") or []
        rows += data
        if not (d or {}).get("meta", {}).get("has_next"):
            break
    return rows


def user_border_days(raid):
    """個人ボーダー rank:2000/100000 の day_of別 日終了累積(億)。pointは通算累積。
    {target_rank: {day_of: 億}}"""
    d = get(f"{GBF}/users/borders?raid_number={raid}", ttl=300)
    out = {}
    for s in (d or {}).get("data") or []:
        by = {}
        for pt in s.get("points") or []:
            do, p = pt.get("day_of"), pt.get("point")
            if do is not None and p is not None:
                by[do] = round(p / 1e8, 1)  # 時刻昇順なので最後=その日終了(30:00/24:00)
        out[s.get("target_rank")] = by
    return out


def user_border_hourly(raid, date):
    """個人ボーダー rank:2000/100000 の指定日の時刻毎累積(億)。{target_rank: {time: 億}}"""
    d = get(f"{GBF}/users/borders?raid_number={raid}", ttl=300)
    out = {2000: {}, 100000: {}}
    for s in (d or {}).get("data") or []:
        tr = s.get("target_rank")
        if tr not in out:
            continue
        for pt in s.get("points") or []:
            if pt.get("day") == date and pt.get("time") and pt.get("point") is not None:
                out[tr][pt["time"]] = round(pt["point"] / 1e8, 1)
    return out


def user_rankings_page(raid, date, rank, time_=None, per_page=200):
    q = {"raid_number": raid, "day": date, "rank": max(1, rank), "per_page": per_page}
    if time_:
        q["time"] = time_
    d = get(f"{GBF}/users/rankings?" + urllib.parse.urlencode(q))
    return (d or {}).get("data") or []


def find_user(raid, date, time_, uid, hint=3000, max_pages=40):
    """個人rankingsから uid を探す(hint近傍→外側へ拡張)。(point億, rank, hourly_point) or None"""
    base = ((hint - 1) // 200) * 200 + 1
    order, tried = [base], set()
    for dd in range(200, 20000, 200):
        order += [base + dd, base - dd]
    for s in order:
        if s < 1 or s > 300000 or s in tried:
            continue
        tried.add(s)
        for x in user_rankings_page(raid, date, s, time_):
            if x.get("user_id") == uid:
                return round(x["point"] / 1e8, 1), x["rank"], x.get("hourly_point")
        if len(tried) >= max_pages:
            break
    return None


def koran_hourly(raid, date, uid, hint=3000):
    """指定日の 本人 と 2000位/100000位 の時刻毎累積・時速(億)。本人はfind_userで並列取得"""
    b = user_border_hourly(raid, date)
    b2000, b100k = b.get(2000, {}), b.get(100000, {})
    times = sorted(set(b2000) | set(b100k), key=lambda t: int(t.split(":")[0]))
    p_cum, p_rank = {}, {}

    def one(t):
        return t, find_user(raid, date, t, uid, hint=hint)

    with ThreadPoolExecutor(max_workers=10) as ex:
        for t, r in ex.map(one, times):
            if r:
                p_cum[t], p_rank[t] = r[0], r[1]

    def speed(cum):
        sp, prev = {}, None
        for t in times:
            if t in cum:
                sp[t] = round(cum[t] - prev, 1) if prev is not None else None
                prev = cum[t]
        return sp
    return {"times": times, "labels": [f"{int(t.split(':')[0]) % 24}時" for t in times],
            "player": {"cum": p_cum, "rank": p_rank, "speed": speed(p_cum)},
            "b2000": {"cum": b2000, "speed": speed(b2000)},
            "b100k": {"cum": b100k, "speed": speed(b100k)}}


def _fin(b):
    return b[max(b)] if b else None


# 個人ボーダー最終着地(億)フォールバック。gbfdataが個人ボーダーを収録しない過去回
# (81回以前)の「直近3回の着地」参考用。出典: グランブルーファンタジー.gamewith.jp
# /article/show/91154 (第82・83回はgbfdata実値と一致で検証済み)
GW_BORDER_FINAL = {
    83: {"b2000": 502.3, "b100k": 64.6}, 82: {"b2000": 456.3, "b100k": 70.5},
    81: {"b2000": 361.2, "b100k": 50.3}, 80: {"b2000": 234.7, "b100k": 33.1},
    79: {"b2000": 241.4, "b100k": 31.3}, 78: {"b2000": 179.4, "b100k": 23.8},
    77: {"b2000": 219.4, "b100k": 34.7},
}


def koran_past3(uid, raid, hist):
    """各ライン(本人/2000位/10万位)の直近過去6回(raid-1..-6)の最終着地(億)。
    2000位/10万位はgbfdata優先、無ければGameWith履歴(GW_BORDER_FINAL)で補完"""
    raids = [raid - k for k in range(1, 7)]
    out = {"raids": raids, "labels": [f"第{r}回" for r in raids], "player": [], "b2000": [], "b100k": []}
    for r in raids:
        bd = user_border_days(r)
        fb = GW_BORDER_FINAL.get(r, {})
        out["b2000"].append(_fin(bd.get(2000, {})) or fb.get("b2000"))
        out["b100k"].append(_fin(bd.get(100000, {})) or fb.get("b100k"))
        pe = {x["day_of"]: x for x in hist if x["raid_number"] == r}
        out["player"].append(round(pe[max(pe)]["point"] / 1e8, 1) if pe else None)
    return out


def koran_time_proj(raid, do, uid, hint, cur, times, hist):
    """時点(最新時刻)での前回比較着地予想。前回開催の同day_of・同時刻に揃えて
    現時点値 ×(前回最終 ÷ 前回同時点)で予測。cur={key:{time:億}}"""
    prev_raid = raid - 1
    pdate = {s["day_of"]: s["day"] for s in meta_for(prev_raid)["schedules"]}.get(do)
    if not pdate:
        return None
    pbh = user_border_hourly(prev_raid, pdate)
    pbd = user_border_days(prev_raid)
    pf = {"b2000": _fin(pbd.get(2000, {})), "b100k": _fin(pbd.get(100000, {}))}
    ph = {x["day_of"]: x for x in hist if x["raid_number"] == prev_raid}
    pfp = round(ph[max(ph)]["point"] / 1e8, 1) if ph else None

    def latest(m):
        return next((t for t in reversed(times) if m.get(t) is not None), None)

    def bproj(key, tr):
        t = latest(cur[key])
        pv = pbh.get(tr, {}).get(t) if t else None
        return round(cur[key][t] * (pf[key] / pv), 1) if (t and pv and pf[key]) else None

    p2, p1 = bproj("b2000", 2000), bproj("b100k", 100000)
    tp = latest(cur["player"])
    pp = None
    if tp and pfp:
        r = find_user(prev_raid, pdate, tp, uid, hint=(ph.get(do) or {}).get("rank") or hint)
        if r and r[0]:
            pp = round(cur["player"][tp] * (pfp / r[0]), 1)
    return {"prev_raid": prev_raid, "time": tp, "player": pp, "b2000": p2, "b100k": p1,
            "vs2000": round(pp - p2, 1) if (pp is not None and p2 is not None) else None,
            "vs100k": round(pp - p1, 1) if (pp is not None and p1 is not None) else None}


def api_koran(q):
    raid = raid_arg(q) or meta_for()["raid"]
    query = (q.get("q", [""])[0] or "").strip()
    if not query:
        return {"error": "プレイヤー名 または ユーザーIDを入力してください"}
    uid, pname = None, None
    if re.fullmatch(r"\d{4,10}", query):
        uid = int(query)
    else:
        cands = user_search(query)
        if not cands:
            return {"error": f"「{query}」が見つかりません。名前を正確に入力するか、ユーザーIDで指定してください"}
        if len(cands) > 1:
            return {"candidates": [{"user_id": c["user_id"], "name": c.get("name"),
                                    "rank": (c.get("ranking") or {}).get("rank"),
                                    "point": round(((c.get("ranking") or {}).get("point") or 0) / 1e8, 1)}
                                   for c in cands[:30]]}
        uid, pname = cands[0]["user_id"], cands[0].get("name")

    hist = user_histories(uid)
    ev = {r["day_of"]: r for r in hist if r["raid_number"] == raid}
    if pname is None:
        pname = (hist[0].get("name") if hist else None) or f"ID{uid}"

    # 時刻毎モード(対象日が指定された場合): その日の 本人 vs 2000位/10万位 を1H毎に
    day = (q.get("day", [""])[0] or "").strip()
    if day:
        sched = {s["day"]: s["day_of"] for s in meta_for(raid)["schedules"]}
        do = sched.get(day)
        hint = (ev.get(do) or {}).get("rank") or 3000
        h = koran_hourly(raid, day, uid, hint)
        if not h["times"]:
            return {"error": "この日の時刻毎データはgbfdataに未収録です"}
        cur_h = {"player": h["player"]["cum"], "b2000": h["b2000"]["cum"], "b100k": h["b100k"]["cum"]}
        h["proj"] = koran_time_proj(raid, do, uid, hint, cur_h, h["times"], hist)
        h["past3"] = koran_past3(uid, raid, hist)
        h.update({"mode": "hourly", "name": pname, "user_id": uid, "raid": raid, "date": day,
                  "label": KORAN_LABELS.get(do, "")})
        return h

    borders = user_border_days(raid)
    b2000, b100k = borders.get(2000, {}), borders.get(100000, {})
    if not ev and not (b2000 or b100k):
        return {"error": "この回の個人データはgbfdataに未収録です（古い開催回では個人の記録が残っていません）"}

    cur_player = {do: round(ev[do]["point"] / 1e8, 1) for do in ev}
    rows = []
    for do in sorted(set(ev) | set(b2000) | set(b100k)):
        pl = cur_player.get(do)
        v2, v1 = b2000.get(do), b100k.get(do)
        rows.append({"label": KORAN_LABELS.get(do, str(do)), "day_of": do,
                     "player": pl, "rank": ev[do]["rank"] if do in ev else None,
                     "b2000": v2, "b100k": v1,
                     "vs2000": round(pl - v2, 1) if (pl is not None and v2 is not None) else None,
                     "vs100k": round(pl - v1, 1) if (pl is not None and v1 is not None) else None})

    # 着地見込み: 現時点(各系列の最新day_of)の値 × (前回最終 ÷ 前回同day_of)
    prev_raid = raid - 1
    pborders = user_border_days(prev_raid)
    pev = {r["day_of"]: r for r in hist if r["raid_number"] == prev_raid}
    prev_player = {do: round(pev[do]["point"] / 1e8, 1) for do in pev}
    prev = {"player": prev_player, "b2000": pborders.get(2000, {}), "b100k": pborders.get(100000, {})}
    cur = {"player": cur_player, "b2000": b2000, "b100k": b100k}

    # 基準日は3系列で共通(本人の最新day_of。本人不参加ならボーダー最新)にして整合を取る
    anchor = max(cur_player) if cur_player else max(set(b2000) | set(b100k), default=None)

    def landing(key):
        c, p = cur[key], prev[key]
        if anchor is None or anchor not in c or not p or anchor not in p or not p[anchor]:
            return None
        pfin = p[max(p)]                # 前回最終
        return round(c[anchor] * (pfin / p[anchor]), 1)

    lp, l2, l1 = landing("player"), landing("b2000"), landing("b100k")
    proj = {"prev_raid": prev_raid, "player": lp, "b2000": l2, "b100k": l1,
            "vs2000": round(lp - l2, 1) if (lp is not None and l2 is not None) else None,
            "vs100k": round(lp - l1, 1) if (lp is not None and l1 is not None) else None,
            "day_of": anchor, "label": KORAN_LABELS.get(anchor, "") if anchor else ""}
    return {"name": pname, "user_id": uid, "url": f"https://gbfdata.com/user/{uid}",
            "raid": raid, "rows": rows, "latest": rows[-1] if rows else None, "proj": proj,
            "past3": koran_past3(uid, raid, hist)}


ROUTES = {"/api/config": api_config, "/api/live": api_live,
          "/api/scout": api_scout, "/api/yosen": api_yosen, "/api/koran": api_koran}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 の持続的接続にする(Render等のプロキシが接続を再利用しても
    # no-server にならないように)。全レスポンスで Content-Length を送ること。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ROUTES:
            try:
                body = json.dumps(ROUTES[parsed.path](urllib.parse.parse_qs(parsed.query)),
                                  ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # static
        path = "/index.html" if parsed.path == "/" else parsed.path
        f = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if f.startswith(STATIC) and os.path.isfile(f):
            ctype = "text/html; charset=utf-8" if f.endswith(".html") else "application/octet-stream"
            data = open(f, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


if __name__ == "__main__":
    print(f"グラブル古戦場サポート  →  http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
