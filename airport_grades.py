"""Verified ICAO Aerodrome Reference Codes (3C / 4C / 4D / 4E / 4F) and ground-facility /
high-plateau fleet rules for all 243 vCCA + ZBAL airports.

Policy (NAIP 2612 + International/Regional AIP + High-Plateau Operations):
  - Aerodrome grades verified against CAAC NAIP Cycle 2612 (EFF 2026-11-26) for all 136 active
    Chinese mainland airports, plus CAAP AIP (RPLL 4E) and Taiwan CAA AIP (RCSS Apron 1 limit).
  - No 2B centimeter-level B38M exclusions (ZBCZ, ZBES, ZSGS retain B38M).
  - All 4D airports retain all 4D aircraft (B752, B762, B763): ZUXC, ZSLG, ZWKL, ZUNZ retain B752.
  - High-plateau airports (elevation >= 2,438 m / 8,000 ft: ZUDC, ZUBD, ZUDR, ZUSH, ZULS, ZUHY,
    ZUNZ) exclude A321 (26074), A21N (26076), and C909 (26102).
  - High-plateau airports with historical scheduled B757-200 service (ZULS, ZUNZ, ZUBD) include
    B752 (26090) when the other endpoint also permits B752.
  - Every airport supporting A333 (26078) also supports A339 (26591) (including RCSS).
"""

GRADE_RANK = {"3C": 1, "4C": 2, "4D": 3, "4E": 4, "4F": 5}
RANK_TO_GRADE = {v: k for k, v in GRADE_RANK.items()}

