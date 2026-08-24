const PROFILE_ALIASES = {
  landscape: ['ландшафт', 'ландшафтный', 'landscape', 'сад', 'садовый', 'озеленение', 'благоустройство'],
  architecture: ['архитектура', 'архитектурный', 'architecture', 'экстерьер', 'facade', 'фасад'],
  interior: ['интерьер', 'interior', 'комната', 'гостиная', 'спальня', 'кухня'],
  visualization: ['визуализация', 'visualization', 'виз', 'render', 'рендер'],
};

const SOFTWARE_ALIASES = {
  sketchup: ['sketchup', 'скетчап', 'skp'],
  blender: ['blender', 'блендер', 'blend'],
  '3dsmax': ['3ds max', '3d max', '3dsmax', 'max'],
  'd5-render': ['d5', 'd5 render'],
  lumion: ['lumion'],
  enscape: ['enscape'],
  twinmotion: ['twinmotion'],
};

const LANDSCAPE_CATEGORIES = {
  vegetation: ['дерево', 'деревья', 'tree', 'trees', 'берёза', 'береза', 'сосна', 'ель', 'ёлка', 'елка', 'клён', 'клен', 'куст', 'кустарник', 'shrub', 'plant', 'растение', 'цветок', 'flower', 'трава', 'grass', 'hedge', 'живая изгородь'],
  hardscape: ['камень', 'камни', 'rock', 'rocks', 'валун', 'boulder', 'гравий', 'gravel', 'дорожка', 'path', 'paving', 'плитка'],
  furniture: ['скамейка', 'bench', 'стол', 'table', 'стул', 'chair', 'мебель', 'furniture', 'шезлонг', 'lounger'],
  lighting: ['фонарь', 'светильник', 'lamp', 'light', 'освещение'],
  structures: ['беседка', 'gazebo', 'пергола', 'pergola', 'забор', 'fence', 'ворота', 'gate'],
  site: ['фонтан', 'fountain', 'бассейн', 'pool', 'pond', 'пруд', 'водоём', 'водоем', 'planter', 'кашпо', 'вазон'],
  props: ['человек', 'people', 'human', 'автомобиль', 'машина', 'car', 'велосипед', 'bike', 'скульптура', 'sculpture'],
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
    : (matchAlias(query, PROFILE_ALIASES) || 'landscape');
  const software = matchAlias(query, SOFTWARE_ALIASES);
  let category = explicitCategory || null;

  if (!category && task === 'landscape') {
    category = matchAlias(query, LANDSCAPE_CATEGORIES);
  }

  return { task, software, category };
}
