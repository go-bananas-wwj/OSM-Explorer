"""Query-only multilingual vocabulary. Never rewrites source v2 labels."""
import re,unicodedata
from functools import lru_cache

@lru_cache(maxsize=200000)
def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC',str(text or '')).casefold().split())

@lru_cache(maxsize=200000)
def fold_name(text):
    # Accent-insensitive Latin matching, preserving Japanese/Cyrillic/Arabic marks.
    result=[];latin=False
    for char in unicodedata.normalize('NFD',normalize(text)):
        if unicodedata.combining(char):
            if not latin:result.append(char)
        else:
            latin='LATIN' in unicodedata.name(char,'');result.append(char)
    return unicodedata.normalize('NFC',''.join(result))

NAME_FAMILIES={'name','official_name','alt_name','short_name','loc_name','old_name','int_name'}
def is_name_key(key):
    head,sep,suffix=key.partition(':')
    return head in NAME_FAMILIES and (not sep or bool(re.fullmatch(r'[a-z]{2,3}(?:[-_][A-Za-z0-9]+)*',suffix)))

# (Chinese concept, canonical targets, synonyms in common query languages)
CONCEPTS=[
 ('光伏',['theme:光伏','tag:generator:source=solar','tag:plant:source=solar','tag:generator:method=photovoltaic'],['光伏板','太阳能','太陽能','光伏电站','solar','solar panel','solar panels','solar farm','photovoltaic','panneau solaire','panneaux solaires','centrale solaire','panel solar','paneles solares','planta solar','solaranlage','solarpark','photovoltaikanlage','ソーラーパネル','太陽光発電','太陽光パネル','солнечные панели','солнечная электростанция','ألواح شمسية']),
 ('工厂',['theme:工业设施','tag:landuse=industrial','tag:building=industrial','tag:man_made=works'],['工业设施','工業設施','工廠','厂房','工廠建築','factory','factories','industrial','industrial facility','industrial facilities','usine','usines','fábrica','fábricas','fabrik','fabriken','工場','工業施設','завод','фабрика','مصنع']),
 ('工业用地',['tag:landuse=industrial'],['工業用地','industrial land','industrial area','zone industrielle','zona industrial','industriegebiet','工業用地','промышленная зона']),
 ('工业建筑',['tag:building=industrial'],['工業建築','industrial building','industrial buildings','bâtiment industriel','edificio industrial','industriegebäude']),
 ('风电',['theme:风电','tag:generator:source=wind','tag:plant:source=wind'],['風電','风力发电','风机','wind turbine','wind turbines','wind farm','wind power','éolienne','éoliennes','parc éolien','aerogenerador','parque eólico','windkraftanlage','windpark','風力発電','ветряная электростанция','ветрогенератор','طاقة الرياح']),
 ('农田',['theme:农田','category:耕地','tag:landuse=farmland'],['農田','耕地','farmland','cropland','agricultural land','champs agricoles','terres agricoles','tierras agrícolas','ackerland','農地','пашня','أراض زراعية']),
 ('水域',['theme:水域','category:水面','tag:natural=water'],['水体','水體','水面','water','water body','water bodies','plan d’eau','plan d eau','cuerpo de agua','gewässer','水域','водоем','водоём','مسطح مائي']),
 ('机场',['theme:机场','tag:aeroway=aerodrome','tag:aeroway=runway'],['機場','airport','airports','aéroport','aeropuerto','flughafen','空港','аэропорт','مطار']),
 ('港口',['theme:港口','tag:landuse=port','tag:harbour=yes'],['port','harbor','harbour','seaport','puerto','hafen','港湾','морской порт','ميناء']),
 ('矿场',['theme:矿场','tag:landuse=quarry','tag:man_made=mine'],['礦場','矿山','礦山','quarry','mine','open pit mine','carrière','mina','steinbruch','tagebau','採石場','карьер','منجم']),
 ('道路',['category:道路'],['road','roads','route','routes','carretera','carreteras','straße','strasse','straßen','道路','дорога','дороги','طريق']),
 ('小路',['category:小路或步道'],['步道','footpath','path','trail','sentier','sendero','fußweg','fussweg','小道','тропа']),
 ('建筑',['category:建筑'],['建築','建筑物','building','buildings','bâtiment','bâtiments','edificio','edificios','gebäude','建物','здание','здания','مبنى']),
 ('建成区',['category:建成区'],['建成區','built up area','built-up area','urban area','zone bâtie','zona urbana','siedlung','市街地','застроенная территория']),
 ('森林',['category:林地','tag:landuse=forest','tag:natural=wood'],['林地','forest','forests','woodland','forêt','bosque','wald','森林','лес','غابة']),
 ('河流',['category:河流或溪流','tag:waterway=river','tag:waterway=stream'],['溪流','river','rivers','stream','creek','rivière','río','rio','fluss','川','река','نهر']),
 ('湖泊',['tag:water=lake','tag:natural=water'],['湖','lake','lakes','lac','lago','see','湖水','озеро','بحيرة']),
 ('沙漠',['category:natural=desert','tag:natural=desert'],['desert','désert','desierto','wüste','砂漠','пустыня','صحراء']),
 ('湿地',['category:natural=wetland','tag:natural=wetland'],['濕地','wetland','wetlands','zone humide','humedal','feuchtgebiet','湿地','болото']),
 ('海岸线',['category:natural=coastline','tag:natural=coastline'],['海岸線','coastline','coast','littoral','costa','küste','海岸線','береговая линия']),
 ('冰川',['category:natural=glacier','tag:natural=glacier'],['glacier','glaciar','gletscher','氷河','ледник']),
 ('铁路',['category:铁路轨道','tag:railway=rail'],['鐵路','railway','railroad','rail','voie ferrée','ferrocarril','eisenbahn','鉄道','железная дорога']),
 ('桥梁',['category:桥梁或高架桥','tag:bridge=yes'],['橋樑','bridge','bridges','pont','puente','brücke','橋','мост']),
 ('停车场',['category:停车场或停车位','tag:amenity=parking'],['停車場','parking','parking lot','aparcamiento','parkplatz','駐車場','парковка']),
 ('运动场',['category:运动场地','tag:leisure=pitch'],['運動場','sports field','pitch','terrain de sport','campo deportivo','sportplatz','運動場','спортивная площадка']),
 ('高尔夫球场',['category:leisure=golf_course','tag:leisure=golf_course'],['高爾夫球場','golf course','terrain de golf','campo de golf','golfplatz','ゴルフ場','поле для гольфа']),
 ('公园',['category:公园或花园','tag:leisure=park','tag:leisure=garden'],['公園','花园','花園','park','garden','parc','jardin','parque','garten','公園','парк']),
 ('墓地',['category:墓地','tag:landuse=cemetery','tag:amenity=grave_yard'],['cemetery','graveyard','cimetière','cementerio','friedhof','墓地','кладбище']),
 ('储罐',['category:储罐','tag:man_made=storage_tank'],['儲罐','storage tank','storage tanks','réservoir industriel','tanque de almacenamiento','lagertank','貯蔵タンク','резервуар']),
 ('污水处理厂',['category:污水处理设施','tag:man_made=wastewater_plant'],['污水處理廠','wastewater treatment plant','sewage treatment plant','station d’épuration','depuradora','kläranlage','下水処理場','очистные сооружения']),
 ('水坝',['tag:waterway=dam','category:waterway=dam'],['水壩','大坝','大壩','dam','barrage','presa','staudamm','ダム','плотина']),
 ('电站',['tag:power=plant','category:power=plant'],['電站','发电厂','發電廠','power plant','power station','centrale électrique','central eléctrica','kraftwerk','発電所','электростанция']),
 ('变电站',['category:变电站设施','tag:power=substation'],['變電站','substation','electrical substation','poste électrique','subestación','umspannwerk','変電所','подстанция']),
]

