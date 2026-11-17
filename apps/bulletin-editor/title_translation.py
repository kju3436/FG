"""Translate sermon titles only. Scripture never enters this operation."""
import re
from ai_client import complete_json
GLOSSARY={'하나님':'神','예수 그리스도':'イエス・キリスト','성령':'聖霊','은혜':'恵み','구원':'救い','믿음':'信仰','회개':'悔い改め','복음':'福音','부활':'復活','영생':'永遠のいのち','하나님의 나라':'神の国','주님':'主','제자':'弟子'}
def translate(data):
    source=data.get('source');text=data.get('text','')
    if source not in ('ko','ja') or not isinstance(text,str) or not text.strip() or len(text)>500:raise ValueError('번역할 설교 제목을 500자 이내로 입력해주세요.')
    value=complete_json('Translate only the sermon TITLE between Korean and Japanese. Use natural Protestant Christian terminology consistent with 개역개정 and 新改訳2017. Do not generate or translate Bible passages. Preserve names and numerals. Return {"translation":"..."}.',{'source':source,'target':'ja' if source=='ko' else 'ko','title':text,'terminology':GLOSSARY},max_tokens=700)
    result=value.get('translation')
    if not isinstance(result,str) or not result.strip() or len(result)>1000:raise ValueError('제목 번역 결과를 확인하지 못했습니다.')
    if re.findall(r'\d+',text)!=re.findall(r'\d+',result):raise ValueError('제목 번역에서 숫자가 달라졌습니다. 직접 확인해주세요.')
    return {'source':source,'text':text,'translation':result.strip()}