AIRPORT_GRADE = {
    # === 中国内地 4F (18, 经 NAIP 2612 逐一核实) ===
    "ZBAA": "4F",  # 北京首都
    "ZBAD": "4F",  # 北京大兴
    "ZBHH": "4F",  # 呼和浩特盛乐 (NAIP 2612 RWY07/25: 4F, 23个F位)
    "ZSPD": "4F",  # 上海浦东
    "ZGGG": "4F",  # 广州白云
    "ZGSZ": "4F",  # 深圳宝安
    "ZUUU": "4F",  # 成都双流
    "ZUTF": "4F",  # 成都天府
    "ZPPP": "4F",  # 昆明长水
    "ZLXY": "4F",  # 西安咸阳
    "ZUCK": "4F",  # 重庆江北
    "ZHHH": "4F",  # 武汉天河
    "ZSHC": "4F",  # 杭州萧山 (NAIP 2612 实有9个≥68.4×76.3m F位)
    "ZHCC": "4F",  # 郑州新郑
    "ZSNJ": "4F",  # 南京禄口
    "ZSQD": "4F",  # 青岛胶东
    "ZWWW": "4F",  # 乌鲁木齐天山
    "ZSFZ": "4F",  # 福州长乐 (NAIP 2612 RWY04/22: 4F, 实有6个F位)

    # === 港澳台 (5) ===
    "VHHH": "4F",  # 香港国际
    "RCTP": "4F",  # 台北桃园
    "VMMC": "4E",  # 澳门国际
    "RCKH": "4E",  # 高雄国际
    "RCSS": "4E",  # 台北松山 (ICAO ANP 4E, 支持A333/A339, 剔B744/B77W/A359/A346)

    # === 中国内地 4E (35, 经 NAIP 2612 逐一核实) ===
    "ZBDS": "4E",  # 鄂尔多斯
    "ZBSJ": "4E",  # 石家庄正定
    "ZBTJ": "4E",  # 天津滨海
    "ZBYC": "4E",  # 运城盐湖
    "ZBYN": "4E",  # 太原武宿
    "ZGHA": "4E",  # 长沙黄花 (NAIP 2612 4E, 全机位≤65m, 搬移上限B747-400)
    "ZGKL": "4E",  # 桂林两江
    "ZGNN": "4E",  # 南宁吴圩
    "ZGOW": "4E",  # 揭阳潮汕 (NAIP 2612 4E, 6个E类机位)
    "ZGSD": "4E",  # 珠海金湾
    "ZGZJ": "4E",  # 湛江吴川
    "ZJHK": "4E",  # 海口美兰 (NAIP 2612 4E, AD 2.20.1.3限B747-400及以下)
    "ZJSY": "4E",  # 三亚凤凰
    "ZLIC": "4E",  # 银川河东
    "ZLLL": "4E",  # 兰州中川
    "ZLXN": "4E",  # 西宁曹家堡
    "ZSAM": "4E",  # 厦门高崎
    "ZSCG": "4E",  # 常州奔牛
    "ZSCN": "4E",  # 南昌昌北
    "ZSJN": "4E",  # 济南遥墙
    "ZSJX": "4E",  # 嘉兴南湖
    "ZSNB": "4E",  # 宁波栎社
    "ZSNT": "4E",  # 南通兴东
    "ZSOF": "4E",  # 合肥新桥 (NAIP 2612 4E, 机位最大长≤74m, 不容A346)
    "ZSSS": "4E",  # 上海虹桥
    "ZSWZ": "4E",  # 温州龙湾
    "ZSYA": "4E",  # 扬州泰州
    "ZSYT": "4E",  # 烟台蓬莱
    "ZUGY": "4E",  # 贵阳龙洞堡
    "ZULS": "4E",  # 拉萨贡嘎 (高高原3570.8m; 历史757; 窄体剔A321/A21N/C909; 宽体剔B744/B77W/A359/A346)
    "ZWSH": "4E",  # 喀什徕宁
    "ZYCC": "4E",  # 长春龙嘉 (NAIP 2612 4E, 常规E位均<71m, 02号为除冰坪)
    "ZYHB": "4E",  # 哈尔滨太平
    "ZYTL": "4E",  # 大连周水子
    "ZYTX": "4E",  # 沈阳桃仙

    # === 中国内地 4D (21, 含非在飞ZUJZ; 所有4D机场均保留4D机型) ===
    "ZBLA": "4D",  # 海拉尔
    "ZGDY": "4D",  # 张家界荷花
    "ZHYC": "4D",  # 宜昌三峡
    "ZLDH": "4D",  # 敦煌莫高
    "ZLYL": "4D",  # 榆林榆阳
    "ZPJH": "4D",  # 西双版纳嘎洒
    "ZPLJ": "4D",  # 丽江三义
    "ZSDY": "4D",  # 东营胜利
    "ZSLG": "4D",  # 连云港花果山 (NAIP 2612 4D, 保留4D机型)
    "ZSLY": "4D",  # 临沂启阳
    "ZSQZ": "4D",  # 泉州晋江
    "ZSTX": "4D",  # 黄山屯溪
    "ZSWH": "4D",  # 威海大水泊
    "ZSYW": "4D",  # 义乌 (NAIP 2612 4D, 11号位≤52m)
    "ZUJZ": "4D",  # 九寨黄龙 (高高原, 非在飞)
    "ZUMY": "4D",  # 绵阳南郊 (NAIP 2612 4D, 11号位B757-200及以下)
    "ZUNZ": "4D",  # 林芝米林 (高高原2950.4m; 4D+历史757保留B752; 高高原剔A321/A21N/C909, 与拉萨/昌都统一8型)
    "ZUXC": "4D",  # 西昌青山 (NAIP 2612 4D, 保留4D机型)
    "ZWKL": "4D",  # 库尔勒梨城 (NAIP 2612 4D, 保留4D机型)
    "ZWKM": "4D",  # 克拉玛依古海 (NAIP 2612 4D, 03-09号≤52×67m)
    "ZWTN": "4D",  # 和田昆冈

    # === 中国内地 4C (66, 含ZBAL与降级6座) + 朝鲜 ZKPY ===
    "ZBAL": "4C",  # 阿拉善左旗巴彦浩特 (NAIP 2612 4C, AD 2.20.3.1限01-03号≤30m×37m/消防5级, 锁C909)
    "ZBCF": "4C",  # 赤峰玉龙
    "ZBCZ": "4C",  # 长治王村 (NAIP 2612 4C, 不使用2B, 保留B38M)
    "ZBES": "4C",  # 阿尔山伊尔施 (NAIP 2612 4C, 不使用2B, 保留B38M)
    "ZBLF": "4C",  # 临汾尧都
    "ZBLL": "4C",  # 吕梁大武
    "ZBOW": "4C",  # 包头东河 (NAIP 2612 4C, 搬移限A321)
    "ZBTL": "4C",  # 通辽
    "ZBUH": "4C",  # 乌海
    "ZBUL": "4C",  # 乌兰浩特
    "ZBXH": "4C",  # 锡林浩特
    "ZBYZ": "4C",  # 巴彦淖尔天吉泰
    "ZGBH": "4C",  # 北海福成 (NAIP 2612 4C, 搬移/桥载按C类)
    "ZGHY": "4C",  # 衡阳南岳
    "ZGHZ": "4C",  # 惠州平潭
    "ZGMX": "4C",  # 梅州梅县
    "ZGSY": "4C",  # 邵阳武冈 (NAIP 2612 4C, 全4机位限36m×40m, 剔A321/A21N)
    "ZGZH": "4C",  # 柳州白莲 (NAIP 2612 4C, 搬移/桥载按C类)
    "ZHSY": "4C",  # 十堰武当山
    "ZLHZ": "4C",  # 汉中城固 (NAIP 2612 4C, 全可用位限<36m×<42.5m, 剔A321/A21N)
    "ZLYA": "4C",  # 延安南泥湾
    "ZPBS": "4C",  # 保山云瑞
    "ZPDL": "4C",  # 大理凤仪
    "ZPMS": "4C",  # 德宏芒市 (VAMSYS现用代码)
    "ZSBO": "4C",  # 亳州
    "ZSFY": "4C",  # 阜阳西关
    "ZSGS": "4C",  # 井冈山 (NAIP 2612 4C, 全位限36m×39.5m, 剔A321/A21N; 不使用2B保留B38M)
    "ZSGZ": "4C",  # 赣州黄金
    "ZSJD": "4C",  # 景德镇罗家
    "ZSJU": "4C",  # 衢州
    "ZSLI": "4C",  # 丽水
    "ZSLQ": "4C",  # 台州路桥
    "ZSRJ": "4C",  # 瑞金
    "ZSSR": "4C",  # 上饶三清山
    "ZSWY": "4C",  # 武夷山
    "ZSYN": "4C",  # 盐城南洋 (NAIP 2612 4C, 搬移上限A321)
    "ZUAL": "4C",  # 阿里昆莎 (高高原, 非在飞)
    "ZUBD": "4C",  # 昌都邦达 (高高原4332.9m; 1995年历史开通B757航线增加B752; 剔A321/A21N/C909)
    "ZUBZ": "4C",  # 巴中恩阳
    "ZUDA": "4C",  # 达州金垭
    "ZUDC": "4C",  # 稻城亚丁 (高高原4411.0m, 剔A321/A21N/C909)
    "ZUDR": "4C",  # 日喀则定日 (高高原4316.5m, 剔A321/A21N/C909)
    "ZUGU": "4C",  # 广元盘龙
    "ZUHY": "4C",  # 阿坝红原 (高高原3539.7m, 剔A321/A21N/C909)
    "ZULZ": "4C",  # 泸州云龙
    "ZUMT": "4C",  # 遵义茅台
    "ZUNC": "4C",  # 南充高坪
    "ZUPS": "4C",  # 六盘水月照
    "ZUSH": "4C",  # 山南隆子 (高高原3950m, 剔A321/A21N/C909)
    "ZUWX": "4C",  # 重庆万州五桥
    "ZUYB": "4C",  # 宜宾五粮液 (NAIP 2612 4C, 全13机位≤36m)
    "ZUZH": "4C",  # 攀枝花保安营
    "ZWAK": "4C",  # 阿克苏红旗坡
    "ZWAT": "4C",  # 阿勒泰雪都
    "ZWHM": "4C",  # 哈密伊州
    "ZWKC": "4C",  # 库车龟兹
    "ZWTS": "4C",  # 图木舒克唐王城
    "ZWYN": "4C",  # 伊犁伊宁
    "ZYDD": "4C",  # 丹东浪头
    "ZYDQ": "4C",  # 大庆萨尔图
    "ZYFY": "4C",  # 抚远东极
    "ZYJM": "4C",  # 佳木斯松江
    "ZYJS": "4C",  # 建三江湿地
    "ZYQQ": "4C",  # 齐齐哈尔三家子
    "ZYSD": "4C",  # 绥芬河东宁 (NAIP 2612 4C; 注: 松原查干湖代码为ZYSQ)
    "ZYTN": "4C",  # 通化三源浦
    "ZYYJ": "4C",  # 延吉朝阳川
    "ZKPY": "4E",  # 平壤顺安 (4000m)

    # === 国际 4F (32) ===
    "CYVR": "4F",  # 温哥华
    "CYYZ": "4F",  # 多伦多皮尔逊
    "EDDB": "4F",  # 柏林勃兰登堡
    "EDDF": "4F",  # 法兰克福
    "EDDM": "4F",  # 慕尼黑
    "EGLL": "4F",  # 伦敦希思罗
    "EHAM": "4F",  # 阿姆斯特丹史基浦
    "FAOR": "4F",  # 约翰内斯堡
    "HECA": "4F",  # 开罗 (60m跑道)
    "KIAD": "4F",  # 华盛顿杜勒斯
    "KJFK": "4F",  # 纽约肯尼迪
    "KLAX": "4F",  # 洛杉矶
    "KMIA": "4F",  # 迈阿密
    "KORD": "4F",  # 芝加哥奥黑尔
    "KSFO": "4F",  # 旧金山
    "LEBL": "4F",  # 巴塞罗那 (60m跑道)
    "LEMD": "4F",  # 马德里巴拉哈斯
    "LFPG": "4F",  # 巴黎戴高乐
    "LIRF": "4F",  # 罗马菲乌米奇诺
    "LTFM": "4F",  # 伊斯坦布尔新机场
    "NZAA": "4F",  # 奥克兰
    "OMAA": "4F",  # 阿布扎比
    "OMDB": "4F",  # 迪拜
    "OMDW": "4F",  # 迪拜阿勒马克图姆
    "OPIS": "4F",  # 伊斯兰堡 (60m跑道)
    "RJAA": "4F",  # 东京成田
    "RJBB": "4F",  # 大阪关西
    "RJTT": "4F",  # 东京羽田
    "RKSI": "4F",  # 首尔仁川
    "SBGR": "4F",  # 圣保罗瓜鲁柳斯
    "UUEE": "4F",  # 莫斯科谢列梅捷沃
    "VDTI": "4F",  # 金边德崇
    "VIDP": "4F",  # 德里甘地 (11R/29L 60m)
    "VTBS": "4F",  # 曼谷素万那普
    "WIII": "4F",  # 雅加达苏加诺-哈达 (60m跑道)
    "WMKK": "4F",  # 吉隆坡
    "WSSS": "4F",  # 新加坡樟宜
    "YMML": "4F",  # 墨尔本
    "YSSY": "4F",  # 悉尼

    # === 国际 4E (53) ===
    "BIKF": "4E",  # 凯夫拉维克
    "EBBR": "4E",  # 布鲁塞尔
    "EBLG": "4E",  # 列日
    "EGKK": "4E",  # 伦敦盖特威克
    "EGPK": "4E",  # 格拉斯哥普雷斯威克
    "EKCH": "4E",  # 哥本哈根
    "EPWA": "4E",  # 华沙肖邦
    "ESSA": "4E",  # 斯德哥尔摩阿兰达
    "KADW": "4E",  # 安德鲁斯基地
    "KPAE": "4E",  # 西雅图佩恩
    "LDZA": "4E",  # 萨格勒布
    "LGAV": "4E",  # 雅典
    "LHBP": "4E",  # 布达佩斯
    "LIMC": "4E",  # 米兰马尔彭萨
    "LIPZ": "4E",  # 威尼斯马可波罗
    "LOWW": "4E",  # 维也纳
    "LROP": "4E",  # 布加勒斯特
    "LSGG": "4E",  # 日内瓦
    "LSZH": "4E",  # 苏黎世
    "MMSM": "4E",  # 墨西哥城圣卢西亚 AIFA (45m跑道)
    "MUHA": "4E",  # 哈瓦那 (45m跑道)
    "MYNN": "4E",  # 拿骚 (45m跑道)
    "OERK": "4E",  # 利雅得
    "OIIE": "4E",  # 德黑兰
    "OJAI": "4E",  # 安曼
    "OKKK": "4E",  # 科威特
    "OPKC": "4E",  # 卡拉奇
    "PANC": "4E",  # 安克雷奇
    "RJGG": "4E",  # 名古屋中部
    "RKPC": "4E",  # 济州
    "RKPK": "4E",  # 金海
    "RKSS": "4E",  # 首尔金浦
    "RPLL": "4E",  # 马尼拉 NAIA (CAAP AIP / ICAO ANP 4E, 航站楼全E类机位)
    "SKBO": "4E",  # 波哥大 (45m跑道)
    "SVMI": "4E",  # 加拉加斯 (45m跑道)
    "UAAA": "4E",  # 阿拉木图
    "UACC": "4E",  # 阿斯塔纳
    "UCFM": "4E",  # 比什凯克玛纳斯
    "UGTB": "4E",  # 第比利斯
    "UHHH": "4E",  # 伯力哈巴罗夫斯克
    "UHWW": "4E",  # 海参崴
    "UMMS": "4E",  # 明斯克
    "UZTT": "4E",  # 塔什干
    "VCBI": "4E",  # 科伦坡
    "VGHS": "4E",  # 达卡
    "VNKT": "4E",  # 加德满都
    "VOBL": "4E",  # 班加罗尔
    "VTCC": "4E",  # 清迈
    "VTSP": "4E",  # 普吉
    "VVNB": "4E",  # 河内内排
    "VVTS": "4E",  # 胡志明新山一
    "VYYY": "4E",  # 仰光
    "WMKP": "4E",  # 槟城
    "ZMCK": "4E",  # 乌兰巴托成吉思汗

    # === 国际 4D / 4C / 3C (4) ===
    "UIAA": "4D",  # 赤塔
    "UIII": "4D",  # 伊尔库茨克
    "PADQ": "4C",  # 科迪亚克
    "CYKA": "3C",  # 坎卢普斯
}

