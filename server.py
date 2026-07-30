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
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8930))   # Render等のPaaSはPORT環境変数を渡す
BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
GBF = "https://gbfdata.com/api"
OURS_NAME = "霞桜団"
OURS_GID = 1147615
OURS_UID = 3052899        # 個ランの時刻毎を先読みする本人(Laphis)
OPP_FILE = "/Applications/gbf/honsen_opponent.txt"
HOURS = [f"{h:02d}:00" for h in range(8, 24)] + ["24:00"]

# 団員(霞桜団)のGN/ユーザーID。個ランでの名前検索(users/search)を省いて即取得できる
# 出典: 団員貢献度DBスプレッドシート(2026-07-30時点の30名)
MEMBERS = [
    ("Laphis", 3052899), ("明蓮", 2090466), ("ibls", 2722700),
    ("ノイン", 8884374), ("ジーク", 4148920), ("Yamato", 1240007),
    ("クラシック１", 3767386), ("えりゅ", 10408332), ("ぎのこ", 3136402),
    ("とくこ", 4984108), ("Ryan", 10219632), ("きぃ", 929001),
    ("はりま", 204160), ("幼女新撰組", 9727079), ("nao", 12607435),
    ("藤城真香", 27769761), ("さくらえび", 7628906), ("カノン", 16101483),
    ("だよだよめん", 6710599), ("すなっ", 3557547), ("ジータ", 34524668),
    ("D.N", 2569839), ("ルルフォン", 1490546), ("ねろそーす", 3740709),
    ("紅蘭", 16483149), ("皇杞枢", 22967234), ("しびー", 23600641),
    ("風色幻想", 25557296), ("レン", 12275867), ("じゅりこ", 38776098),
]

_cache = {}
_cache_lock = threading.Lock()
# エントリ数上限(超えたら古い順に捨てる)。1件およそ110KB なので上限×110KB が概ねの上限メモリ。
# 時刻毎モード1回分(約18時刻×数ページ)を保持できないと2回目もキャッシュが効かない
CACHE_MAX = 1200
# 先読みで温めたURL。他の団員を何人も検索しても本人ぶんが押し出されないよう削除対象から外す
_pinned = set()
PIN_MAX = 600
_pin_on = False


def _cache_put(url, now, data):
    """保存時に上限を超えたら古い順に削除(メモリ肥大の防止)。
    先読み済み(_pinned)のURLは残して、本人の時刻毎が常にキャッシュ命中で返るようにする"""
    with _cache_lock:
        _cache[url] = (now, data)
        if _pin_on and len(_pinned) < PIN_MAX:
            _pinned.add(url)
        if len(_cache) > CACHE_MAX:
            over = len(_cache) - CACHE_MAX
            old = sorted(_cache, key=lambda k: _cache[k][0])
            for k in (k for k in old if k not in _pinned):
                _cache.pop(k, None)
                over -= 1
                if over <= 0:
                    break


def get(url, ttl=180, slim=None):
    """GET with in-memory TTL cache.
    slim: レスポンスを保存前に間引く関数(巨大なランキングJSONをそのまま持たない)"""
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
    if slim:
        try:
            data = slim(data)
        except Exception:
            pass
    _cache_put(url, now, data)
    return data


def gbf_today():
    """古戦場の「今日」(JST・5時未満はまだ前日扱い)"""
    now = datetime.now(timezone(timedelta(hours=9)))
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.date().isoformat()


def day_ttl(date):
    """確定した過去日のランキングは変化しないので長期キャッシュ(2時間)。当日は短め"""
    return 7200 if (date and date < gbf_today()) else 180


def _slim_guilds(d):
    """キャッシュ保存前に必要項目だけ残す(1件38KB→大幅圧縮)"""
    return {"data": [{"guild_id": x.get("guild_id"), "name": x.get("name"),
                      "point": x.get("point"), "rank": x.get("rank")}
                     for x in (d or {}).get("data") or []],
            "snapshots": (d or {}).get("snapshots") or []}


def rankings_page(raid, date, rank, time_=None, per_page=50):
    q = {"raid_number": raid, "day": date, "rank": max(1, rank), "per_page": per_page}
    if time_:
        q["time"] = time_
    d = get(f"{GBF}/guilds/rankings?" + urllib.parse.urlencode(q),
            ttl=day_ttl(date), slim=_slim_guilds)
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
            "raids": raids, "schedules": m["schedules"],
            "members": [{"name": n, "uid": u} for n, u in MEMBERS]}


def ratio_winprob(r, k=3.5):
    """貢献度比から勝率(0〜1)。比の3.5乗のロジスティックで、格差が大きいほど極端に振る。
    r=1→50%, 1.2→65%, 1.5→81%, 0.8→31%, 0.5→8%, 0.13(団1位級)→1%未満"""
    if not r or r <= 0:
        return 0.01
    try:
        p = r ** k
    except OverflowError:
        return 0.99
    return min(0.99, max(0.01, p / (1 + p)))


BANDS = [("朝", 8, 11), ("昼", 12, 16), ("夕", 17, 20), ("夜", 21, 24)]


