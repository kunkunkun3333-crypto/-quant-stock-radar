"""V5.1 股票池與可解釋評分。"""
import time
import requests
import pandas as pd

INDUSTRIES = dict(zip(map(str, range(24, 32)), ['半導體業','電腦及週邊設備業','光電業','通信網路業','電子零組件業','電子通路業','資訊服務業','其他電子業']))
SOURCES = [('上市','https://openapi.twse.com.tw/v1/opendata/t187ap03_L','.TW'), ('上櫃','https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O','.TWO')]

def normalize(rows, board, suffix):
    out=[]
    for r in rows:
        code=str(r.get('公司代號') or r.get('SecuritiesCompanyCode') or '').strip()
        industry=str(r.get('產業別') or r.get('SecuritiesIndustryCode') or '').strip().zfill(2)
        if not code.isdigit(): continue
        out.append({'Ticker':code+suffix,'公司名稱':r.get('公司簡稱') or r.get('CompanyAbbreviation') or code,'上市櫃':board,'產業代碼':industry,'產業':INDUSTRIES.get(industry,'其他分類'),'清單日期':r.get('出表日期') or r.get('Date') or ''})
    return out

def fetch_catalog():
    rows=[]; errors=[]
    for board,url,suffix in SOURCES:
        for attempt in range(3):
            try:
                response=requests.get(url,timeout=25,headers={'User-Agent':'Mozilla/5.0'})
                response.raise_for_status()
                data=response.json()
                if not isinstance(data,list) or not data: raise ValueError('來源未提供有效清單')
                parsed=normalize(data,board,suffix)
                if not parsed: raise ValueError('無法辨識公司代號欄位')
                rows.extend(parsed); break
            except Exception as exc:
                if attempt==2: errors.append(f'{board}清單取得失敗：{exc}')
                else: time.sleep(attempt+1)
    return pd.DataFrame(rows,columns=['Ticker','公司名稱','上市櫃','產業代碼','產業','清單日期']).drop_duplicates('Ticker'),errors

def score_details(row,cfg):
    rows=[]
    def yes(k): return pd.notna(row.get(k)) and bool(row.get(k))
    def gt(k,n=0): return pd.notna(row.get(k)) and row[k]>n
    def add(group,rule,points,condition): rows.append({'分類':group,'規則':rule,'可得分':points,'得分':points if condition else 0})
    add('趨勢','最新日短均線上穿長均線',10,yes('Golden Cross'))
    add('趨勢','價格高於趨勢均線',9,yes('Above MA200'))
    add('趨勢','長均線高於趨勢均線',6,yes('MA60 Above MA200'))
    for k,p in [('3M Return',4),('6M Return',4),('Relative Strength 3M',2),('Relative Strength 6M',2)]: add('動能',k+' > 0',p,gt(k))
    add('動能','MACD 高於訊號線',5,pd.notna(row.get('MACD')) and pd.notna(row.get('MACD Signal')) and row['MACD']>row['MACD Signal'])
    add('動能','MACD 柱狀值 > 0',3,gt('MACD Hist'))
    pe=row.get('P/E')
    vp=5 if pd.isna(pe) else (15 if 0<pe<20 else 11 if pe<cfg.pe_max else 5 if pe<50 else 0)
    rows.append({'分類':'估值','規則':'本益比分段：缺值5分；0–20得15分；其餘依上限判斷（沿用V5）','可得分':15,'得分':vp})
    for k,p in [('Revenue Growth',6),('EPS Growth',6),('ROE',5),('Free Cash Flow',3)]: add('基本面',k+' > 0',p,gt(k))
    vr=row.get('Volume Ratio'); v=10 if pd.notna(vr) and vr>=cfg.volume_ratio_bonus else 5 if pd.notna(vr) and vr>=.8 else 0
    rows.append({'分類':'成交量','規則':f'量比 ≥ {cfg.volume_ratio_bonus} 得10分；否則 ≥ 0.8 得5分','可得分':10,'得分':v})
    add('買點',f'乖離率介於 {cfg.bias_min}% 至 {cfg.bias_max}%',3,pd.notna(row.get('BIAS20')) and cfg.bias_min<=row['BIAS20']<=cfg.bias_max)
    add('買點','RSI介於30至70',2,pd.notna(row.get('RSI14')) and 30<=row['RSI14']<=70)
    add('買點','價格介於布林下軌與中軌',5,all(pd.notna(row.get(k)) for k in ['Latest Price','BB Lower','BB Mid']) and row['BB Lower']<=row['Latest Price']<=row['BB Mid'])
    return pd.DataFrame(rows)