# Active & catalog high-plateau airports (CAAC definition: elevation >= 2,438 m / 8,000 ft)
HIGH_PLATEAU_AIRPORTS: set[str] = {
    "ZUDC",  # 稻城亚丁 4411.0m
    "ZUBD",  # 昌都邦达 4332.9m
    "ZUDR",  # 日喀则定日 4316.5m
    "ZUSH",  # 山南隆子 3950m
    "ZULS",  # 拉萨贡嘎 3570.8m
    "ZUHY",  # 阿坝红原 3539.7m
    "ZUNZ",  # 林芝米林 2950.4m
    # Inactive catalog high-plateau airports (kept for completeness)
    "ZUAL",  # 阿里昆莎 4274m
    "ZUJZ",  # 九寨黄龙 3447.6m
    "SKBO",  # 波哥大 2548m
}

# High-plateau airports with verified historical scheduled B757-200 service
# (ZULS 4E & ZUNZ 4D already qualify via grade >= 4D; ZUBD 4C explicitly granted B752)
HISTORIC_B757_HIGH_PLATEAU_AIRPORTS: set[str] = {
    "ZULS",  # 拉萨贡嘎 (西南航/国航主力757航线)
    "ZUNZ",  # 林芝米林 (2006年国航B757-200 RNP-AR首航并长期执飞)
    "ZUBD",  # 昌都邦达 (1995-04-28西南航/国航B757-200首航成都-昌都并长期执飞)
    "ZUJZ",  # 九寨黄龙 (非在飞; 历史国航757执飞)
}