def time_pattern(cum):
    """時刻毎累積(億)から時間帯配分と型を判定。
    朝型=朝に集中 / 夜型=夕夜に集中 / 終盤型=夜だけ跳ねる / 持久型=一日中平坦"""
    sp, prev = {}, 0
    for t in HOURS:
        v = cum.get(t)
        if v is not None:
            sp[t] = v - prev
            prev = v
    if not sp:
        return None
    band = {b[0]: 0.0 for b in BANDS}
    for t, v in sp.items():
        hh = int(t.split(":")[0])
        for name, lo, hi in BANDS:
            if lo <= hh <= hi:
                band[name] += max(0.0, v)
                break
    total = sum(band.values())
    if total <= 0:
        return None
    pct = {k: v / total * 100 for k, v in band.items()}
    # 4分割なので均等=25%。そこからの偏りで判定する
    if pct["夜"] >= 32:
        label, desc = "終盤型", "夜(21〜24時)に一気に伸ばす"
    elif pct["朝"] >= 33:
        label, desc = "朝型", "朝(8〜11時)に稼いで先行する"
    elif pct["夕"] + pct["夜"] >= 52:
        label, desc = "夜型", "夕方以降に追い上げる"
    elif max(pct.values()) - min(pct.values()) <= 12:
        label, desc = "持久型", "時速は控えめだが一日中走り続ける"
    elif pct["昼"] >= 33:
        label, desc = "日中型", "日中(12〜16時)に安定して走る"
    else:
        label, desc = "標準型", "特定の時間帯に偏りが少ない"
    return {"label": label, "desc": desc, "total": round(total, 1),
            "pct": {k: round(v) for k, v in pct.items()},
            "rest_ratio": {k: round(v / 100, 3) for k, v in pct.items()}}


def remaining_share(pat, hk):
    """パターン上、現時刻hk以降に残っている割合(0〜1)"""
    if not pat:
        return None
    hh = int(hk.split(":")[0])
    rest = 0.0
    for name, lo, hi in BANDS:
        if hi <= hh:
            continue
        share = pat["rest_ratio"][name]
        if lo > hh:
            rest += share                       # まるごと未消化
        else:
            rest += share * (hi - hh) / (hi - lo + 1)   # 帯の途中
    return round(min(1.0, max(0.0, rest)), 3)


