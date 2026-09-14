# -*- coding: utf-8 -*-
"""
KIS -> GitHub Issue live scanner
Runs entirely in GitHub Actions. No local Python required.
Updates one GitHub issue body with the current leader table.
"""
import os, time, json, math, requests
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
REST_URL = "https://openapi.koreainvestment.com:9443"
GH_API = "https://api.github.com"

APP_KEY = os.environ["KIS_APP_KEY"]
APP_SECRET = os.environ["KIS_APP_SECRET"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]

SCAN_SEC = int(os.getenv("SCAN_SEC", "20"))
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "130"))
TOP_N = int(os.getenv("TOP_N", "10"))
ISSUE_TITLE = "KIS LIVE LEADERS"

s = requests.Session()
access_token = None
token_exp = 0

def num(x, d=0.0):
    try: return float(str(x).replace(",",""))
    except: return d

def get_token():
    global access_token, token_exp
    if access_token and time.time() < token_exp - 120:
        return access_token
    r = s.post(REST_URL + "/oauth2/tokenP", json={
        "grant_type":"client_credentials",
        "appkey":APP_KEY,
        "appsecret":APP_SECRET
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    access_token = d["access_token"]
    token_exp = time.time() + int(d.get("expires_in", 86400))
    return access_token

def kis_get(path, tr_id, params):
    r = s.get(REST_URL+path, headers={
        "content-type":"application/json; charset=utf-8",
        "authorization":f"Bearer {get_token()}",
        "appkey":APP_KEY,
        "appsecret":APP_SECRET,
        "tr_id":tr_id,
        "custtype":"P"
    }, params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("rt_cd") not in (None,"0"):
        raise RuntimeError(f"{d.get('msg_cd')} {d.get('msg1')}")
    return d.get("output") or d.get("Output") or []

def fluctuation(m):
    return kis_get("/uapi/domestic-stock/v1/ranking/fluctuation","FHPST01700000",{
        "fid_rsfl_rate2":"","fid_cond_mrkt_div_code":m,"fid_cond_scr_div_code":"20170",
        "fid_input_iscd":"0000","fid_rank_sort_cls_code":"0","fid_input_cnt_1":"0",
        "fid_prc_cls_code":"1","fid_input_price_1":"","fid_input_price_2":"",
        "fid_vol_cnt":"","fid_trgt_cls_code":"0","fid_trgt_exls_cls_code":"0",
        "fid_div_cls_code":"1","fid_rsfl_rate1":""
    })

def money(m):
    return kis_get("/uapi/domestic-stock/v1/quotations/volume-rank","FHPST01710000",{
        "FID_COND_MRKT_DIV_CODE":m,"FID_COND_SCR_DIV_CODE":"20171","FID_INPUT_ISCD":"0000",
        "FID_DIV_CLS_CODE":"1","FID_BLNG_CLS_CODE":"3","FID_TRGT_CLS_CODE":"0",
        "FID_TRGT_EXLS_CLS_CODE":"0000000000","FID_INPUT_PRICE_1":"","FID_INPUT_PRICE_2":"",
        "FID_VOL_CNT":""
    })

def power(m):
    return kis_get("/uapi/domestic-stock/v1/ranking/volume-power","FHPST01680000",{
        "fid_trgt_exls_cls_code":"0","fid_cond_mrkt_div_code":m,"fid_cond_scr_div_code":"20168",
        "fid_input_iscd":"0000","fid_div_cls_code":"1","fid_input_price_1":"",
        "fid_input_price_2":"","fid_vol_cnt":"","fid_trgt_cls_code":"0"
    })

def code_of(r):
    return str(r.get("stck_shrn_iscd") or r.get("mksc_shrn_iscd") or "").strip()

def points(rank, weight):
    return 0 if not rank else weight*max(0,(31-rank)/30)

def scan():
    rows={}
    for market in ("J","NX"):
        for src, fn in (("rise",fluctuation),("money",money),("power",power)):
            try:
                data=fn(market)
            except Exception as e:
                print("WARN",market,src,e)
                continue
            for rank,r in enumerate(data,1):
                c=code_of(r)
                if not c: continue
                x=rows.setdefault(c,{
                    "code":c,"name":"","price":0.0,"pct":0.0,"power":0.0,
                    "volume":0.0,"amount":0.0,"markets":set(),"ranks":{}
                })
                x["name"]=str(r.get("hts_kor_isnm") or x["name"])
                x["price"]=num(r.get("stck_prpr"),x["price"])
                x["pct"]=num(r.get("prdy_ctrt"),x["pct"])
                x["power"]=max(x["power"],num(r.get("tday_rltv")))
                x["volume"]=max(x["volume"],num(r.get("acml_vol")))
                x["amount"]=max(x["amount"],num(r.get("acml_tr_pbmn")))
                x["markets"].add("KRX" if market=="J" else "NXT")
                x["ranks"][src]=rank

    for x in rows.values():
        score=points(x["ranks"].get("money"),45)+points(x["ranks"].get("power"),30)+points(x["ranks"].get("rise"),20)
        if x["pct"]>0: score += min(12,x["pct"]*1.3)
        if x["power"]>100: score += min(12,(x["power"]-100)/8)
        if "money" not in x["ranks"]: score -= 10
        if "rise" in x["ranks"] and "money" not in x["ranks"] and "power" not in x["ranks"]: score -= 12
        x["score"]=round(score,1)

    return sorted(rows.values(),key=lambda z:z["score"],reverse=True)[:TOP_N]

def gh_headers():
    return {
        "Authorization":f"Bearer {GH_TOKEN}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28"
    }

def ensure_issue():
    r=requests.get(f"{GH_API}/repos/{REPO}/issues",headers=gh_headers(),
                   params={"state":"open","per_page":100},timeout=15)
    r.raise_for_status()
    for item in r.json():
        if item.get("title")==ISSUE_TITLE and "pull_request" not in item:
            return item["number"]
    r=requests.post(f"{GH_API}/repos/{REPO}/issues",headers=gh_headers(),
                    json={"title":ISSUE_TITLE,"body":"Starting scanner..."},timeout=15)
    r.raise_for_status()
    return r.json()["number"]

def issue_body(leaders, err=None):
    now=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    if err:
        return f"# KIS LIVE LEADERS\n\n**Updated:** {now}\n\n⚠️ `{err}`"
    lines=[
        "# KIS LIVE LEADERS",
        "",
        f"**Updated:** {now}",
        "",
        "|#|종목|코드|현재가|등락률|체결강도|상승R|대금R|체결R|시장|점수|",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|---:|"
    ]
    for i,x in enumerate(leaders,1):
        r=x["ranks"]
        lines.append(
            f"|{i}|{x['name']}|{x['code']}|{x['price']:,.0f}|{x['pct']:+.2f}%|"
            f"{x['power']:.1f}|{r.get('rise','-')}|{r.get('money','-')}|{r.get('power','-')}|"
            f"{'/'.join(sorted(x['markets']))}|{x['score']:.1f}|"
        )
    lines += [
        "",
        "> 거래대금 > 체결강도 > 상승률 가중. 많이 올랐다는 이유만으로 제외하지 않음.",
        "> 조회 전용. 주문 기능 없음."
    ]
    return "\n".join(lines)

def update_issue(n, body):
    r=requests.patch(f"{GH_API}/repos/{REPO}/issues/{n}",headers=gh_headers(),
                     json={"body":body},timeout=15)
    r.raise_for_status()

def main():
    issue=ensure_issue()
    print("live issue =",issue)
    deadline=time.time()+RUN_MINUTES*60
    while time.time()<deadline:
        try:
            leaders=scan()
            update_issue(issue,issue_body(leaders))
            if leaders:
                print(datetime.now(KST).strftime("%H:%M:%S"),
                      leaders[0]["name"],leaders[0]["pct"],leaders[0]["score"])
        except Exception as e:
            print("SCAN ERROR",repr(e))
            try: update_issue(issue,issue_body([],repr(e)))
            except: pass
        time.sleep(max(15,SCAN_SEC))

if __name__=="__main__":
    main()