def concept_matches(query):
    q=fold_name(query);found=[]
    if not q:return found
    for label,targets,aliases in CONCEPTS:
        score=0;matched=None
        for alias in [label]+aliases:
            term=fold_name(alias)
            if q==term:rank=100
            elif len(term)>1 and (re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',q) if term.isascii() else term in q):rank=70+min(len(term),20)
            else:continue
            if rank>score:score=rank;matched=alias
        if score:found.append({'label':label,'targets':targets,'match':matched,'rank':score})
    return sorted(found,key=lambda r:(-r['rank'],r['label']))

CONCEPTS.extend([
 ('仓库',['tag:building=warehouse'],['倉庫','warehouse','warehouses','entrepôt','almacén','lagerhalle','倉庫','склад','مستودع']),
 ('草地',['category:草地'],['grassland','grass','prairie','pradera','grasland','草地','луг']),
 ('灌丛',['category:灌丛'],['灌叢','scrub','shrubland','bushes','broussailles','matorral','gebüsch','低木林','кустарник']),
 ('果园',['category:果园'],['果園','orchard','verger','huerto','obstgarten','果樹園','фруктовый сад']),
 ('树木',['category:树木或树列'],['樹木','tree','trees','tree row','arbres','árboles','bäume','樹木','деревья']),
 ('输电线',['category:架空输电线'],['輸電線','power line','power lines','transmission line','ligne électrique','línea eléctrica','stromleitung','送電線','линия электропередачи']),
 ('塔',['category:塔状构筑物'],['塔状构筑物','tower','towers','tour','torre','turm','塔','башня']),
 ('水渠',['category:人工水道或排水沟'],['排水溝','canal','ditch','drainage ditch','irrigation canal','canal de riego','kanal','用水路','канал']),
 ('闸门',['category:出入口闸门'],['閘門','gate','entrance gate','portail','puerta','tor','門','ворота']),
 ('围栏',['category:围栏'],['圍欄','fence','fencing','clôture','valla','zaun','フェンス','забор']),
 ('围墙',['category:围墙'],['圍牆','wall','walls','mur','muro','mauer','塀','стена']),
 ('隧道',['category:隧道出入口'],['tunnel','tunnel entrance','túnel','tunneleingang','トンネル','тоннель']),
 ('公交站',['category:公交站台或停靠设施'],['公車站','bus stop','bus station','arrêt de bus','parada de autobús','bushaltestelle','バス停','автобусная остановка']),
 ('农场',['category:农场院落'],['農場','farmyard','farm yard','cour de ferme','corral','bauernhof','農場','ферма']),
 ('铁路道口',['category:铁路道口'],['鐵路道口','level crossing','railway crossing','passage à niveau','paso a nivel','bahnübergang','踏切','железнодорожный переезд']),
 ('加油站',['category:加油站设施'],['gas station','petrol station','fuel station','station-service','gasolinera','tankstelle','ガソリンスタンド','автозаправка']),
 ('人行横道',['category:过街设施'],['人行橫道','crosswalk','pedestrian crossing','passage piéton','paso de peatones','fußgängerüberweg','横断歩道','пешеходный переход']),
 ('纪念碑',['category:纪念构筑物'],['紀念碑','monument','memorial','monumento','denkmal','記念碑','памятник']),
 ('儿童游乐场',['category:儿童游乐设施'],['兒童遊樂場','playground','aire de jeux','parque infantil','spielplatz','遊び場','детская площадка']),
 ('游泳池',['category:游泳池'],['swimming pool','pool','piscine','piscina','schwimmbecken','プール','бассейн']),
 ('亭棚',['category:亭棚'],['shelter','pavilion','abri','refugio','unterstand','東屋','павильон']),
 ('施工场地',['category:施工场地'],['施工場地','construction site','chantier','obra','baustelle','工事現場','стройплощадка']),
 ('长椅',['category:长椅'],['長椅','bench','benches','banc','banco','sitzbank','ベンチ','скамейка']),
 ('邮筒',['category:邮筒'],['郵筒','post box','postbox','mailbox','boîte aux lettres','buzón','briefkasten','郵便ポスト','почтовый ящик']),
 ('绿篱',['category:绿篱'],['綠籬','hedge','haie','seto','hecke','生け垣','живая изгородь']),
 ('站台',['category:铁路站台'],['月台','railway platform','train platform','quai','andén','bahnsteig','鉄道ホーム','железнодорожная платформа']),
 ('喷泉',['category:喷泉'],['噴泉','fountain','fontaine','fuente','springbrunnen','噴水','фонтан']),
 ('自行车停车',['category:自行车停放设施'],['自行車停車','bicycle parking','bike parking','stationnement vélo','aparcabicis','fahrradparkplatz','駐輪場','велопарковка']),
 ('城堡',['category:城堡建筑'],['castle','château','castillo','burg','schloss','城','замок']),
 ('充电站',['category:充电设施'],['充電站','charging station','ev charging','borne de recharge','estación de carga','ladestation','充電スタンド','зарядная станция']),
 ('广场',['category:广场'],['廣場','town square','plaza','piazza','stadtplatz','広場','площадь']),
 ('温室',['category:温室'],['溫室','greenhouse','greenhouses','serre','invernadero','gewächshaus','温室','теплица']),
])
# Lake is narrower than all water. Only offer the exact tag when it exists.
CONCEPTS[:]=[(label,(['tag:water=lake'] if label=='湖泊' else targets),aliases) for label,targets,aliases in CONCEPTS]

# Parks and gardens share a broad v2 category, but keep their exact OSM tags separate.
CONCEPTS[:]=[c for c in CONCEPTS if c[0]!='公园']
CONCEPTS.extend([
 ('公园',['tag:leisure=park','category:公园或花园'],['公園','park','parks','parc','parque','公園','парк']),
 ('花园',['tag:leisure=garden','category:公园或花园'],['花園','garden','gardens','jardin','jardín','garten','庭園','сад'])
])