def battle_advice(cur_do, win, o_pat=None, p_pat=None, lead=None, hk=None, proj_lead=None):
    """本戦の日・勝敗見込み・両団の時間帯パターンから推奨アクション。
    Day1/2は翌日マッチングが前日貢献度で決まるため、決着後は抑えるほど有利。"""
    if not (4 <= cur_do <= 7):
        return None
    # パターンの読み(相手が後半型なら、リードがあっても警戒が要る)
    note = ""
    risk = False
    if p_pat:
        note = f"相手は{p_pat['label']}（{p_pat['desc']}／朝{p_pat['pct']['朝']}% 昼{p_pat['pct']['昼']}% 夕{p_pat['pct']['夕']}% 夜{p_pat['pct']['夜']}%）。"
        rest = remaining_share(p_pat, hk) if hk else None
        if rest is not None and p_pat["label"] in ("夜型", "終盤型") and rest >= 0.25:
            risk = True
            note += f"残り時間に相手の約{round(rest*100)}%が控えています。"
    if o_pat:
        note += f"自団は{o_pat['label']}。"
    if proj_lead is not None:
        note += f"このままの型なら最終差は約{proj_lead:+.0f}億の見込み。"

    if cur_do in (4, 5):
        nxt = f"本戦{cur_do - 2}日目"
        if win >= 85 and not risk:
            return {"label": "抑え推奨", "tone": "good",
                    "text": f"{note}勝勢が固まりました。ここから流せば{nxt}のマッチングが楽になり、グラッジ・半汁・体力も温存できます"}
        if win >= 85 and risk:
            return {"label": "リード維持", "tone": "mid",
                    "text": f"{note}数字上は優勢ですが、相手の追い上げ余地が大きい時間帯です。差が詰まらない程度に維持し、決着後に抑えると{nxt}が楽になります"}
        if win <= 15:
            return {"label": "撤退推奨", "tone": "good",
                    "text": f"{note}逆転は困難。早めに切り上げれば{nxt}のマッチングが有利になり、戦力も残せます"}
        return {"label": "継続", "tone": "mid", "text": f"{note}接戦。取れる試合なので押し切りましょう"}
    if cur_do == 6:
        if win >= 85 and not risk:
            return {"label": "Day4に温存", "tone": "good",
                    "text": f"{note}勝ちが見えました。余力は本戦4日目に回すと最終日の勝率が上がります"}
        if win >= 85 and risk:
            return {"label": "リード維持", "tone": "mid",
                    "text": f"{note}相手の伸びしろが残っています。振り切るまでは維持し、決着後にDay4へ余力を回しましょう"}
        if win <= 15:
            return {"label": "撤退推奨", "tone": "good",
                    "text": f"{note}逆転困難。本戦4日目に戦力を残しましょう"}
        return {"label": "継続", "tone": "mid", "text": f"{note}接戦。ここは取りに行く場面"}
    if win >= 85 and risk:
        return {"label": "最終日・振り切る", "tone": "mid",
                "text": f"{note}最終日。相手の追い上げ時間帯が残っているので、差を詰めさせないよう走り切りましょう"}
    return {"label": "最終日・全力", "tone": "mid",
            "text": f"{note}最終日。翌日を考える必要はないので、出せる分は出し切りましょう"}


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
    day_label = {s["day"]: f"本戦{s['day_of'] - 3}日目" for s in battle}
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
    # 予選も当日分(1日目/2日目)で表示。本戦は today_point がそのまま当日分
    ylbl = {1: "予選1日目", 2: "予選2日目", 3: "インターバル"}
    ref = []
    for do in (1, 2, 3):
        if do in o_ev or do in p_ev:
            ref.append({"label": ylbl[do],
                        "ours": round(o_ev.get(do, 0), 1),
                        "opp": round(p_ev[do], 1) if do in p_ev else None,
                        "ours_rank": o_rank.get(do), "opp_rank": p_rank.get(do)})
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
            prior = ratio_winprob(oa / pa)

        # 当日予測の確率化(残り時間が多いほど不確実性大) → 経過に応じて事前確率とブレンド
        sigma = max(25.0, (fo + fp) / 2 * 0.06 + (fo + fp) / 2 * 0.30 * remain / len(HOURS))
        p_proj = 1 / (1 + math.exp(-(fo - fp) / sigma))
        w = elapsed / len(HOURS)
        win = round(100 * ((1 - w) * prior + w * p_proj))
        win = max(1, min(99, win))
        # 時間帯パターン: 前日の実績があればそれを、無ければ当日の推移から判定
        o_pat = time_pattern(prev_day["ours"]["cum"]) if prev_day else None
        p_pat = time_pattern(prev_day["opp"]["cum"]) if prev_day else None
        if not o_pat:
            o_pat = time_pattern(ours)
        if not p_pat:
            p_pat = time_pattern(opp)
        forecast = {"win": win, "proj_ours": round(fo, 1), "proj_opp": round(fp, 1),
                    "policy": policy, "prior": round(prior * 100),
                    "basis": "前日推移ベース" if prev_day else "平均時速ベース",
                    "ours_pattern": o_pat, "opp_pattern": p_pat,
                    "advice": battle_advice(cur_do, win, o_pat, p_pat,
                                            round(o_now - p_now, 1), hk, round(fo - fp, 1))}

    # 過去開催の総合順位推移(最終day_ofのrank)
    # 直近3開催の 予選→本戦1〜4 の総合順位推移
    RK_DAYS = [(2, "予選"), (4, "本1"), (5, "本2"), (6, "本3"), (7, "本4")]
    rk3 = sorted({x["raid_number"] for x in ours_hist} | {x["raid_number"] for x in opp_hist},
                 reverse=True)[:3][::-1]

    def rank_at(rows):
        return {(x["raid_number"], x["day_of"]): x["rank"] for x in rows}
    o_at, p_at = rank_at(ours_hist), rank_at(opp_hist)
    rank_history = []
    for rn in rk3:
        for do, dl in RK_DAYS:
            o, p = o_at.get((rn, do)), p_at.get((rn, do))
            if o is None and p is None:
                continue
            rank_history.append({"label": f"{rn}回{dl}", "raid": rn, "day_of": do,
                                 "ours": o, "opp": p})

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

    ours_rows = guild_histories(OURS_GID)

    def event_summary(r):
        ev = {x["day_of"]: x for x in rows if x["raid_number"] == r}
        if not ev:
            return None
        last = ev[max(ev)]
        oev = {x["day_of"]: x for x in ours_rows if x["raid_number"] == r}
        olast = oev[max(oev)] if oev else None
        daily = {do: round(ev[do]["today_point"] / 1e8, 1) for do in sorted(ev)}
        odaily = {do: round(oev[do]["today_point"] / 1e8, 1) for do in sorted(oev)}
        return {"raid": r, "daily": daily,
                "total": round(last["point"] / 1e8, 1), "final_rank": last["rank"],
                "ours_total": round(olast["point"] / 1e8, 1) if olast else None,
                "ours_rank": olast["rank"] if olast else None,
                # 本戦(day_of 4〜7)のみの貢献度
                "honsen": round(sum(v for do, v in daily.items() if do >= 4), 1),
                "ours_honsen": round(sum(v for do, v in odaily.items() if do >= 4), 1) if odaily else None,
                "honsen_days": {do: daily.get(do) for do in range(4, 8) if do in daily},
                "ours_honsen_days": {do: odaily.get(do) for do in range(4, 8) if do in odaily}}

    raids = sorted({x["raid_number"] for x in rows}, reverse=True)
    events = [e for e in (event_summary(r) for r in raids[:6]) if e]

    # 勝率の基準: 直近3開催(現開催含む・両団に本戦データがある回)の本戦日毎平均
    h_raids = sorted({x["raid_number"] for x in rows if x["day_of"] >= 4}
                     & {x["raid_number"] for x in ours_rows if x["day_of"] >= 4},
                     reverse=True)[:3]
    pv = [x["today_point"] / 1e8 for x in rows
          if x["raid_number"] in h_raids and x["day_of"] >= 4]
    past_avg = round(sum(pv) / len(pv), 1) if pv else None

    # 自団との比較
    ours_ev = {x["day_of"]: x for x in ours_rows if x["raid_number"] == raid}
    opp_ev = {x["day_of"]: x for x in rows if x["raid_number"] == raid}
    compare = []
    lbl = {1: "予選1日目", 2: "予選2日目", 3: "インターバル", 4: "本戦1日目", 5: "本戦2日目", 6: "本戦3日目", 7: "本戦4日目"}
    for do in sorted(set(ours_ev) | set(opp_ev)):
        compare.append({"label": lbl.get(do, str(do)),
                        "ours": round(ours_ev[do]["today_point"] / 1e8, 1) if do in ours_ev else None,
                        "opp": round(opp_ev[do]["today_point"] / 1e8, 1) if do in opp_ev else None})

    ours_pv = [x["today_point"] / 1e8 for x in ours_rows
               if x["raid_number"] in h_raids and x["day_of"] >= 4]
    ours_avg = round(sum(ours_pv) / len(ours_pv), 1) if ours_pv else None
    winrate = None
    if past_avg and ours_avg:
        winrate = round(100 * ratio_winprob(ours_avg / past_avg))

    # 本戦中なら「前日」の両団貢献度(マッチング基準の日)
    prev_day = None
    if 4 <= cur_do <= 7:
        pd = cur_do - 1 if cur_do > 4 else None      # 本戦2日目以降は前日=本戦の前日
        if pd and (pd in ours_ev or pd in opp_ev):
            o = round(ours_ev[pd]["today_point"] / 1e8, 1) if pd in ours_ev else None
            p = round(opp_ev[pd]["today_point"] / 1e8, 1) if pd in opp_ev else None
            prev_day = {"label": lbl.get(pd, str(pd)), "ours": o, "opp": p,
                        "diff": round(o - p, 1) if (o is not None and p is not None) else None}
        elif cur_do == 4:                             # 本戦1日目の前日相当=予選合計
            o = round(sum(ours_ev[d]["today_point"] for d in (1, 2) if d in ours_ev) / 1e8, 1)
            p = round(sum(opp_ev[d]["today_point"] for d in (1, 2) if d in opp_ev) / 1e8, 1) if opp_ev else None
            prev_day = {"label": "予選(計)", "ours": o, "opp": p,
                        "diff": round(o - p, 1) if p is not None else None}

    # 直近3開催の 予選→本戦1〜4 の総合順位推移(ライブと同じ粒度)
    RK_DAYS = [(2, "予選"), (4, "本1"), (5, "本2"), (6, "本3"), (7, "本4")]
    rk3 = sorted({x["raid_number"] for x in ours_rows} | {x["raid_number"] for x in rows},
                 reverse=True)[:3][::-1]
    o_at = {(x["raid_number"], x["day_of"]): x["rank"] for x in ours_rows}
    p_at = {(x["raid_number"], x["day_of"]): x["rank"] for x in rows}
    rank_history = []
    for rn in rk3:
        for do, dl in RK_DAYS:
            o, p = o_at.get((rn, do)), p_at.get((rn, do))
            if o is None and p is None:
                continue
            rank_history.append({"label": f"{rn}回{dl}", "ours": o, "opp": p})

    return {"name": gname, "gid": gid, "url": f"https://game.granbluefantasy.jp/#guild/detail/{gid}",
            "events": events, "past_avg": past_avg, "ours_avg": ours_avg,
            "winrate": winrate, "compare": compare, "cur_do": cur_do, "prev_day": prev_day,
            "ours_name": OURS_NAME, "rank_history": rank_history}


