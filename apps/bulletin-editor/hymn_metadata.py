"""Private, editable title crosswalk. Unknown associations stay empty."""
import csv,io,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[2]
PATH=ROOT/'system'/'찬송가_제목번호_대장.csv'
FIELDS=['한일번호','한국어제목','일본어제목','새찬송가번호','일본찬송가종류','일본찬송가번호','확인출처']
def records():
    if not PATH.exists():return {}
    with PATH.open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    result={}
    for r in rows:
        if not r.get('한일번호','').isdigit():continue
        n=int(r['한일번호'])
        if n in result:raise ValueError('찬송가 대장에 중복 번호가 있습니다: '+str(n))
        result[n]=dict(titleKo=r.get('한국어제목',''),titleJa=r.get('일본어제목',''),newKorean=r.get('새찬송가번호',''),japaneseType=r.get('일본찬송가종류',''),japaneseNumber=r.get('일본찬송가번호',''),metadataSource=r.get('확인출처',''),newKoreanStatus=r.get('새번호상태',''),japaneseStatus=r.get('일본번호상태',''),metadataNote=r.get('비고',''))
    return result
def format_song(song):
    title=song.get('titleKo') or song['title'];ja=song.get('titleJa','')
    names=(f'『{ja}』' if ja else '')+f'({title})'
    refs=[]
    if song.get('japaneseType') and song.get('japaneseNumber'):refs.append(song['japaneseType']+str(song['japaneseNumber']))
    if song.get('newKorean'):refs.append('새'+str(song['newKorean']))
    return names+'\n韓日'+str(song['number'])+'章'+('('+','.join(refs)+')' if refs else '')
