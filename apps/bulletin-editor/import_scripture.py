"""Import the user's flat Korean JSON without guessing merged/invalid verse keys."""
import argparse, hashlib, json, pathlib, re, shutil
from scripture import ROOT, IDS

ABBR='창 출 레 민 신 수 삿 룻 삼상 삼하 왕상 왕하 대상 대하 스 느 에 욥 시 잠 전 아 사 렘 애 겔 단 호 욜 암 옵 욘 미 나 합 습 학 슥 말 마 막 눅 요 행 롬 고전 고후 갈 엡 빌 골 살전 살후 딤전 딤후 딛 몬 히 약 벧전 벧후 요일 요이 요삼 유 계'.split()

def convert(data):
    names=dict(zip(ABBR,IDS));books={};blocked=[];invalid=[]
    for key,text in data.items():
        m=re.fullmatch(r'([가-힣]+)(\d+):(\d+)(?:-(\d+))?',key)
        if not m or m[1] not in names or not isinstance(text,str) or not text.strip():
            invalid.append(key);continue
        book=names[m[1]];chapter=int(m[2]);verse=int(m[3]);end=int(m[4] or verse)
        if end!=verse:
            blocked.append({'book':book,'chapter':chapter,'verses':list(range(verse,end+1)),'key':key});continue
        books.setdefault(book,{}).setdefault(str(chapter),{})[str(verse)]=text
    # Unknown verse strings are retained in the report. Gaps fail lookup automatically.
    return {'version':'개역개정','verification':'user-designated; not independently verified','books':books,'blockedRanges':blocked},invalid

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('file');a=p.parse_args();source=pathlib.Path(a.file)
    raw=source.read_bytes();data=json.loads(raw.decode('utf-8-sig'));result,invalid=convert(data)
    dest=ROOT/'자료'/'성경';dest.mkdir(parents=True,exist_ok=True)
    (dest/'bible.user-original.json').write_bytes(raw)
    target=dest/'개역개정.user.json';target.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    report={'inputEntries':len(data),'importedVerses':sum(len(c) for b in result['books'].values() for c in b.values()),'bookCount':len(result['books']),'mergedRanges':result['blockedRanges'],'invalidKeys':invalid,'sha256':hashlib.sha256(raw).hexdigest(),'versionVerified':False}
    (dest/'import-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    cp=ROOT/'system'/'scripture-provider.json';config=json.loads(cp.read_text(encoding='utf-8-sig')) if cp.exists() else {}
    config['ko']={'file':target.relative_to(ROOT).as_posix(),'source':'사용자 제공 bible.json · 개역개정 지정 · 판본 대조 미완료','userSupplied':True,'permissionConfirmed':False}
    cp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('mergedRanges','invalidKeys')},ensure_ascii=True));print('merged',len(report['mergedRanges']),'invalid',len(invalid))