def api_scout_speed(q):
    """サーチの時速分析: 本戦各日の 最高時速/平均時速 を両団ぶん(重いので別API)"""
    raid = raid_arg(q) or meta_for()["raid"]
    v = (q.get("gid", [""])[0] or "").strip()
    if not v.isdigit():
        return {"error": "団IDが必要です"}
    gid = int(v)
    m = meta_for(raid)
    days = [(s["day_of"], s["day"]) for s in sorted(m["schedules"], key=lambda s: s["day_of"])
            if s["day_of"] >= 4]
    ours_hist, opp_hist = guild_histories(OURS_GID), guild_histories(gid)

    def hint_of(rows, do, default):
        ev = {x["day_of"]: x for x in rows if x["raid_number"] == raid}
        return (ev.get(do) or ev.get(do - 1) or {}).get("rank") or default

    def stats(item):
        do, date, rows_, g, dflt = item
        ser = hourly_series(raid, date, day_base(rows_, raid, date), g, hint_of(rows_, do, dflt))
        sp = _speeds(ser)
        # 08:00は日始(前日終了からの差分でない)ので除外
        vals = [v for t, v in sp.items() if t != "08:00" and v is not None and v > 0]
        if not vals:
            return None
        peak = max(sp.items(), key=lambda kv: (kv[1] if kv[0] != "08:00" and kv[1] is not None else -1))
        return {"max": round(max(vals), 1), "avg": round(sum(vals) / len(vals), 1), "peak_time": peak[0]}

    jobs = []
    for do, date in days:
        jobs.append((do, date, ours_hist, OURS_GID, 250))
        jobs.append((do, date, opp_hist, gid, 400))
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(stats, jobs))
    out = []
    for i, (do, date) in enumerate(days):
        o, p = res[i * 2], res[i * 2 + 1]
        if o or p:
            out.append({"label": f"本戦{do - 3}", "day_of": do,
                        "ours_max": (o or {}).get("max"), "ours_avg": (o or {}).get("avg"),
                        "opp_max": (p or {}).get("max"), "opp_avg": (p or {}).get("avg")})

    # 前日(マッチング基準の日)の時刻毎比較。本戦1日目が対象なら前日=予選なので日別で返す
    sched = {s["day_of"]: s["day"] for s in m["schedules"]}
    cur_do = max((s["day_of"] for s in m["schedules"] if s["day"] in
                  {x["day"] for x in ours_hist if x["raid_number"] == raid}), default=7)
    prev = None
    pdo = cur_do - 1
    if 4 <= pdo <= 7 and sched.get(pdo):
        pdate = sched[pdo]

        def ser(rows_, g, dflt):
            return hourly_series(raid, pdate, day_base(rows_, raid, pdate), g, hint_of(rows_, pdo, dflt))
        with ThreadPoolExecutor(max_workers=2) as ex:
            fo = ex.submit(ser, ours_hist, OURS_GID, 250)
            fp = ex.submit(ser, opp_hist, gid, 400)
            o_ser, p_ser = fo.result(), fp.result()
        prev = {"mode": "hourly", "label": f"本戦{pdo - 3}日目", "times": HOURS,
                "ours": {"cum": o_ser, "speed": _speeds(o_ser)},
                "opp": {"cum": p_ser, "speed": _speeds(p_ser)}}
    elif pdo == 3 or cur_do == 4:
        # 前日=予選: 予選1日目/2日目の日別で比較
        oe = {x["day_of"]: x for x in ours_hist if x["raid_number"] == raid}
        pe = {x["day_of"]: x for x in opp_hist if x["raid_number"] == raid}
        rows_ = []
        for do, dl in ((1, "予選1日目"), (2, "予選2日目"), (3, "インターバル")):
            if do in oe or do in pe:
                rows_.append({"label": dl,
                              "ours": round(oe[do]["today_point"] / 1e8, 1) if do in oe else None,
                              "opp": round(pe[do]["today_point"] / 1e8, 1) if do in pe else None})
        if rows_:
            prev = {"mode": "daily", "label": "予選", "rows": rows_}
    return {"raid": raid, "days": out, "ours_name": OURS_NAME, "prev": prev}


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
            labels.append((key, f"{int(t.split(':')[0])}時"))
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
KORAN_LABELS = {1: "予選1日目", 2: "予選2日目", 3: "インターバル", 4: "本戦1日目", 5: "本戦2日目", 6: "本戦3日目", 7: "本戦4日目"}


