"""Single source of truth for every regional variety the assistant supports.

Backend reads `register` / `style_notes` / `fallback_*` to build prompts.
Frontend reads `label` / `script` / `letter` / `description_short` / `description_style`
/ `keywords` / `landmarks` / `trial` from `/api/presets` — no duplicate tables in JS.

Adding a new variety = add an enum value + one PresetMetadata entry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Script(str, Enum):
    SIMPLIFIED = "simplified"
    TRADITIONAL = "traditional"


class Scenario(str, Enum):
    """Conversational context. Steers the LLM toward register/audience-appropriate language.

    The point: a Beijing native talks to their boss differently than to their xiaohuoji friend,
    even using the same dialect. Without a scenario hint, the LLM averages toward a safe, dull
    middle ground.
    """

    FRIENDS_CASUAL = "friends_casual"  # default — chatting with peers
    WITH_ELDERS = "with_elders"  # respectful, slower, kinship-aware
    WORKPLACE = "workplace"  # semi-formal, hedged, professional
    ONLINE_CHAT = "online_chat"  # IM/forum slang, abbreviations, emoji-paced
    VENTING = "venting"  # ranting; emotional, dramatic, uses cuss-adjacent particles
    COAXING = "coaxing"  # placating/soothing — gentle, soft endings


@dataclass(frozen=True)
class ScenarioMetadata:
    label: str  # human-readable Chinese label for UI
    description: str  # short hint shown under the chip
    prompt_addendum: str  # appended to the system prompt; behavioral steering


SCENARIO_METADATA: dict[Scenario, ScenarioMetadata] = {
    Scenario.FRIENDS_CASUAL: ScenarioMetadata(
        label="朋友闲聊",
        description="跟同辈朋友/室友/发小放松地聊",
        prompt_addendum=(
            "【场景】跟同辈朋友闲聊。可以撒娇、可以幼稚、可以糙、可以耍宝。"
            "句子可以短、可以省略主语，语气词放开用；不要装正经，也不要拽文。"
        ),
    ),
    Scenario.WITH_ELDERS: ScenarioMetadata(
        label="跟长辈说话",
        description="对父母/长辈/老一辈用敬称，更稳",
        prompt_addendum=(
            "【场景】跟长辈/父母/家里老一辈说话。"
            "比平时慢、稳一档；用「您」「叔」「阿姨」之类称呼；不耍宝、不爆粗、不用网络梗；"
            "情绪要收着，体贴一点；语气词偏柔（如「呀」「呢」「啦」），少用「靠」「卧槽」类。"
        ),
    ),
    Scenario.WORKPLACE: ScenarioMetadata(
        label="工作场合",
        description="同事/客户/老板，半正式有分寸",
        prompt_addendum=(
            "【场景】工作沟通——跟同事/老板/客户讲。"
            "保留方言味但收一档：不用粗口、不用太土的俚语；"
            "结构要清晰，多用「这边」「那边」「咱们」类缓冲词；"
            "拒绝/质疑要委婉，加「方便的话」「您看」类垫词。"
        ),
    ),
    Scenario.ONLINE_CHAT: ScenarioMetadata(
        label="网上聊",
        description="微信/小红书/连登/LINE 群的口吻",
        prompt_addendum=(
            "【场景】网上跟朋友打字聊天（微信、小红书、连登、LINE 群）。"
            "节奏短促，断句自由，可以用网络流行语和缩写；"
            "可以「哈哈哈」「awsl」「绝了」「笑死」类口语化反应；"
            "不要正式标点感，逗号句号可以少用。"
        ),
    ),
    Scenario.VENTING: ScenarioMetadata(
        label="吐槽抱怨",
        description="情绪要外露，夸张点没事",
        prompt_addendum=(
            "【场景】吐槽/抱怨/发牢骚。"
            "情绪要明显外露——用强调词、夸张比喻、反讽；"
            "句末语气词偏「啊！」「了喂」「死了」「不行」类带情绪的；"
            "可以用本方言里偏冲的口头禅（如东北「整不明白」、川渝「搞球哦」、粤语「黐线」），"
            "但避免直接人身攻击或粗口。"
        ),
    ),
    Scenario.COAXING: ScenarioMetadata(
        label="哄人安抚",
        description="哄朋友/对象/小孩，软一点慢一点",
        prompt_addendum=(
            "【场景】哄人/安抚/劝慰对方。"
            "语速要软，多用让步词（「先这样」「慢慢来」「没事的」）；"
            "句末加柔尾巴（「呀」「啦」「嘛」「好不好」）；"
            "不要讲道理压人，先共情再建议；"
            "称呼可以亲昵一点（「乖」「小笨蛋」类，视方言习惯调整）。"
        ),
    ),
}


def parse_scenario(value: str | None) -> Scenario:
    """Parse scenario from API. None / empty / unknown → FRIENDS_CASUAL (safe default)."""
    if not value:
        return Scenario.FRIENDS_CASUAL
    try:
        return Scenario(value)
    except ValueError:
        return Scenario.FRIENDS_CASUAL


def scenario_options() -> list[dict[str, str]]:
    return [
        {"value": s.value, "label": meta.label, "description": meta.description}
        for s, meta in SCENARIO_METADATA.items()
    ]


class VarietyPreset(str, Enum):
    STANDARD_PUTONGHUA = "standard_putonghua"
    BEIJING_MANDARIN = "beijing_mandarin"
    DONGBEI_MANDARIN = "dongbei_mandarin"
    SICHUAN_CHONGQING_MANDARIN = "sichuan_chongqing_mandarin"
    JIANGHUAI_OR_LOWER_YANGTZE_MANDARIN = "jianghuai_or_lower_yangtze_mandarin"
    GUANGDONG_MANDARIN = "guangdong_mandarin"
    SHANGHAI_MANDARIN_STYLE = "shanghai_mandarin_style"
    CANTONESE_WRITTEN = "cantonese_written"
    HOKKIEN_WRITTEN = "hokkien_written"
    MINNAN_WRITTEN = "minnan_written"


@dataclass(frozen=True)
class Landmark:
    url: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"url": self.url, "name": self.name}


@dataclass(frozen=True)
class PresetMetadata:
    label: str
    script: Script
    letter: str  # one-character glyph for the atlas tile
    register: str  # for LLM prompt
    style_notes: str  # for LLM prompt
    fallback_warning: str  # for heuristic fallback path
    description_short: str  # short tag-line for UI ("当前风味" card register)
    description_style: str  # one-line style hint for UI atlas / variety card
    keywords: tuple[str, ...]  # for client-side keyword highlight; longest first by convention
    landmarks: tuple[Landmark, ...]  # background images
    trial: bool = False  # mark "试用版" — UI flags it and disables Mandarin TTS
    tts_lang: str | None = None  # BCP-47 lang for SpeechSynthesis; None = TTS disabled
    # DeepSeek reasoning control for rewrite generation (2026-08 quality work):
    # "disabled" = no CoT (fast, right for Mandarin-family varieties where the
    # transform is shallow); "low" = bounded light CoT (low-resource varieties
    # whose grammar genuinely differs — function words, word order, sentence-
    # final particles need planning, and zero-CoT flash just sprinkles flavor
    # words onto Mandarin syntax). Anything else = provider default (full CoT).
    reasoning: str = "disabled"
    # Model routing for rewrite generation (2026-08 A/B/C/D experiment, judged
    # by deepseek-v4-pro: the pro tier is the ONLY lever that moved low-resource
    # dialect quality — 4.33 vs 3.67 mean, higher floor, zero empty-content
    # errors; light CoT was neutral). None = global LLM_MODEL (flash tier).
    model: str | None = None


# ---------------- preset table ----------------
#
# All landmark images are from Wikimedia Commons (CC-licensed, stable URLs).
# Each is the lead image of the article-of-record for that landmark.
# Source: en.wikipedia.org / zh.wikipedia.org REST page-summary API.

PRESET_METADATA: dict[VarietyPreset, PresetMetadata] = {
    VarietyPreset.STANDARD_PUTONGHUA: PresetMetadata(
        label="标准普通话",
        script=Script.SIMPLIFIED,
        letter="通",
        register="Authentic daily conversational Mandarin spoken by native speakers in mainland China.",
        style_notes=(
            "Use highly natural, conversational Putonghua. Include common discourse particles "
            "(啊, 嘛, 呢, 呗), natural filler words (那个, 就是), and idiomatic phrasing typical "
            "of daily life. Do not sound like a news anchor; sound like a real person talking "
            "to a friend."
        ),
        fallback_warning="Returned in standard Putonghua.",
        description_short="标准普通话 · 跟朋友聊天那种自然顺口的语气",
        description_style="保留原意，去掉书面腔，加生活化语气词。",
        keywords=("呗", "嘛", "呢", "啊"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/The_Great_Wall_of_China_at_Jinshanling-edit.jpg?width=2400",
                "万里长城",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Hall_of_Supreme_Harmony_%2820241127120000%29.jpg?width=2400",
                "故宫",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/%E5%A4%A9%E5%AE%89%E9%97%A8%E5%A4%9C%E6%99%AF.jpg?width=2400",
                "天安门",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Huangshan_fengjing.jpg?width=2400",
                "黄山",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Qutang_Gorge_on_Changjiang.jpg?width=2400",
                "长江",
            ),
        ),
        tts_lang="zh-CN",
    ),
    VarietyPreset.BEIJING_MANDARIN: PresetMetadata(
        label="北京话",
        script=Script.SIMPLIFIED,
        letter="京",
        register="Authentic, lively Beijing local dialect and urban slang.",
        style_notes=(
            "Fully embrace Beijing slang, heavy erhua (儿化音), and local expressions "
            "(e.g., 哥们儿, 发小, 局气, 鸡贼, 没戏, 靠谱). Use a relaxed, confident, slightly "
            "banter-heavy tone (侃大山 style). Include typical Beijing sentence endings and "
            "particles like '嘛您呐', '得嘞', '哟'."
        ),
        fallback_warning="Regional flavor reduced toward standard Putonghua.",
        description_short="北京话 · 胡同口侃大山的松弛劲儿",
        description_style="儿化音、靠谱、没戏、得嘞——一开口就是地道京片子。",
        keywords=("哥们儿", "发小儿", "靠谱", "没戏", "局气", "鸡贼", "得嘞", "您呐", "儿"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Temple_of_Heaven_20160323_01.jpg?width=2400",
                "天坛祈年殿",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Longevity_Hill_of_the_Summer_Palace.jpg?width=2400",
                "颐和园",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/IMG_3084_Hutong_seen_from_Drum_Tower_Beijing_august_2007.JPG?width=2400",
                "胡同",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Beijing_Drum_Tower_viewed_from_Beijing_Bell_Tower.jpg?width=2400",
                "钟鼓楼",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Beihai_Park_2.jpg?width=2400", "北海公园"
            ),
        ),
        tts_lang="zh-CN",
    ),
    VarietyPreset.DONGBEI_MANDARIN: PresetMetadata(
        label="东北话",
        script=Script.SIMPLIFIED,
        letter="东",
        register="Highly expressive, direct, and humorous Northeast China (Dongbei) dialect.",
        style_notes=(
            "Use authentic Dongbei vocabulary extensively (e.g., 贼拉, 嘎嘎, 唠嗑, 瞎掰, 扯犊子, "
            "磨叽, 哎呀妈呀, 咋整). The tone should be very enthusiastic, warm, straightforward, "
            "and slightly dramatic. Use characteristic grammatical structures like '整', "
            "'干啥呢', '老...了'."
        ),
        fallback_warning="Dongbei flavor reduced toward standard Putonghua.",
        description_short="东北话 · 自带 BGM 的热情豪爽",
        description_style="贼拉、嘎嘎、唠嗑、整一个，三句话就能聊成老铁。",
        keywords=("贼拉", "嘎嘎", "唠嗑", "扯犊子", "瞎掰", "磨叽", "哎呀妈呀", "咋整", "整", "老"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Harbin_Ice_Festival.jpg?width=2400",
                "哈尔滨冰雪节",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/West_facade_of_St._Sophia_Cathedral%2C_Harbin_%2820230721150450%29.jpg?width=2400",
                "圣索菲亚教堂",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Baitou_Mountain_Tianchi.jpg?width=2400",
                "长白山天池",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Paektu-san.jpg?width=2400", "长白山"
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Songhua_River_1.JPG?width=2400", "松花江"
            ),
        ),
        tts_lang="zh-CN",
    ),
    VarietyPreset.SICHUAN_CHONGQING_MANDARIN: PresetMetadata(
        label="四川/重庆话",
        script=Script.SIMPLIFIED,
        letter="川",
        register="Spicy, lively, and laid-back Sichuan/Chongqing dialect (Chuanqiang).",
        style_notes=(
            "Use rich Chuan-Yu localisms (e.g., 巴适得很, 安逸, 雄起, 搞锤子, 瓜娃子, 撇脱, 要得). "
            "Incorporate typical sentence-final particles like '嘛', '撒', '哈', '哦'. The mood "
            "should feel relaxed but emotionally expressive, matching the local 'chill but "
            "fiery' temperament."
        ),
        fallback_warning="Southwest flavor reduced toward standard Putonghua.",
        description_short="川渝话 · 麻辣里透着安逸",
        description_style="巴适、要得、瓜娃子、撇脱，句尾爱挂'嘛'、'撒'。",
        keywords=("巴适得很", "巴适", "安逸", "雄起", "瓜娃子", "撇脱", "要得", "嘛", "撒", "哈"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Leshan_Giant_Buddha_%281%29.jpg?width=2400",
                "乐山大佛",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/1_nuorilang_jiuzhaigou_2023.jpg?width=2400",
                "九寨沟",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/%E5%B3%A8%E7%9C%89%E5%B1%B1%E9%A3%8E%E6%99%AF%E5%8C%BA_Mount_Emei_Scenic_Area_07.jpg?width=2400",
                "峨眉山",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Giant_Panda_2004-03-2.jpg?width=2400",
                "大熊猫",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Hongya_Cave_20180520.jpg?width=2400",
                "洪崖洞",
            ),
        ),
        tts_lang="zh-CN",
    ),
    VarietyPreset.JIANGHUAI_OR_LOWER_YANGTZE_MANDARIN: PresetMetadata(
        label="江淮/下江官话",
        script=Script.SIMPLIFIED,
        letter="江",
        register="Authentic Jianghuai (Nanjing/Yangzhou area) regional dialect.",
        style_notes=(
            "Use Nanjing/Jianghuai regional expressions (e.g., 乖乖隆地咚, 阿是啊, 蛮好, 夹生, "
            "潘西). Tone should be casual, brisk, and slightly earthy but friendly. Use localized "
            "particles and pronunciation habits where possible in text."
        ),
        fallback_warning="Regional flavor reduced toward standard Putonghua.",
        description_short="江淮话 · 南京、扬州一带的烟火气",
        description_style="乖乖隆地咚、阿是啊、蛮好的——亲切朴实。",
        keywords=("乖乖隆地咚", "阿是啊", "蛮好", "夹生", "潘西", "蛮"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Hall_of_Sun_Yat-sen_Mausoleum.jpg?width=2400",
                "中山陵",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/South_Lake_of_Hongcun_20141110.JPG?width=2400",
                "宏村",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Thin_West_Lake_south_entrance.JPG?width=2400",
                "瘦西湖",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/2024Apr_-_Nanjing_City_Wall_--_south_section_-_img_04.jpg?width=2400",
                "秦淮河",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Ancient_Villages_in_Southern_Anhui_%E2%80%93_Xidi_and_Hongcun-114155.jpg?width=2400",
                "徽派建筑",
            ),
        ),
        tts_lang="zh-CN",
        model="deepseek-v4-pro",  # low-resource variety → pro tier (2026-08 experiment)
    ),
    VarietyPreset.GUANGDONG_MANDARIN: PresetMetadata(
        label="广东普通话",
        script=Script.SIMPLIFIED,
        letter="粤",
        register=(
            "广东本地人讲的「煲冬瓜 / 塑料普通话」，用简体汉字写成，"
            "字面看是普通话，但语序、词汇和句末助词都暴露明显粤语母语痕迹。"
            "目标读者：内地朋友、同事、客户。"
        ),
        style_notes=(
            "★ 语序粤化（最关键）：副词后置、宾语后置。"
            "例：「你先走」→「你走先」；「你先吃」→「你食先 / 你先吃啦」；"
            "「再说一次」→「再讲多一次」；「多吃一点」→「食多啲 / 多吃点啦」；"
            "「给我」常说成「畀我 / 拿俾我」。\n"
            "★ 「有/冇」结构：表完成或经历用「有没有」。"
            "「你吃了吗」→「你有没有吃啊」；「我没去过」→「我冇去过」（口语可写「冇」）。\n"
            "★ 高频粤式词汇直接夹在普通话里：埋单、搞掂（搞定）、得闲、唔好意思、"
            "好犀利、靓仔/靓女、冲凉、返工/收工、揾食、饮茶、叹世界、扑街（骂人慎用）、"
            "唔该（谢谢/劳驾）、冇问题、好正、好嘢、好彩、湿碎、求其、晒（很/极了）。\n"
            "★ 句末助词必须高频出现，让句子有粤味韵律：啦、咯、啰、嘛、嘅、㗎、吖、囉、咧、哎也。"
            "例：「就这样啦」「真系唔好意思咯」「冇所谓嘅」「好啦好啦」。\n"
            "★ 程度词替换：「非常」→「好」「劲」「超」；「真的」→「真系 / 真嘅」；"
            "「特别」→「特登」；「很厉害」→「劲犀利 / 好正」。\n"
            "★ 招呼与人称：朋友常叫「靓仔/靓女/阿叔/阿姨」；自称少用「我」+长串解释，"
            "更爱说「我啊…」「我就…」。\n"
            "★ 文化背景：广东人讲话直接、效率高、爱讲意头（吉利话），饭桌上爱说"
            "「饮茶先啦」「搞掂收工」「得闲饮茶」。\n"
            "★ 反面禁忌：不要写得像新闻联播（「我即将前往」「请您稍等片刻」），"
            "不要照搬北方俚语（哥们儿、整一个、贼啦），不要全篇粤字（那是粤语书面语，"
            "不是广东普通话）。整体看像「会说普通话的广州人在跟你聊天」。"
        ),
        fallback_warning="Guangdong flavor reduced toward standard Putonghua.",
        description_short="广东普通话 · 粤味底色的塑料普通话",
        description_style="我走先、有冇搞错、埋单咯，粤味的尾音一耳听出。",
        keywords=("搞掂", "埋单", "得闲", "唔好意思", "靓仔", "靓女", "犀利", "好正", "啦", "咯", "嘅"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/%E5%B9%BF%E5%B7%9E%E5%A1%94Scenery_in_Guangzhou%2C_China_-_panoramio_%289%29.jpg?width=2400",
                "广州塔",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Chen_Clan_Ancestral_Hall_2025.06_03.jpg?width=2400",
                "陈家祠",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/ShamianIsland.JPG?width=2400", "沙面"
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/The_west_panorama_of_Shenzhen2021.jpg?width=2400",
                "深圳天际线",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Pearl_river%2C_Guangzhou.JPG?width=2400",
                "珠江",
            ),
        ),
        tts_lang="zh-CN",
    ),
    VarietyPreset.SHANGHAI_MANDARIN_STYLE: PresetMetadata(
        label="上海话风格",
        script=Script.SIMPLIFIED,
        letter="沪",
        register="Shanghai urbanite 'Jiao-Yuan' Mandarin mixed with Wu dialect habits.",
        style_notes=(
            "Blend standard Mandarin with Shanghainese syntax and vocabulary (e.g., 晓得, 拎不清, "
            "结棍, 阿拉, 蛮, 嗲, 捣糨糊). Tone should be polite but sharp, slightly pragmatic, "
            "and cosmopolitan. Use typical Shanghai particles like '呀', '哦', '伐' at the end "
            "of questions."
        ),
        fallback_warning="Shanghai-style flavor reduced toward standard Putonghua.",
        description_short="上海话风格 · 上海宁讲话有腔调",
        description_style="晓得伐、阿拉、蛮、嗲、捣糨糊，市井又精致。",
        keywords=("晓得伐", "晓得", "阿拉", "拎不清", "结棍", "嗲", "捣糨糊", "蛮", "伐"),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Promenade_du_Bund.jpg?width=2400", "外滩"
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Oriental_Pearl_Tower_2012.jpg?width=2400",
                "东方明珠",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Shanghai_Tower_in_2015_%282%29.jpg?width=2400",
                "上海中心",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/2024-Apr_Shanghai_Yu_Yuan_Garden_%E8%B1%AB%E5%9B%AD_-_img_20.jpg?width=2400",
                "豫园",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/East_Nanjing_Road_2020_%2850361842166%29.jpg?width=2400",
                "南京路",
            ),
        ),
        tts_lang="zh-CN",
        model="deepseek-v4-pro",  # low-resource variety → pro tier (2026-08 experiment)
    ),
    VarietyPreset.CANTONESE_WRITTEN: PresetMetadata(
        label="粵語書面語",
        script=Script.TRADITIONAL,
        letter="粵",
        register=(
            "香港 / 廣州街頭真粵語口語，全用繁體粵字寫，"
            "貼近 WhatsApp、連登、討論區、口語對白嘅日常風格，"
            "唔係用普通話直譯加幾個粵字。"
        ),
        style_notes=(
            "★ 全部係 / 唔係 / 唔好 / 冇 / 點解 / 點樣 / 乜嘢 / 邊個 / 喺 / 嚟 / 去 / 識 / 識唔識 / 想唔想 / 有冇，"
            "禁用普通話寫法（係 ≠ 是；唔 ≠ 不；冇 ≠ 沒有；喺 ≠ 在；嚟 ≠ 來；點解 ≠ 為什麼；"
            "乜嘢 ≠ 什麼；邊個 ≠ 哪個；唔該 ≠ 謝謝/勞煩；多謝 ≠ 感謝）。\n"
            "★ 代詞與所有格：你 / 我 / 佢（他/她）/ 我哋 / 你哋 / 佢哋；"
            "「的」一律寫成「嘅」；「了」一律寫成「咗」（如「食咗飯未呀」）。\n"
            "★ 動詞貼粵：吃 → 食；喝 → 飲；睡 → 瞓；走 → 行；跑 → 走；"
            "看 → 睇；給 → 畀；知道 → 知 / 知道；不要 → 唔好 / 咪。\n"
            "★ 句末助詞係靈魂，每句加 1～2 個（唔好堆）：啦、囉、咯、㗎、㗎喇、嘅、咩、吖、噃、添、啫、咧、呀、嘩。"
            "例：「真係好正啊」「冇所謂啦」「點解唔講聲嘅」「唔好咁啦」「食咗飯未呀」。\n"
            "★ 高頻地道詞：搞掂、得閒、唔好意思、唔該晒、勁正、好嘢、扺死、頂癮、爆、抵讚、"
            "好彩、好衰、慘啦、揾食、返工、收工、傾偈、出 pool、做嘢、嘥氣、無厘頭、㨂、"
            "走鬼、執生、放飛機、㩒掣、講真、講開又講。\n"
            "★ 港式英文 code-switch 自然出現：OK、send、check、book、cancel、friend、case、project、deadline、"
            "send 個 file 畀我、book 個位、cancel 咗個 booking、好 chill、好 high、check 下個 mail。\n"
            "★ 語氣分寸：粵語直率但有禮，朋友之間爆粗（仆街、頂）只在熟人間用，"
            "對長輩 / 客戶用「唔該晒」「麻煩晒」「唔好意思呀」。\n"
            "★ 文化錨點：飲早茶、煲劇、搭港鐵 / 搭巴士、出街、屋企、阿媽阿爸、放工去食宵夜、"
            "周末行山 / 去街、抽獎、攞 offer。\n"
            "★ 反面禁忌：唔好出現「是的、不是、什麼、怎麼、沒有、了、的、為什麼」呢類普通話寫法；"
            "唔好用全簡體；唔好寫成書面中文加幾個粵詞嘅雜種；唔好過度堆砌粗口；"
            "整體要讀得出聲、聽落自然，似一個香港 / 廣州人 send WhatsApp 畀朋友咁。"
        ),
        fallback_warning="Output drifted toward more standard written Chinese.",
        description_short="粵語書面語 · 港式 / 廣府口語",
        description_style="係、唔、點解、㗎喇——像在 WhatsApp 跟港朋友傾偈。",
        keywords=(
            "係",
            "唔係",
            "唔",
            "冇",
            "點解",
            "點樣",
            "乜嘢",
            "邊個",
            "喺",
            "嚟",
            "畀",
            "睇",
            "瞓",
            "嘅",
            "咗",
            "啦",
            "囉",
            "㗎",
            "咩",
            "吖",
            "噃",
            "添",
            "啫",
        ),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/HK_YTM_%E8%A5%BF%E4%B9%9D%E9%BE%8D%E6%96%87%E5%8C%96%E5%8D%80_West_Kowloon_Cultural_District_M%2B_Plus_Museum_%E5%A4%A9%E5%8F%B0%E8%8A%B1%E5%9C%92_roof_garden_view_Victoria_Harbour_July_2022_Px3_13.jpg?width=2400",
                "维多利亚港",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Tsim_Sha_Tsui_Overview_2018.jpg?width=2400",
                "尖沙咀",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Solar_Star_20110730.jpg?width=2400",
                "天星小轮",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Tian_Tan_Buddha_by_Beria.jpg?width=2400",
                "天坛大佛",
            ),
            Landmark("https://commons.wikimedia.org/wiki/Special:FilePath/Lionrock.jpg?width=2400", "狮子山"),
        ),
        tts_lang="zh-HK",
        model="deepseek-v4-pro",  # low-resource variety → pro tier (2026-08 experiment)
    ),
    VarietyPreset.HOKKIEN_WRITTEN: PresetMetadata(
        label="閩南語書面語（臺灣）",
        script=Script.TRADITIONAL,
        letter="臺",
        register=(
            "正港臺灣人講嘅臺語（Tâi-gí / 臺灣閩南語），用教育部推薦漢字寫，"
            "聽起來就像菜市場、夜市、家裡阿公阿嬤、朋友 LINE 群裡咧開講，"
            "毋是用國語直譯加幾个臺語詞。"
        ),
        style_notes=(
            "★ 代詞用法：我 / 你 / 伊（他/她）/ 阮（我們，不含對方）/ 咱（我們，含對方）/ 恁（你們）/ 𪜶 / 怹（他們）。\n"
            "★ 否定：毋（不）／無（沒有）／袂（不會、不能）／免（不用）。\n"
            "  例：「我不知道」→「我毋知」；「沒有啦」→「無啦」；「我做不到」→「我袂使做」；「不用啦」→「免啦」。\n"
            "★ 動詞貼臺：吃 → 食；喝 → 啉 / 飲；睡 → 睏；走 → 行；跑 → 走；看 → 看 / 觀；給 → 予（hōo）；"
            "說 → 講；好 → 好 / 讚；不好 → 歹；漂亮 → 媠（suí）；累 → 忝（thiám）。\n"
            "★ 高頻日常詞：歹勢（不好意思）、多謝（感謝）、拜託（請）、敢有（有沒有）、咁有（口語）、"
            "按呢（這樣）、按怎（怎麼）、啥物（什麼）、佗位（哪裡）、佮（和、跟）、攏（都）、嘛（也）、"
            "鬥陣（一起）、相揪（相約）、揣（找）、貯（裝）、鬱卒、龜毛、衰小、煞猛、撐腰、走䠫（緊張）、"
            "厝（家）、飯坩（飯鍋）、便當、紅龜粿、肉粽、滷肉飯。\n"
            "★ 句末助詞要密：啦、喔、呀、哩、欸、咧、呢、矣、噢。"
            "例：「無啦無啦」「真正好食喔」「食飽未咧」「你哪會按呢呢」「免緊張啦」。\n"
            "★ 文化錨點：拜拜、廟口、夜市、阿嬤、阿公、阿姨、做工課、放假轉去厝、"
            "去夜市食小吃、相揪去咖啡店、去淡水散步、唱卡啦 OK、阿莎力（爽快、乾脆）。\n"
            "★ 語氣：熱情、直接、帶點俏皮，朋友之間「啦」「喔」「咧」滿天飛，"
            "對長輩多用「歹勢」「拜託」「多謝」表禮貌。\n"
            "★ 反面禁忌：禁止整段用普通話只換幾個臺語詞；禁止寫「是」「不是」「什麼」「怎麼」「沒有」"
            "（要寫「是 / 毋是」「啥物」「按怎」「無」）；禁止用簡體；禁止把臺語當作 emoji 來貼。"
            "整段唸出來要像臺灣人原汁原味咧講話。"
        ),
        fallback_warning="Output drifted toward more standard written Chinese.",
        description_short="臺灣閩南語（試用版）",
        description_style="歹勢、讚啦、毋知、鬥陣——夜市那種熱絡親切。",
        keywords=(
            "歹勢",
            "讚",
            "毋知",
            "鬥陣",
            "龜毛",
            "袂",
            "啥物",
            "按呢",
            "按怎",
            "佮",
            "攏",
            "厝",
            "阿莎力",
            "啦",
            "喔",
            "咧",
        ),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/101.portrait.altonthompson.jpg?width=2400",
                "台北 101",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Sun_Moon_Lake_Sentinel-2B_MSI_2024-04-04.jpg?width=2400",
                "日月潭",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Eternal_Spring_Shrine_in_Taroko.JPG?width=2400",
                "太鲁阁",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/180124_Jiufen%2C_Taiwan_%E4%B9%9D%E4%BB%BD.jpg?width=2400",
                "九份山城",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Alishan_National_Scenic_Area_sign_20160208b.jpg?width=2400",
                "阿里山",
            ),
        ),
        trial=True,
        tts_lang=None,  # no widely-deployed Min Nan TTS — disable to avoid Mandarin mispronunciation
        model="deepseek-v4-pro",  # low-resource variety → pro tier (2026-08 experiment)
    ),
    VarietyPreset.MINNAN_WRITTEN: PresetMetadata(
        label="閩南語書面語（福建）",
        script=Script.TRADITIONAL,
        letter="閩",
        register=(
            "正港福建閩南話（泉州、漳州、廈門腔），用閩南本字漢字寫，"
            "風味比臺灣腔閩南語更厚實、更舊、更貼近老一輩人講古、泡茶、講鄉里故事嘅口吻，"
            "毋是用普通話直譯加幾個閩字。"
        ),
        style_notes=(
            "★ 代詞用法：我 / 汝（你）/ 伊（他/她）/ 阮（我們，排除對方）/ 咱（我們，含對方）/ 恁（你們）/ 𪜶（他們）。\n"
            "★ 否定：毋（不）／無（沒）／袂（不會、不能）／免（不必）。\n"
            "  例：「我不知道」→「我毋知影」；「沒有啦」→「無啦」；「不要這樣」→「莫按呢」；「不用」→「免」。\n"
            "★ 高頻動詞與形容詞：食（吃）、啉（喝）、睏（睡）、行（走）、走（跑）、看（看）、"
            "予（給）、講（說）、知影（知道）、敢（敢/可不可以）、會曉（會、懂）、會使（可以）、"
            "好食（好吃）、好聽、媠、忝（累）、爽快、阿莎力。\n"
            "★ 高頻地方詞：厝（家）、阿爸、阿母、阿公、阿嬤、阿叔、阿姆、頭家（老闆）、頭家娘、"
            "𤆬囝（帶小孩）、做穡（幹活）、討海（出海捕魚）、食茶（喝功夫茶）、泡茶話仙、"
            "拜拜、廟仔、起厝、囡仔、安怎、按呢、遮（這裡）、遐（那裡）、佗位、咧、嘛、"
            "佮意（喜歡）、無要緊（不要緊）、加減（多少）、煞煞去（算了）、"
            "歡頭喜面（高興）、頇顢（笨拙）、好佳哉（幸好）、衰潲（倒楣）。\n"
            "★ 句末助詞：啦、喔、咧、呢、嘛、欸、矣、矣啦、囉。"
            "例：「按呢敢會使啦」「食飽未咧」「免緊張啦」「好啦好啦，煞煞去」。\n"
            "★ 文化氣息：閩南家族觀念重，講話帶長輩口氣、愛講道理、愛勸人，"
            "席間離不開「食茶」「拜拜」「𤆬囝」「討海」「起厝」這些底色。"
            "適合用一兩句俗諺收尾，例如「拚過 chiah 知影」「食人一斤 嘛還人四兩」「天公疼憨人」。\n"
            "★ 語氣：比臺灣腔更穩重、更慢、有種長輩泡茶話仙嘅厚實感。\n"
            "★ 反面禁忌：禁止寫成普通話加幾個閩字（如「我覺得這個事情非常好」這種）；"
            "禁止用「是、什麼、怎麼、沒有、了、的、嗎」等普通話寫法（要寫「是 / 毋是」「啥物」「按怎」"
            "「無」「咧 / 矣」「ê」「無？/ 無啦」）；禁止用簡體字；禁止跟臺灣腔混淆"
            "（如過度用「讚啦」「夯」「鬥陣」這類偏臺青年詞，福建腔更愛用「好」「相佮」）。"
            "整段要讓泉州 / 廈門人讀起來覺得「這就是我厝裡阿伯講話的款」。"
        ),
        fallback_warning="Output drifted toward more standard written Chinese.",
        description_short="福建閩南語（試用版）",
        description_style="汝、阮、伊、按呢、食茶、討海——茶香與海風。",
        keywords=(
            "汝",
            "阮",
            "伊",
            "𪜶",
            "毋",
            "袂",
            "佮",
            "按呢",
            "按怎",
            "啥物",
            "佗位",
            "食茶",
            "討海",
            "拜拜",
            "厝",
            "頭家",
            "知影",
            "好佳哉",
            "煞煞去",
            "啦",
            "矣",
        ),
        landmarks=(
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/%E9%BC%93%E6%B5%AA%E5%B1%BF_-_panoramio.jpg?width=2400",
                "鼓浪屿",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Hakka_china.jpg?width=2400", "福建土楼"
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Wuyi_Mountains_Sea_of_clouds_4.jpg?width=2400",
                "武夷山",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/Xiamen_night_cityscape_2018_-_Flickr_-_Jaykhuang.jpg?width=2400",
                "厦门海岸",
            ),
            Landmark(
                "https://commons.wikimedia.org/wiki/Special:FilePath/20230130_Old_City_of_Quanzhou_01.jpg?width=2400",
                "泉州古城",
            ),
        ),
        trial=True,
        tts_lang=None,
        model="deepseek-v4-pro",  # low-resource variety → pro tier (2026-08 experiment)
    ),
}


def preset_options() -> list[dict[str, Any]]:
    """Full preset config consumed by /api/presets — frontend has no other variety table."""
    return [
        {
            "value": preset.value,
            "label": meta.label,
            "script": meta.script.value,
            "letter": meta.letter,
            "trial": meta.trial,
            "tts_lang": meta.tts_lang,
            "description_short": meta.description_short,
            "description_style": meta.description_style,
            "keywords": list(meta.keywords),
            "landmarks": [lm.as_dict() for lm in meta.landmarks],
        }
        for preset, meta in PRESET_METADATA.items()
    ]
