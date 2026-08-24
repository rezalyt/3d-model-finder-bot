const PROFILE_ALIASES = {
  landscape: ['ландшафт', 'ландшафтный', 'landscape', 'сад', 'садовый', 'озеленение', 'благоустройство'],
  architecture: ['архитектура', 'архитектурный', 'architecture', 'экстерьер', 'facade', 'фасад'],
  interior: ['интерьер', 'interior', 'комната', 'гостиная', 'спальня', 'кухня'],
  visualization: ['визуализация', 'visualization', 'виз', 'render', 'рендер', 'визуализатор', 'visualizer'],
};

const SOFTWARE_ALIASES = {
  sketchup: ['sketchup', 'скетчап', 'skp'],
  blender: ['blender', 'блендер', 'blend'],
  '3dsmax': ['3ds max', '3d max', '3dsmax', 'max'],
  'd5-render': ['d5', 'd5 render'],
  lumion: ['lumion'],
  enscape: ['enscape'],
  twinmotion: ['twinmotion'],
  revit: ['revit', 'rvt'],
  archicad: ['archicad', 'archicad'],
};

const AEC_CATEGORIES = {
  vegetation: ['дерево', 'деревья', 'tree', 'trees', 'берёза', 'береза', 'сосна', 'ель', 'ёлка', 'елка', 'клён', 'клен', 'куст', 'кустарник', 'shrub', 'plant', 'растение', 'цветок', 'flower', 'трава', 'grass', 'hedge', 'живая изгородь'],
  hardscape: ['камень', 'камни', 'rock', 'rocks', 'валун', 'boulder', 'гравий', 'gravel', 'дорожка', 'path', 'paving', 'плитка', 'бетон', 'concrete', 'asphalt'],
  furniture: ['скамейка', 'bench', 'стол', 'table', 'стул', 'chair', 'мебель', 'furniture', 'шезлонг', 'lounger', 'диван', 'sofa', 'кресло', 'armchair', 'кровать', 'bed', 'шкаф', 'wardrobe', 'desk', 'письменный стол', 'книжный шкаф', 'bookshelf'],
  lighting: ['фонарь', 'светильник', 'lamp', 'light', 'освещение', 'бра', 'pendant', 'люстра', 'chandelier'],
  structures: ['беседка', 'gazebo', 'пергола', 'pergola', 'забор', 'fence', 'ворота', 'gate', 'лестница', 'staircase', 'дверь', 'door', 'окно', 'window', 'фасад', 'facade'],
  site: ['фонтан', 'fountain', 'бассейн', 'pool', 'pond', 'пруд', 'водоём', 'водоем', 'planter', 'кашпо', 'вазон', 'клумба', 'courtyard', 'терраса', 'terrace'],
  props: ['человек', 'people', 'human', 'автомобиль', 'машина', 'car', 'велосипед', 'bike', 'скульптура', 'sculpture', 'телевизор', 'television', 'холодильник', 'refrigerator', 'раковина', 'sink', 'ванна', 'bathtub', 'унитаз', 'toilet'],
  kitchen: ['кухня', 'kitchen', 'остров', 'island', 'cabinet', 'cabinetry', 'шкафчик', 'stove', 'плита'],
  bathroom: ['ванная', 'bathroom', 'sink', 'toilet', 'bathtub', 'shower', 'душ'],
  openings: ['дверь', 'door', 'окно', 'window', 'curtain', 'штора'],
  building_elements: ['стена', 'wall', 'brick', 'кирпич', 'мармур', 'marble', 'пол', 'floor', 'потолок', 'ceiling', 'radiator', 'радиатор'],
};

function normalize(value) {
  return String(value || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').replace(/\s+/g, ' ').trim();
}

function matchAlias(text, aliases) {
  const normalized = normalize(text);
  for (const [canonical, values] of Object.entries(aliases)) {
    if (values.some(value => normalized.includes(normalize(value)))) return canonical;
  }
  return null;
}

export function inferTaskProfile(query = '', explicitTask = null, explicitCategory = null) {
  const task = explicitTask && PROFILE_ALIASES[explicitTask]
    ? explicitTask
    : (matchAlias(query, PROFILE_ALIASES) || 'visualization');
  const software = matchAlias(query, SOFTWARE_ALIASES);
  let category = explicitCategory || null;

  if (!category) category = matchAlias(query, AEC_CATEGORIES);

  return { task, software, category };
}