# Stand-level / ground-facility & high-plateau exclusions
# Fleet ID reference:
#   26071 A319 | 26072 A320 | 26073 A20N | 26074 A321 | 26076 A21N
#   26084 B737 | 26085 B738 | 26086 B38M | 26097 C919 | 26102 C909 | 26090 B752
#   26091 B762 | 26092 B763 | 26077 A332 | 26078 A333 | 26079 A343 | 26080 A359
#   26087 B742 | 26088 B744 | 26089 B748 | 26093 B772 | 26094 B77W | 26095 B789
#   26591 A339 | 26594 A346 | 26718 BJN B738 | 26719 CCD B738
_NON_C909_PAX = {
    "26071", "26072", "26073", "26074", "26076",
    "26084", "26085", "26086", "26097", "26090",
    "26091", "26092", "26077", "26078", "26079", "26080",
    "26087", "26088", "26089", "26093", "26094", "26095",
    "26591", "26594", "26718", "26719",
}
_HIGH_PLATEAU_EXCLUDED = {"26074", "26076", "26102"}  # A321, A21N, C909

AIRPORT_FLEET_EXCLUSIONS: dict[str, set[str]] = {
    # 4D 高高原林芝：保留 4D/历史757 (B752)，放开 B738/B38M/C919，与拉萨/昌都统一高高原剔除 A321/A21N/C909
    "ZUNZ": set(_HIGH_PLATEAU_EXCLUDED),
    # 其余高高原机场：统一剔除 A321/A21N/C909 (ZULS另叠加宽体机位限尺寸)
    "ZUDC": set(_HIGH_PLATEAU_EXCLUDED),
    "ZUBD": set(_HIGH_PLATEAU_EXCLUDED),
    "ZUDR": set(_HIGH_PLATEAU_EXCLUDED),
    "ZUSH": set(_HIGH_PLATEAU_EXCLUDED),
    "ZUHY": set(_HIGH_PLATEAU_EXCLUDED),
    "ZUAL": set(_HIGH_PLATEAU_EXCLUDED),
    "ZUJZ": set(_HIGH_PLATEAU_EXCLUDED),
    # 4C 机位机长受限 (不使用2B，保留B38M)
    "ZBAL": {"26074", "26076"},             # 4C 剔 A321/A21N
    "ZGSY": {"26074", "26076"},             # 全位限长40m → 剔 A321/A21N
    "ZLHZ": {"26074", "26076"},             # 全可用位限长<42.5m → 剔 A321/A21N
    "ZSGS": {"26074", "26076"},             # 全位限长39.5m → 剔 A321/A21N (保留B38M)
    # 4E 宽体机位尺寸受限 (所有支持A333的机场均支持A339)
    "ZULS": _HIGH_PLATEAU_EXCLUDED | {"26088", "26094", "26080", "26594"},  # 高高原剔A321/A21N/C909 + 宽体剔B744/B77W/A359/A346
    "ZYCC": {"26094", "26594"},                                              # 常规E位全<71m (02为除冰坪) → 剔 B77W/A346
    "ZSOF": {"26594"},                                                       # 全机坪最大长≤74m → 剔 A346 (75.36m)
    "RCSS": {"26088", "26094", "26080", "26594"},                            # 支持A333即支持A339 → 剔 B744/B77W/A359/A346
}


