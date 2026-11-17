"""Public-source eligibility information, without starting OAuth or submitting forms."""
import siwc

def status():
    value=siwc.eligibility()
    return {'enabled':value['allowed'],'private':False,'status':value['route'],
        'message':value['message'],'sourceUrl':value['sourceUrl'],
        'docsUrl':'https://developers.openai.com/siwc/quickstart',
        'interestUrl':'https://openai.com/form/sign-in-with-chatgpt-interest/',
        'report':'공개 대상은 MIT 라이선스 프로그램 코드입니다. 교회 자료·성경·찬송가·PPT·글꼴·개인정보·로그인 정보는 제외합니다. 계정별 실제 권한은 공식 로그인 후 확인합니다.',
        'draft':'','submitted':False,'missing':[] if value['allowed'] else ['프로그램 코드의 공개 배포 확인']}
