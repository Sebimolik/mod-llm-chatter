"""ptBR language tables for mod-llm-chatter.

Session-vibe and per-bot mood wording only. 3.3.5a ships
no ptBR rows in the world DB's *_template_locale tables,
so this locale carries no name data -- the wording here
needs none.
"""


BOT_MOOD_LINE_TEMPLATES = {
    'own': "O seu próprio humor: {mood}",
    'current': "Humor atual: {mood}",
}


BOT_MOOD_WORDS = {
    'miserable': "péssimo",
    'gloomy': "sombrio",
    'tired': "cansado",
    'neutral': "neutro",
    'content': "contente",
    'cheerful': "animado",
    'ecstatic': "eufórico",
}


VIBE_LINE_TEMPLATES = {
    'sourced': (
        "Os membros do grupo continuam {mood} após {event}."
    ),
    'sourceless': (
        "Os membros do grupo estão {mood} agora, após algo"
        " que acabou de acontecer."
    ),
    'distinct': (
        " Esse é o clima do grupo inteiro, não o seu próprio"
        " humor indicado acima: os dois podem divergir"
        " tranquilamente."
    ),
    'instruction': (
        " Deixe isso tingir o seu jeito de falar: o"
        " sentimento deve transparecer no tom, nunca ser"
        " anunciado."
    ),
}


VIBE_MOOD_WORDS = {
    'accomplished': "com o dever cumprido",
    'admiring': "admirados",
    'adventurous': "com sede de aventura",
    'affectionate': "afetuosos",
    'alarmed': "alarmados",
    'alert': "em alerta",
    'amused': "divertidos",
    'approving': "aprovadores",
    'awed': "maravilhados",
    'breathless': "sem fôlego",
    'cautious': "cautelosos",
    'cheerful': "animados",
    'contemplative': "contemplativos",
    'curious': "curiosos",
    'delighted': "encantados",
    'determined': "determinados",
    'disappointed': "decepcionados",
    'eager': "ansiosos por ação",
    'elated': "eufóricos",
    'engaged': "absortos",
    'envious': "invejosos",
    'excited': "empolgados",
    'exhilarated': "exultantes",
    'fierce': "ferozes",
    'focused': "concentrados",
    'fond': "cheios de carinho",
    'frustrated': "frustrados",
    'glad': "felizes",
    'gleeful': "risonhos",
    'grateful': "gratos",
    'grimly amused': "com humor sombrio",
    'humbled': "humilhados",
    'impressed': "impressionados",
    'inspired': "inspirados",
    'intrigued': "intrigados",
    'jubilant': "jubilosos",
    'moved': "comovidos",
    'nostalgic': "nostálgicos",
    'playful': "brincalhões",
    'pleased': "contentes",
    'proud': "orgulhosos",
    'reflective': "reflexivos",
    'relieved': "aliviados",
    'resilient': "inabaláveis",
    'respectful': "respeitosos",
    'rueful': "cheios de pesar",
    'ruthless': "implacáveis",
    'satisfied': "satisfeitos",
    'stoic': "estoicos",
    'surprised': "surpresos",
    'thoughtful': "pensativos",
    'triumphant': "triunfantes",
    'victorious': "vitoriosos",
    'warm': "calorosos",
    'wistful': "melancólicos",
}


# Portuguese and Korean are supported output languages (see
# _LANGUAGE_LABELS in chatter_shared.py) even though they lack
# full-scale flavor-text coverage elsewhere. These strings carry no
# proper nouns and depend on no other localized data, so there is no
# baseline to run ahead of.
# Confidence: PT/KO are best-effort hand-written renderings by a
# non-native writer -- lower confidence than the RU/FR/DE/ES tables
# above and worth a native review; the one game term they still
# contain ("montaria", "탈것") follows the usual client vocabulary,
# and the rest is in-world prose rather than player jargon.
VIBE_SOURCE_PHRASES = {
    'wipe': "caírem todos até o último",
    'boss_kill': "derrubar um inimigo temível",
    'rare_kill': "caçar uma fera rara e perigosa",
    'achievement': "um feito digno de memória",
    'level_up': "um avanço conquistado a duras penas",
    'quest_complete': "levar uma tarefa até o fim",
    'dungeon': "o último trecho daquelas profundezas",
    'bg_win': "uma batalha vencida",
    'bg_loss': "uma batalha perdida",
    'pvp_kill': "vencer um inimigo em combate",
    'discovery': "avistar terras desconhecidas",
    'player_message': "o que se disse entre eles",
    'party_member': "um momento partilhado com um dos seus",
    'gear_change': "as armas novas de alguém",
    'mount_change': "a montaria nova de alguém",
    'first_meeting': "conhecer alguém novo na estrada",
    'ambient': "um momento tranquilo na estrada",
    'condensed': "tudo o que passaram juntos",
}