def get_airport_grade(icao: str) -> str:
    return AIRPORT_GRADE.get(str(icao or "").strip().upper(), "4C")


def route_min_grade(orig: str, dest: str) -> tuple[str, str, str]:
    g1 = get_airport_grade(orig)
    g2 = get_airport_grade(dest)
    mg = RANK_TO_GRADE[min(GRADE_RANK[g1], GRADE_RANK[g2])]
    return g1, g2, mg


def airport_allows_b752(icao: str) -> bool:
    """Return True if airport allows B752 (grade >= 4D, or historic B757 high-plateau airport)."""
    ap = str(icao or "").strip().upper()
    if ap in AIRPORT_FLEET_EXCLUSIONS and "26090" in AIRPORT_FLEET_EXCLUSIONS[ap]:
        return False
    return GRADE_RANK[get_airport_grade(ap)] >= GRADE_RANK["4D"] or ap in HISTORIC_B757_HIGH_PLATEAU_AIRPORTS


def route_allows_b752(orig: str, dest: str) -> bool:
    return airport_allows_b752(orig) and airport_allows_b752(dest)


def get_route_fleet_exclusions(orig: str, dest: str, route_type: str = "scheduled") -> set[str]:
    """Return stand/ground-facility & high-plateau excluded Fleet IDs for an O/D pair."""
    excl: set[str] = set()
    for ap in (str(orig or "").strip().upper(), str(dest or "").strip().upper()):
        excl |= AIRPORT_FLEET_EXCLUSIONS.get(ap, set())
    return excl
