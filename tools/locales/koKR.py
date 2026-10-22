# -*- coding: utf-8 -*-
"""koKR locale data for mod-llm-chatter.

Split out of chatter_constants.py so the central module stays
readable: the language tables are bulk data that changes for
translation reasons, not logic that changes with the module.

Names here are unsuffixed -- the locale is the module. The
registry in this package maps locale codes onto these.
"""

ZONE_NAMES = {
    65: "용의 안식처",  # verified: official Blizzard ko-kr news source
    66: "줄드락",  # verified: official Blizzard ko-kr news source
    67: "폭풍우 봉우리",  # verified: official Blizzard ko-kr news source
    210: "얼음왕관",  # verified: official Blizzard ko-kr news source
    394: "회색 구릉지",  # verified: official Blizzard ko-kr news source
    495: "울부짖는 협만",  # verified: official Blizzard ko-kr news source
    3537: "북풍의 땅",  # verified: official Blizzard ko-kr news source
    3711: "숄라자르 분지",  # verified: official Blizzard ko-kr news source
}


BOT_MOOD_LINE_TEMPLATES = {
    'own': "너 자신의 기분: {mood}",
    'current': "현재 기분: {mood}",
}


BOT_MOOD_WORDS = {
    'miserable': "비참함",
    'gloomy': "침울함",
    'tired': "지쳐 있음",
    'neutral': "평범함",
    'content': "만족스러움",
    'cheerful': "쾌활함",
    'ecstatic': "황홀함",
}


VIBE_LINE_TEMPLATES = {
    'sourced': "{event} 이후로 파티 전체가 아직 {mood} 분위기다.",
    'sourceless': (
        "방금 무슨 일이 있었는지, 파티 전체가 {mood} 분위기다."
    ),
    'distinct': (
        " 그건 파티 전체의 분위기이지 위에 적힌 네 기분이 아니다."
        " 둘은 얼마든지 다를 수 있다."
    ),
    'instruction': (
        " 그 감정이 말투에 배어 나오게 하되, 절대 말로 설명하지"
        " 마라."
    ),
}


VIBE_MOOD_WORDS = {
    'accomplished': "해냈다는 뿌듯함에 찬",
    'admiring': "감탄한",
    'adventurous': "모험심에 들뜬",
    'affectionate': "정이 넘치는",
    'alarmed': "놀라 경계하는",
    'alert': "바짝 긴장한",
    'amused': "재미있어하는",
    'approving': "인정하는",
    'awed': "경외에 찬",
    'breathless': "숨이 턱에 찬",
    'cautious': "조심스러운",
    'cheerful': "쾌활한",
    'contemplative': "사색에 잠긴",
    'curious': "호기심에 찬",
    'delighted': "기뻐하는",
    'determined': "결의에 찬",
    'disappointed': "실망한",
    'eager': "싸울 기세가 오른",
    'elated': "들뜬",
    'engaged': "몰입한",
    'envious': "부러워하는",
    'excited': "흥분한",
    'exhilarated': "짜릿함에 취한",
    'fierce': "사나운",
    'focused': "집중한",
    'fond': "서로에게 살가운",
    'frustrated': "답답해하는",
    'glad': "반가운",
    'gleeful': "신이 난",
    'grateful': "고마워하는",
    'grimly amused': "씁쓸하게 웃는",
    'humbled': "겸허해진",
    'impressed': "감탄한",
    'inspired': "고무된",
    'intrigued': "흥미가 동한",
    'jubilant': "환호에 찬",
    'moved': "뭉클해진",
    'nostalgic': "그리움에 잠긴",
    'playful': "장난기 어린",
    'pleased': "흡족한",
    'proud': "자랑스러워하는",
    'reflective': "생각에 잠긴",
    'relieved': "안도한",
    'resilient': "꺾이지 않은",
    'respectful': "존중이 담긴",
    'rueful': "후회가 남은",
    'ruthless': "무자비한",
    'satisfied': "만족한",
    'stoic': "담담한",
    'surprised': "놀란",
    'thoughtful': "사려 깊은",
    'triumphant': "승리에 도취된",
    'victorious': "승리한",
    'warm': "따뜻한",
    'wistful': "아련한",
}


VIBE_SOURCE_PHRASES = {
    'wipe': "다 함께 쓰러진 일",
    'boss_kill': "무서운 적을 쓰러뜨린 일",
    'rare_kill': "희귀하고 위험한 야수를 사냥한 일",
    'achievement': "기억에 남을 위업",
    'level_up': "힘겹게 얻은 성장",
    'quest_complete': "맡은 일을 끝까지 해낸 일",
    'dungeon': "그 깊은 곳에서 넘긴 마지막 고비",
    'bg_win': "싸움에서 거둔 승리",
    'bg_loss': "싸움에서 당한 패배",
    'pvp_kill': "적을 싸움에서 꺾은 일",
    'discovery': "낯선 땅을 처음 본 일",
    'player_message': "서로 주고받은 이야기",
    'party_member': "동료와 나눈 한순간",
    'gear_change': "누군가의 새 무기와 갑옷",
    'mount_change': "누군가의 새 탈것",
    'first_meeting': "길에서 만난 새로운 이",
    'ambient': "길 위의 조용한 한때",
    'condensed': "함께 겪어온 모든 일",
}