def user_search(q):
    d = get(f"{GBF}/users/search?q=" + urllib.parse.quote(q), ttl=300)
    return (d or {}).get("data") or []


def user_histories(uid, pages=6):
    rows = []
    for pg in range(1, pages + 1):
        d = get(f"{GBF}/users/{uid}/histories?page={pg}", ttl=1800)
        data = (d or {}).get("data") or []
        rows += data
        if not (d or {}).get("meta", {}).get("has_next"):
            break
    return rows


def border_ttl(raid):
    """個人ボーダー: 現開催は15分、終了した過去回は確定値なので6時間キャッシュ"""
    try:
        lt = meta_for().get("latest")
    except Exception:
        lt = None
    return 900 if (not lt or raid >= lt) else 21600


def user_border_days(raid):
    """個人ボーダー rank:2000/100000 の day_of別 日終了累積(億)。pointは通算累積。
    {target_rank: {day_of: 億}}"""
    d = get(f"{GBF}/users/borders?raid_number={raid}", ttl=border_ttl(raid))
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
    d = get(f"{GBF}/users/borders?raid_number={raid}", ttl=border_ttl(raid))
    out = {2000: {}, 100000: {}}
    for s in (d or {}).get("data") or []:
        tr = s.get("target_rank")
        if tr not in out:
            continue
        for pt in s.get("points") or []:
            if pt.get("day") == date and pt.get("time") and pt.get("point") is not None:
                out[tr][pt["time"]] = round(pt["point"] / 1e8, 1)
    return out


def _slim_users(d):
    """キャッシュ保存前に user_id → (点数, 順位, 時速) の辞書へ畳む。
    500件ぶんのdictを抱えずに済んでメモリが数分の一になり、find_user も走査せず直接引ける
    (このページに名前は不要なので捨てる)"""
    return {"m": {x["user_id"]: (x.get("point"), x.get("rank"), x.get("hourly_point"))
                  for x in (d or {}).get("data") or [] if x.get("user_id") is not None}}


PAGE = 500   # gbfdataが1リクエストで返せる最大件数(1000は不可)。500順位ぶんまとめて見る


def user_rankings_page(raid, date, rank, time_=None, per_page=PAGE):
    q = {"raid_number": raid, "day": date, "rank": max(1, rank), "per_page": per_page}
    if time_:
        q["time"] = time_
    d = get(f"{GBF}/users/rankings?" + urllib.parse.urlencode(q),
            ttl=day_ttl(date), slim=_slim_users)
    return (d or {}).get("m") or {}


def find_user(raid, date, time_, uid, hint=3000, max_pages=12):
    """個人rankingsから uid を探す(hint近傍→外側へ拡張)。(point億, rank, hourly_point) or None
    1ページ500順位ぶんなので max_pages=12 で hint の前後およそ±3000位をカバーする"""
    base = ((hint - 1) // PAGE) * PAGE + 1
    order, tried = [base], set()
    for dd in range(PAGE, 40000, PAGE):
        order += [base + dd, base - dd]
    for s in order:
        if s < 1 or s > 300000 or s in tried:
            continue
        tried.add(s)
        hit = user_rankings_page(raid, date, s, time_).get(uid)
        if hit and hit[0] is not None and hit[1] is not None:
            return round(hit[0] / 1e8, 1), hit[1], hit[2]
        if len(tried) >= max_pages:
            break
    return None


def find_user_sweep(raid, date, time_, uid, max_rank=60000, chunk=24):
    """1位から順位帯を総なめして uid を探す(中位以下で順位が読めない時刻用)。
    find_user はページを1枚ずつ順に見るので枚数が多いと逐次通信で遅くなる。
    ここは chunk枚ずつ並列に見て、見つかった時点で打ち切る(無駄な通信を出さない)"""
    starts = range(1, max_rank + 1, PAGE)

    def probe(s):
        return user_rankings_page(raid, date, s, time_).get(uid)

    batch = []
    for s in starts:
        batch.append(s)
        if len(batch) < chunk:
            continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            for hit in ex.map(probe, batch):
                if hit and hit[0] is not None and hit[1] is not None:
                    return round(hit[0] / 1e8, 1), hit[1], hit[2]
        batch = []
    if batch:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for hit in ex.map(probe, batch):
                if hit and hit[0] is not None and hit[1] is not None:
                    return round(hit[0] / 1e8, 1), hit[1], hit[2]
    return None


def koran_hourly(raid, date, uid, hint=3000, hint_start=None, day_of=None,
                 base_cum=None, prev_date=None):
    """指定日の 本人 と 2000位/100000位 の時刻毎累積・時速(億)。
    2段階で探す: まず均等に5点(アンカー)だけ広めに探し、残りの時刻はその実測順位を
    前後から補間して狭い範囲だけ見る。全時刻を広く走査するより無駄が減り、並列も保てる。"""
    b = user_border_hourly(raid, date)
    b2000, b100k = b.get(2000, {}), b.get(100000, {})
    times = sorted(set(b2000) | set(b100k), key=lambda t: int(t.split(":")[0]))
    # 本戦(day_of 4〜7)は7〜24時が稼働時間。24時以降は貢献度に反映されないので走査しない
    if day_of and day_of >= 4:
        times = [t for t in times if int(t.split(":")[0]) <= 24]
    # 予選・インターバルは深夜も稼働するため、末尾はボーダーが動いたかで判定して落とす
    # (走査しても増分0が並ぶだけの時刻を削る)
    while len(times) > 1:
        t, pv = times[-1], times[-2]
        if any(x.get(t) is not None and x.get(pv) is not None and x[t] > x[pv]
               for x in (b2000, b100k)):
            break
        times.pop()
    n = max(1, len(times))
    p_cum, p_rank, found = {}, {}, {}      # found: 時刻index -> 実測順位

    def lerp_hint(i):
        """i番目の時刻の順位を見積もる。既知アンカーがあれば前後から補間、
        なければ hint_start(前日終了順位)→hint(当日終了順位) の線形補間"""
        lo = max((j for j in found if j <= i), default=None)
        hi = min((j for j in found if j >= i), default=None)
        if lo is not None and hi is not None:
            if lo == hi:
                return found[lo]
            return int(found[lo] + (found[hi] - found[lo]) * (i - lo) / (hi - lo))
        if lo is not None:
            return found[lo]
        if hi is not None:
            return found[hi]
        if hint_start:
            return int(hint_start + (hint - hint_start) * (i + 1) / n)
        return hint

    def scan(idxs, max_pages):
        def one(i):
            return i, find_user(raid, date, times[i], uid,
                                hint=max(1, lerp_hint(i)), max_pages=max_pages)
        with ThreadPoolExecutor(max_workers=8) as ex:
            return list(ex.map(one, idxs))

    def sweep(idxs):
        """総なめは内部でも並列化するので、外側は控えめ(同時接続を増やしすぎない)"""
        def one(i):
            return i, find_user_sweep(raid, date, times[i], uid)
        with ThreadPoolExecutor(max_workers=3) as ex:
            return list(ex.map(one, idxs))

    def take(results):
        for i, r in results:
            if r:
                found[i] = r[1]
                p_cum[times[i]], p_rank[times[i]] = r[0], r[1]

    anchors = sorted({0, n - 1} | {round(n * k / 4) for k in (1, 2, 3)})
    take(scan([i for i in anchors if 0 <= i < n], 14))          # ①アンカーは広めに
    rest = [i for i in range(n) if i not in found]
    if rest:
        take(scan(rest, 8))                        # ②近傍が近いぶんはこれで当たる
    # ③残りは1位から総なめする。順位は1時間で数万位動くことがあり(急に伸ばすと順位が
    #   大幅に上がり、その後は他人に抜かれて下がっていく)、近傍からの補間では原理的に
    #   届かない。ここに来るのは中位以下の団員のみ。
    #   全時刻を総なめすると遅いので、まず数点だけ総なめして足場を作り、間は補間で埋める
    #   (足場どうしの間では順位がなめらかに動くので補間が効く)
    rest = [i for i in range(n) if i not in found]
    if rest:
        take(sweep(rest[::max(1, len(rest) // 4)]))
        rest = [i for i in range(n) if i not in found]
        if rest:
            take(scan(rest, 20))
        rest = [i for i in range(n) if i not in found]
        if rest:                                   # 最後の取り残しだけ総なめ
            take(sweep(rest))

    # 取れなかった時刻は「その1時間は稼ぎ0」として直前の値を引き継ぐ。
    # 実測がまだ無い先頭は base_cum(前日終了時点の累積。初日は0)で埋めて「—」を出さない
    last_c, last_r = base_cum, None
    for t in times:
        if t in p_cum:
            last_c = p_cum[t]
        elif last_c is not None:
            p_cum[t] = last_c
        if p_rank.get(t) is not None:      # 順位は累積とは別に引き継ぐ
            last_r = p_rank[t]
        elif last_r is not None:
            p_rank[t] = last_r

    def speed(cum, prev0):
        """prev0 = 前日の最終値。これを「1つ前の時刻」として使うことで初回時刻の時速も出る。
        本戦は24時→翌7時に稼ぎが無いので0.0、予選2日目は30時→7時の1時間ぶんが出る。
        初日(day_of=1)はイベント開始前=0からの増分なので累積そのものが時速になる。"""
        sp, prev = {}, prev0
        for t in times:
            if t in cum:
                sp[t] = round(cum[t] - prev, 1) if prev is not None else 0.0
                prev = cum[t]
        return sp

    if day_of == 1 or not prev_date:       # イベント初日は0からの増分
        pv2 = pv1 = 0.0
        pvp = 0.0
    else:
        pb = user_border_hourly(raid, prev_date)
        pv2, pv1 = _fin(pb.get(2000, {})), _fin(pb.get(100000, {}))
        pvp = base_cum
    return {"times": times, "labels": [f"{int(t.split(':')[0])}時" for t in times],
            "player": {"cum": p_cum, "rank": p_rank, "speed": speed(p_cum, pvp)},
            "b2000": {"cum": b2000, "speed": speed(b2000, pv2)},
            "b100k": {"cum": b100k, "speed": speed(b100k, pv1)}}


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
        # 団員名なら登録済みIDで即解決(users/search を省いて高速化)
        hit = next((u for n, u in MEMBERS if n == query), None) \
            or next((u for n, u in MEMBERS if n.lower() == query.lower()), None)
        if hit:
            uid, pname = hit, next(n for n, u in MEMBERS if u == hit)
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

    # この回が確定(本戦終了)済みか。新しい回が始まっている or 最終日を過ぎていれば確定
    _m = meta_for(raid)
    _last = max((s["day"] for s in _m["schedules"]), default=None)
    _today = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
    confirmed = (raid < (_m.get("latest") or raid)) or bool(_last and _today > _last)

    day = (q.get("day", [""])[0] or "").strip()

    # 時刻毎モード(対象日が指定された場合): その日の 本人 vs 2000位/10万位 を1H毎に
    # 日程に無い値(廃止した day=all の古いブックマーク等)は概要(日別)にフォールバック
    _sc = meta_for(raid)["schedules"]
    if day and day in {s["day"] for s in _sc}:
        sched = {s["day"]: s["day_of"] for s in _sc}
        do = sched.get(day)
        hint = (ev.get(do) or {}).get("rank") or 3000
        _pp = (ev.get(do - 1) or {}).get("point")   # 前日終了時点の累積(初日は0)
        _pd = next((s["day"] for s in _sc if s["day_of"] == (do or 0) - 1), None)
        h = koran_hourly(raid, day, uid, hint,
                         hint_start=(ev.get(do - 1) or {}).get("rank"), day_of=do,
                         base_cum=(round(_pp / 1e8, 1) if _pp else 0.0), prev_date=_pd)
        if not h["times"]:
            return {"error": "この日の時刻毎データはgbfdataに未収録です"}
        cur_h = {"player": h["player"]["cum"], "b2000": h["b2000"]["cum"], "b100k": h["b100k"]["cum"]}
        h["proj"] = koran_time_proj(raid, do, uid, hint, cur_h, h["times"], hist)
        h["past3"] = koran_past3(uid, raid, hist)
        h.update({"mode": "hourly", "name": pname, "user_id": uid, "raid": raid, "date": day,
                  "label": KORAN_LABELS.get(do, ""), "confirmed": confirmed})
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
            "past3": koran_past3(uid, raid, hist), "confirmed": confirmed}




def _prewarm_once(m, raid, members):
    """先読み1回分。members に渡した団員の履歴だけ取得する"""
    oh = guild_histories(OURS_GID)                     # ①自団の日別実績
    get(f"{GBF}/users/borders?raid_number={raid}", ttl=900)   # ②英雄(2000位)/10万位ボーダー(全期間の時刻毎を含む)
    # ③個ランの時刻毎(1H)は走査量が多く開くと待たされるので、本人ぶんを最優先で温める
    #   (全員ぶん毎時やるとgbfdataへの負荷が高いので本人に限定)
    _prewarm_koran_day(m, raid, OURS_UID)
    for r in range(raid - 1, raid - 7, -1):            # 直近6回の着地(過去回は確定値なので長期キャッシュ)
        if r > 0:
            get(f"{GBF}/users/borders?raid_number={r}", ttl=21600)
    for _, uid in members:                             # ④団員の個人履歴
        user_histories(uid)
    # ⑤開催中は当日の推移も(本戦=自団の時刻毎 / 予選=300位ボーダーと自団)
    today = gbf_today()
    do = {sc["day"]: sc["day_of"] for sc in m["schedules"]}.get(today)
    if do and do >= 4:
        hint = next((x["rank"] for x in oh
                     if x["raid_number"] == raid and x["day_of"] == do - 1), 250)
        hourly_series(raid, today, day_base(oh, raid, today), OURS_GID, hint)
    elif do in (1, 2):
        for t in _snapshot_times(raid, today):
            rankings_page(raid, today, 300, t, per_page=1)
            find_guild(raid, today, t, gid=OURS_GID, name=OURS_NAME, hint=120, max_pages=12)


def _prewarm_koran_day(m, raid, uid):
    """個ランの時刻毎(1H)で最初に開かれる日=当日(開催終了後は最終日)だけを先読みする。
    全期間モードを廃止したので7日ぶん温める必要はなく、gbfdataへの負荷も約1/7になる。
    api_koran と同じ経路を通すので、開いたときはキャッシュ命中で返る。
    この間に取得したURLは _pinned に入れ、他の団員を検索しても押し出されないようにする"""
    global _pin_on
    scs = sorted(m["schedules"], key=lambda s: s["day_of"])
    today = gbf_today()
    sc = next((s for s in scs if s["day"] == today), None) or (scs[-1] if scs else None)
    if not sc:
        return
    ev = {r["day_of"]: r for r in user_histories(uid) if r["raid_number"] == raid}
    do = sc["day_of"]
    hint = (ev.get(do) or ev.get(do - 1) or {}).get("rank") or 3000
    pp = (ev.get(do - 1) or {}).get("point")
    pd = next((s["day"] for s in scs if s["day_of"] == do - 1), None)
    with _cache_lock:
        _pinned.clear()          # 前回ぶんは作り直す(同じURLを取り直すので取りこぼさない)
    _pin_on = True
    try:
        koran_hourly(raid, sc["day"], uid, hint,
                     hint_start=(ev.get(do - 1) or {}).get("rank"), day_of=do,
                     base_cum=(round(pp / 1e8, 1) if pp else 0.0), prev_date=pd)
    finally:
        _pin_on = False


def prewarm_loop():
    """gbfdataは毎時更新。更新直後に主要データを先読みしてキャッシュに載せ、
    ユーザーが開いたときの待ち時間を減らす。
    起動直後に団員全員ぶんを温め、以降は毎時4分に6名ずつローテーション
    (30名を毎時取り直すとgbfdataへの負荷が高いため)。"""
    idx = 0
    try:                                               # 起動直後: 全員ぶんを1回
        m0 = meta_for()
        if m0.get("raid"):
            _prewarm_once(m0, m0["raid"], MEMBERS)
    except Exception:
        pass
    while True:
        try:
            now = datetime.now(timezone(timedelta(hours=9)))
            nxt = now.replace(minute=4, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(hours=1)
            time.sleep(max(30, (nxt - now).total_seconds()))
            m = meta_for()
            raid = m.get("raid")
            if not raid:
                continue
            batch = MEMBERS[idx:idx + 6] or MEMBERS[:6]
            idx = (idx + 6) % len(MEMBERS)
            _prewarm_once(m, raid, batch)
        except Exception:
            time.sleep(60)


ROUTES = {"/api/config": api_config, "/api/live": api_live,
          "/api/scout": api_scout, "/api/yosen": api_yosen, "/api/koran": api_koran,
          "/api/scout_speed": api_scout_speed}


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
    threading.Thread(target=prewarm_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
